"""
journal.py — SQLite trade journal + AI self-correction via Claude API.

Every trade (open and close) is persisted to trades.db.
After every 5 closed trades the journal automatically asks Claude to
analyse the patterns and recommend parameter adjustments.
"""
import sqlite3
import logging
from datetime import datetime
from typing import Dict, List, Optional

import config

logger = logging.getLogger(__name__)


class TradeJournal:
    def __init__(self, db_path: str = "trades.db"):
        self.db_path = db_path
        self._init_db()

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_db(self):
        with sqlite3.connect(self.db_path) as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS trades (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp           TEXT    NOT NULL,
                    mode                TEXT    NOT NULL,   -- 'demo' | 'live'
                    ticker              TEXT    NOT NULL,
                    title               TEXT,
                    action              TEXT    NOT NULL,   -- 'buy_yes' | 'buy_no'
                    entry_price_cents   INTEGER,
                    exit_price_cents    INTEGER,
                    contracts           INTEGER,
                    cost_usd            REAL,
                    pnl_usd             REAL,
                    exit_reason         TEXT,
                    confidence          REAL,
                    momentum_score      REAL,
                    obi_score           REAL,
                    volume_surge        REAL,
                    balance_before      REAL,
                    balance_after       REAL,
                    status              TEXT    DEFAULT 'open',  -- 'open' | 'closed'
                    order_id            TEXT
                );

                CREATE TABLE IF NOT EXISTS ai_corrections (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp           TEXT    NOT NULL,
                    analysis            TEXT    NOT NULL,
                    trade_count         INTEGER
                );
            """)

    # ── Write ─────────────────────────────────────────────────────────────────

    def log_open(self, *, mode: str, ticker: str, title: str, action: str,
                 entry_price: int, contracts: int, cost: float, confidence: float,
                 momentum: float, obi: float, volume_surge: float,
                 balance: float, order_id: str = None) -> int:
        """Insert a new open trade. Returns its row id."""
        with sqlite3.connect(self.db_path) as c:
            cur = c.execute("""
                INSERT INTO trades
                    (timestamp, mode, ticker, title, action, entry_price_cents,
                     contracts, cost_usd, confidence, momentum_score, obi_score,
                     volume_surge, balance_before, status, order_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?)
            """, (
                datetime.utcnow().isoformat(), mode, ticker, title, action,
                entry_price, contracts, cost, confidence, momentum, obi,
                volume_surge, balance, order_id,
            ))
            return cur.lastrowid

    def log_close(self, trade_id: int, *, exit_price: int, pnl: float,
                  reason: str, balance_after: float):
        """Mark a trade as closed with its outcome."""
        with sqlite3.connect(self.db_path) as c:
            c.execute("""
                UPDATE trades
                SET exit_price_cents=?, pnl_usd=?, exit_reason=?,
                    balance_after=?, status='closed'
                WHERE id=?
            """, (exit_price, pnl, reason, balance_after, trade_id))

    # ── Read ──────────────────────────────────────────────────────────────────

    def recent_trades(self, limit: int = 10, mode: str = None) -> List[Dict]:
        with sqlite3.connect(self.db_path) as c:
            c.row_factory = sqlite3.Row
            sql  = "SELECT * FROM trades"
            args: list = []
            if mode:
                sql  += " WHERE mode=?"
                args  = [mode]
            sql += " ORDER BY timestamp DESC LIMIT ?"
            args.append(limit)
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def stats(self, mode: str = None) -> Dict:
        where = "status='closed'" + (f" AND mode='{mode}'" if mode else "")
        with sqlite3.connect(self.db_path) as c:
            row = c.execute(f"""
                SELECT
                    COUNT(*)                                     AS total,
                    SUM(CASE WHEN pnl_usd>0 THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN pnl_usd<=0 THEN 1 ELSE 0 END) AS losses,
                    ROUND(SUM(pnl_usd),4)                       AS total_pnl,
                    ROUND(AVG(pnl_usd),4)                       AS avg_pnl,
                    ROUND(MAX(pnl_usd),4)                       AS best,
                    ROUND(MIN(pnl_usd),4)                       AS worst
                FROM trades WHERE {where}
            """).fetchone()
        total = row[0] or 0
        wins  = row[1] or 0
        return {
            "total_trades": total,
            "wins":         wins,
            "losses":       row[2] or 0,
            "total_pnl":    row[3] or 0.0,
            "avg_pnl":      row[4] or 0.0,
            "best_trade":   row[5] or 0.0,
            "worst_trade":  row[6] or 0.0,
            "win_rate":     round(wins / max(total, 1) * 100, 1),
        }

    def closed_count(self, mode: str = None) -> int:
        where = "status='closed'" + (f" AND mode='{mode}'" if mode else "")
        with sqlite3.connect(self.db_path) as c:
            return c.execute(f"SELECT COUNT(*) FROM trades WHERE {where}").fetchone()[0]

    def last_correction_at(self) -> int:
        with sqlite3.connect(self.db_path) as c:
            row = c.execute(
                "SELECT trade_count FROM ai_corrections ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else 0

    def should_self_correct(self, mode: str = None) -> bool:
        """True every 5 closed trades."""
        n = self.closed_count(mode)
        return n > 0 and n >= self.last_correction_at() + 5

    # ── AI Self-Correction ────────────────────────────────────────────────────

    def ai_self_correct(self, mode: str = None) -> str:
        """
        Send recent trade data to Claude and get strategy adjustment recommendations.
        Persists the analysis and returns it as a formatted string.
        """
        try:
            import anthropic
        except ImportError:
            return "⚠️ anthropic package not installed."

        if not config.ANTHROPIC_API_KEY:
            return "⚠️ ANTHROPIC_API_KEY not set — AI analysis skipped."

        trades = self.recent_trades(limit=20, mode=mode)
        s      = self.stats(mode=mode)

        if not trades:
            return "No closed trades yet to analyse."

        rows = []
        for t in trades:
            win = "WIN ✅" if (t.get("pnl_usd") or 0) > 0 else "LOSS ❌"
            rows.append(
                f"• {t['ticker']} | {t['action']} | "
                f"Entry {t.get('entry_price_cents','?')}¢ "
                f"→ Exit {t.get('exit_price_cents','?') or '?'}¢ | "
                f"P&L ${t.get('pnl_usd',0):.2f} {win} | "
                f"Exit: {t.get('exit_reason','?')} | "
                f"Mom {t.get('momentum_score',0):.0f} | "
                f"OBI {t.get('obi_score',0):.2f} | "
                f"VolSurge {t.get('volume_surge',0):.1f}×"
            )

        prompt = f"""You are a quantitative trading coach reviewing a Kalshi prediction-market bot.

