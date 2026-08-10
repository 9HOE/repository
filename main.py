"""
main.py — Telegram bot entry point.

Commands
--------
/start    Welcome & summary
/run      Start auto-trading loop
/stop     Stop the loop
/status   Balance, positions, P&L, progress
/journal  Last 8 trades with stats
/analyze  Trigger AI strategy review (Claude)
/demo     Toggle demo ↔ live mode
/reset    Reset demo balance back to $5
/settings Show all strategy parameters

Demo Engine
-----------
In demo mode no real orders are placed. The bot simulates fills at
the current best bid/ask and monitors for profit target / stop loss /
timeout, then logs the outcome to the journal just like a live trade.
"""
import asyncio
import logging
from datetime import datetime
from typing import List, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

import config
from kalshi_client import KalshiClient
from strategy import StrategyEngine, MarketSignal
from journal import TradeJournal

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ─── Global state ─────────────────────────────────────────────────────────────

class BotState:
    demo_mode:        bool  = True
    running:          bool  = False
    demo_balance:     float = config.START_BALANCE
    daily_trades:     int   = 0
    day_key:          str   = ""
    kalshi:  Optional[KalshiClient]    = None
    strategy: Optional[StrategyEngine] = None
    journal:  Optional[TradeJournal]   = None
    demo_positions: List["DemoPosition"] = []

S = BotState()


# ─── Demo Position ─────────────────────────────────────────────────────────────

class DemoPosition:
    def __init__(self, trade_id: int, ticker: str, title: str, action: str,
                 entry_price: int, contracts: int, cost: float):
        self.trade_id    = trade_id
        self.ticker      = ticker
        self.title       = title
        self.action      = action       # "buy_yes" | "buy_no"
        self.entry_price = entry_price  # cents we paid
        self.contracts   = contracts
        self.cost        = cost
        self.opened_at   = datetime.utcnow()

    def unrealised_pnl(self, current_exit_price: int) -> float:
        """
        current_exit_price — the price we could sell at right now (cents).
        For buy_yes: current YES bid.
        For buy_no:  current NO bid.
        """
        return (current_exit_price - self.entry_price) * self.contracts / 100

    def minutes_held(self) -> float:
        return (datetime.utcnow() - self.opened_at).total_seconds() / 60


def _current_sell_price(ticker: str, action: str) -> Optional[int]:
    """Return the best price we could exit at right now (cents)."""
    ob = S.kalshi.get_orderbook(ticker)
    if not ob:
        return None
    yes_bids = ob.get("yes", [])
    no_bids  = ob.get("no",  [])
    if action == "buy_yes":
        return yes_bids[0].get("price") if yes_bids else None
    else:
        return no_bids[0].get("price") if no_bids else None


# ─── Core trading helpers ─────────────────────────────────────────────────────

def _reset_daily_if_needed():
    today = datetime.utcnow().strftime("%Y-%m-%d")
    if today != S.day_key:
        S.daily_trades = 0
        S.day_key = today


async def _open_demo_trade(sig: MarketSignal, chat_id: int, bot):
    if S.demo_balance < sig.cost:
        return
    S.demo_balance -= sig.cost

    trade_id = S.journal.log_open(
        mode="demo", ticker=sig.ticker, title=sig.title, action=sig.action,
        entry_price=sig.entry_price, contracts=sig.contracts, cost=sig.cost,
        confidence=sig.confidence, momentum=sig.momentum_score,
        obi=sig.obi_score, volume_surge=sig.volume_surge,
        balance=S.demo_balance + sig.cost,
    )

    pos = DemoPosition(trade_id, sig.ticker, sig.title, sig.action,
                       sig.entry_price, sig.contracts, sig.cost)
    S.demo_positions.append(pos)
    S.daily_trades += 1

    reasons = "\n".join(sig.reasons)

    # News / fundamental block
    news_block = ""
    if sig.news_context:
        ctx = sig.news_context
        news_block = (
            f"\n\n🗞 *Fundamental Edge: {ctx.edge:+d}¢*\n"
            f"_{ctx.event_summary}_\n"
            f"True prob estimate: `{ctx.true_prob}¢` | "
            f"Market rationality: `{ctx.rationality}/100`"
        )

    await bot.send_message(
        chat_id=chat_id,
        parse_mode=ParseMode.MARKDOWN,
        text=(
            f"🎯 *Demo Trade Opened*\n"
            f"`{sig.ticker}`\n"
            f"Action: *{sig.action.replace('_',' ').upper()}*\n"
            f"Entry: `{sig.entry_price}¢` × {sig.contracts} contracts = `${sig.cost:.2f}`\n"
            f"Confidence: `{sig.confidence:.0f}%`\n\n"
            f"*Why:*\n{reasons}"
            f"{news_block}\n\n"
            f"Balance: `${S.demo_balance:.2f}`\n"
            f"🛑 Stop `${config.STOP_LOSS_PER_TRADE}` | "
            f"🎯 Target `${config.MIN_PROFIT_PER_TRADE}`"
        ),
    )


async def _monitor_demo_positions(chat_id: int, bot):
    """Check each open demo position for exit triggers."""
    to_close: List[DemoPosition] = []

    for pos in S.demo_positions:
        price = _current_sell_price(pos.ticker, pos.action)
        if price is None:
            continue

        pnl  = pos.unrealised_pnl(price)
        mins = pos.minutes_held()

        if pnl >= config.MIN_PROFIT_PER_TRADE:
            reason = "profit_target"
        elif pnl <= -config.STOP_LOSS_PER_TRADE:
            reason = "stop_loss"
        elif mins >= config.MAX_HOLD_MINUTES:
            reason = "timeout"
        else:
            continue

        # Close it
        S.demo_balance += pos.cost + pnl
        S.journal.log_close(
            pos.trade_id,
            exit_price=price,
            pnl=pnl,
            reason=reason,
            balance_after=S.demo_balance,
        )
        to_close.append(pos)

        icon = "✅" if pnl > 0 else "❌"
        await bot.send_message(
            chat_id=chat_id,
            parse_mode=ParseMode.MARKDOWN,
            text=(
                f"{icon} *Demo Position Closed*\n"
                f"`{pos.ticker}` | {pos.action.replace('_',' ').upper()}\n"
                f"Entry `{pos.entry_price}¢` → Exit `{price}¢`\n"
                f"P&L: `${pnl:+.2f}` — _{reason.replace('_',' ')}_\n"
                f"Balance: `${S.demo_balance:.2f}`"
            ),
        )

    S.demo_positions = [p for p in S.demo_positions if p not in to_close]


async def _open_live_trade(sig: MarketSignal, chat_id: int, bot):
    """Place a real order on Kalshi."""
    balance = S.kalshi.get_balance()
    trade_id = S.journal.log_open(
        mode="live", ticker=sig.ticker, title=sig.title, action=sig.action,
        entry_price=sig.entry_price, contracts=sig.contracts, cost=sig.cost,
        confidence=sig.confidence, momentum=sig.momentum_score,
        obi=sig.obi_score, volume_surge=sig.volume_surge,
        balance=balance,
    )

    side  = sig.action.split("_")[1]   # "yes" or "no"
    order = S.kalshi.place_order(
        ticker=sig.ticker, action="buy", side=side,
        count=sig.contracts, price=sig.entry_price,
    )

    S.daily_trades += 1
    icon = "💰" if order else "⚠️"
    oid  = (order or {}).get("order_id", "failed")
    await bot.send_message(
        chat_id=chat_id,
        parse_mode=ParseMode.MARKDOWN,
        text=(
            f"{icon} *Live Order {'Placed' if order else 'FAILED'}*\n"
            f"`{sig.ticker}` | {sig.action.replace('_',' ').upper()}\n"
            f"Entry: `{sig.entry_price}¢` × {sig.contracts}\n"
            f"Order ID: `{oid}`"
        ),
    )


# ─── Trading loop job ─────────────────────────────────────────────────────────

async def trading_loop(context: ContextTypes.DEFAULT_TYPE):
    if not S.running:
        return

    chat_id = context.job.chat_id
    bot     = context.bot

    _reset_daily_if_needed()

    if S.daily_trades >= config.MAX_TRADES_PER_DAY:
        logger.info("Daily trade limit reached.")
        return

    try:
        # ── Demo housekeeping ──────────────────────────────────────────────
        if S.demo_mode:
            await _monitor_demo_positions(chat_id, bot)

            if S.demo_balance >= config.TARGET_BALANCE:
                S.running = False
                pct = (S.demo_balance - config.START_BALANCE) / config.START_BALANCE * 100
                await bot.send_message(
                    chat_id=chat_id,
                    parse_mode=ParseMode.MARKDOWN,
                    text=(
                        f"🏆 *TARGET REACHED!*\n"
                        f"Balance: `${S.demo_balance:.2f}` "
                        f"(+{pct:.0f}% from ${config.START_BALANCE})\n"
                        f"Bot stopped. Use /reset then /run to go again."
                    ),
                )
                return

            balance = S.demo_balance
            if balance < 1.00:
                return
            if len(S.demo_positions) >= 2:   # max 2 concurrent demo trades
                return
        else:
            balance = S.kalshi.get_balance()

        # ── Scan & execute ─────────────────────────────────────────────────
        signals = S.strategy.scan_markets(balance=balance, max_results=2)

        for sig in signals:
            if S.demo_mode:
                await _open_demo_trade(sig, chat_id, bot)
            else:
                await _open_live_trade(sig, chat_id, bot)

        # ── AI self-correction every 5 closed trades ───────────────────────
        mode = "demo" if S.demo_mode else "live"
        if S.journal.should_self_correct(mode=mode):
            analysis = S.journal.ai_self_correct(mode=mode)
            await bot.send_message(
                chat_id=chat_id,
                parse_mode=ParseMode.MARKDOWN,
                text=f"🤖 *AI Strategy Review*\n\n{analysis}",
            )

    except Exception as e:
        logger.exception(f"Trading loop error: {e}")