=== PERFORMANCE ===
Total closed trades : {s['total_trades']}
Win rate            : {s['win_rate']}%
Total P&L           : ${s['total_pnl']:.2f}
Avg P&L / trade     : ${s['avg_pnl']:.2f}
Best trade          : ${s['best_trade']:.2f}
Worst trade         : ${s['worst_trade']:.2f}

=== RECENT TRADES (newest first) ===
{chr(10).join(rows)}

=== CURRENT STRATEGY PARAMETERS ===
Min momentum score    : {config.MIN_MOMENTUM_SCORE}/100
Min OBI threshold     : {config.MIN_OBI_THRESHOLD}
Min volume surge      : {config.MIN_VOLUME_SURGE}×
Max spread            : {config.MAX_SPREAD_CENTS}¢
Stop loss             : ${config.STOP_LOSS_PER_TRADE}
Profit target/trade   : ${config.MIN_PROFIT_PER_TRADE}
Max hold time         : {config.MAX_HOLD_MINUTES} min
Position size         : {int(config.POSITION_SIZE_PCT*100)}% of balance

=== GOAL ===
Turn $5 → $25 in one week. Min $0.20 profit per winning trade.

Analyse what's working and what's failing. Give exactly 4 numbered, specific, \
actionable recommendations to improve the strategy. Be concise."""

        try:
            client   = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=700,
                messages=[{"role": "user", "content": prompt}],
            )
            analysis = response.content[0].text.strip()

            # Persist analysis
            n = self.closed_count(mode)
            with sqlite3.connect(self.db_path) as c:
                c.execute(
                    "INSERT INTO ai_corrections (timestamp, analysis, trade_count) VALUES (?,?,?)",
                    (datetime.utcnow().isoformat(), analysis, n),
                )
            return analysis

        except Exception as e:
            logger.error(f"AI analysis error: {e}")
            return f"⚠️ AI analysis failed: {e}"

    # ── Formatting ────────────────────────────────────────────────────────────

    def format_for_telegram(self, limit: int = 8, mode: str = None) -> str:
        trades = self.recent_trades(limit=limit, mode=mode)
        s      = self.stats(mode=mode)

        label = "Demo" if mode == "demo" else "Live" if mode == "live" else "All"
        lines = [f"📔 *Trade Journal* ({label})\n"]

        for t in trades:
            pnl  = t.get("pnl_usd") or 0
            icon = "✅" if pnl > 0 else ("❌" if pnl < 0 else "⏳")
            res  = "OPEN" if t.get("status") == "open" else f"${pnl:+.2f}"
            ep   = t.get("entry_price_cents", "?")
            xp   = t.get("exit_price_cents") or "?"
            lines.append(
                f"{icon} `{t['ticker'][:18]}` | {t['action']} | "
                f"{ep}¢→{xp}¢ | {res}"
            )

        lines.append(
            f"\n📊 *{s['total_trades']} trades* | "
            f"Win {s['win_rate']}% | P&L ${s['total_pnl']:+.2f}"
        )
        if not trades:
            return f"📔 Journal ({label}): no trades yet."
        return "\n".join(lines)