# ─── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = "🧪 Demo" if S.demo_mode else "💰 Live"
    st   = "🟢 Running" if S.running else "🔴 Stopped"
    bal  = S.demo_balance if S.demo_mode else (S.kalshi.get_balance() if S.kalshi else 0)
    pct  = bal / config.TARGET_BALANCE * 100

    await update.message.reply_text(
        parse_mode=ParseMode.MARKDOWN,
        text=(
            f"🤖 *Kalshi Auto-Trading Bot*\n\n"
            f"Mode: {mode}   Status: {st}\n"
            f"Balance: `${bal:.2f}` → Target `${config.TARGET_BALANCE:.2f}` ({pct:.0f}%)\n\n"
            f"*Commands*\n"
            f"/run — start bot\n"
            f"/stop — stop bot\n"
            f"/status — balance & open positions\n"
            f"/journal — recent trade log\n"
            f"/analyze — AI strategy review\n"
            f"/demo — toggle demo / live\n"
            f"/reset — reset demo to ${config.START_BALANCE}\n"
            f"/settings — strategy parameters"
        ),
    )


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if S.running:
        await update.message.reply_text("✅ Already running.")
        return

    S.running = True
    chat_id   = update.effective_chat.id

    # Remove stale jobs
    for job in context.application.job_queue.get_jobs_by_name("trading_loop"):
        job.schedule_removal()

    context.application.job_queue.run_repeating(
        trading_loop,
        interval=config.SCAN_INTERVAL_SECONDS,
        first=5,
        chat_id=chat_id,
        name="trading_loop",
    )

    mode = "🧪 Demo" if S.demo_mode else "💰 Live"
    await update.message.reply_text(
        parse_mode=ParseMode.MARKDOWN,
        text=(
            f"🟢 *Bot Started* ({mode})\n"
            f"Scanning every {config.SCAN_INTERVAL_SECONDS}s\n"
            f"Goal: `${config.START_BALANCE}` → `${config.TARGET_BALANCE}` "
            f"| Min profit `${config.MIN_PROFIT_PER_TRADE}` | "
            f"Stop loss `${config.STOP_LOSS_PER_TRADE}`"
        ),
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    S.running = False
    for job in context.application.job_queue.get_jobs_by_name("trading_loop"):
        job.schedule_removal()
    await update.message.reply_text(
        "🔴 *Bot Stopped*\nUse /status for results.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode  = "demo" if S.demo_mode else "live"
    bal   = S.demo_balance if S.demo_mode else S.kalshi.get_balance()
    pnl   = bal - config.START_BALANCE
    pct   = bal / config.TARGET_BALANCE * 100
    s     = S.journal.stats(mode=mode)
    st    = "🟢 Running" if S.running else "🔴 Stopped"

    # Open positions summary
    pos_lines = ""
    if S.demo_mode and S.demo_positions:
        lines = []
        for pos in S.demo_positions:
            price = _current_sell_price(pos.ticker, pos.action)
            upnl  = pos.unrealised_pnl(price) if price else 0.0
            lines.append(
                f"• `{pos.ticker[:18]}` {pos.action} "
                f"unrealised `${upnl:+.2f}` "
                f"({pos.minutes_held():.0f}min)"
            )
        pos_lines = "\n\n*Open positions:*\n" + "\n".join(lines)

    await update.message.reply_text(
        parse_mode=ParseMode.MARKDOWN,
        text=(
            f"📊 *Status* ({st})\n\n"
            f"Mode: {'🧪 Demo' if S.demo_mode else '💰 Live'}\n"
            f"Balance: `${bal:.2f}` | P&L: `${pnl:+.2f}`\n"
            f"Progress: `{pct:.0f}%` to `${config.TARGET_BALANCE}`\n\n"
            f"Trades: {s['total_trades']} | Win rate: {s['win_rate']}%\n"
            f"Today: {S.daily_trades}/{config.MAX_TRADES_PER_DAY}"
            + pos_lines
        ),
    )


async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = "demo" if S.demo_mode else "live"
    await update.message.reply_text(
        S.journal.format_for_telegram(limit=8, mode=mode),
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 Analysing trade history…")
    mode     = "demo" if S.demo_mode else "live"
    analysis = S.journal.ai_self_correct(mode=mode)
    await update.message.reply_text(
        f"🤖 *AI Strategy Review*\n\n{analysis}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_demo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if S.running:
        await update.message.reply_text("⚠️ Use /stop first before switching modes.")
        return
    S.demo_mode = not S.demo_mode
    mode = "🧪 Demo" if S.demo_mode else "💰 Live"
    warn = ("\n\n⚠️ *LIVE MODE — real money!* "
            "Make sure your Kalshi API keys are configured in .env."
            if not S.demo_mode else "")
    await update.message.reply_text(
        f"Switched to *{mode}* mode.{warn}",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not S.demo_mode:
        await update.message.reply_text("Only available in demo mode.")
        return
    if S.running:
        await update.message.reply_text("Use /stop first.")
        return
    S.demo_balance   = config.START_BALANCE
    S.demo_positions = []
    S.daily_trades   = 0
    await update.message.reply_text(
        f"♻️ Demo reset — balance `${S.demo_balance:.2f}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        parse_mode=ParseMode.MARKDOWN,
        text=(
            f"⚙️ *Strategy Settings*\n\n"
            f"*Goals*\n"
            f"  Start balance  `${config.START_BALANCE}`\n"
            f"  Target balance `${config.TARGET_BALANCE}`\n"
            f"  Min profit     `${config.MIN_PROFIT_PER_TRADE}/trade`\n"
            f"  Stop loss      `${config.STOP_LOSS_PER_TRADE}/trade`\n\n"
            f"*Indicators*\n"
            f"  Momentum ≥     `{config.MIN_MOMENTUM_SCORE}/100`\n"
            f"  OBI ≥          `±{config.MIN_OBI_THRESHOLD}`\n"
            f"  Volume surge ≥ `{config.MIN_VOLUME_SURGE}×`\n"
            f"  Max spread     `{config.MAX_SPREAD_CENTS}¢`\n"
            f"  Expiry window  `{config.MIN_TIME_TO_EXPIRY_HOURS}–{config.MAX_TIME_TO_EXPIRY_HOURS}h`\n\n"
            f"*Risk*\n"
            f"  Position size  `{int(config.POSITION_SIZE_PCT*100)}%` of balance\n"
            f"  Max hold time  `{config.MAX_HOLD_MINUTES} min`\n"
            f"  Max trades/day `{config.MAX_TRADES_PER_DAY}`\n"
            f"  Scan interval  `{config.SCAN_INTERVAL_SECONDS}s`\n\n"
            f"Edit any value in your `.env` file and restart."
        ),
    )


# ─── Security middleware ───────────────────────────────────────────────────────

async def restrict_to_owner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if config.ALLOWED_USER_ID and update.effective_user.id != config.ALLOWED_USER_ID:
        await update.message.reply_text("⛔ Unauthorised.")
        return False
    return True


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    if not config.TELEGRAM_TOKEN:
        raise SystemExit("TELEGRAM_TOKEN not set in .env")

    # Init shared clients
    S.kalshi   = KalshiClient()
    S.strategy = StrategyEngine(S.kalshi)
    S.journal  = TradeJournal()
    S.day_key  = datetime.utcnow().strftime("%Y-%m-%d")

    app = Application.builder().token(config.TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("run",      cmd_run))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("journal",  cmd_journal))
    app.add_handler(CommandHandler("analyze",  cmd_analyze))
    app.add_handler(CommandHandler("demo",     cmd_demo))
    app.add_handler(CommandHandler("reset",    cmd_reset))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("news",     cmd_news))

    logger.info("Kalshi bot polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()


# ─── /news command (added by news-awareness update) ───────────────────────────

async def cmd_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /news TICKER — manually pull fundamental analysis for any Kalshi market.
    Example: /news INXD-23-B4950
    """
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: `/news TICKER`\nExample: `/news FED-25-RATE`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    ticker = args[0].upper()
    await update.message.reply_text(f"🔍 Analysing `{ticker}` — searching news…",
                                    parse_mode=ParseMode.MARKDOWN)

    market = S.kalshi.get_market(ticker)
    if not market:
        await update.message.reply_text(f"⚠️ Market `{ticker}` not found.", parse_mode=ParseMode.MARKDOWN)
        return

    ob = S.kalshi.get_orderbook(ticker)
    price = 50
    if ob:
        yes_bids = ob.get("yes", [])
        no_bids  = ob.get("no",  [])
        bid = yes_bids[0].get("price", 0) if yes_bids else 0
        ask = (100 - no_bids[0].get("price", 100)) if no_bids else 99
        price = (bid + ask) // 2

    ctx = S.strategy.news.analyze(ticker, market.get("title", ticker), price)
    if not ctx:
        await update.message.reply_text("⚠️ News analysis unavailable (check ANTHROPIC_API_KEY).")
        return

    await update.message.reply_text(
        S.strategy.news.format_for_telegram(ctx),
        parse_mode=ParseMode.MARKDOWN,
    )
