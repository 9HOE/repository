"""
strategy.py — Trading strategy engine for Kalshi prediction markets.

Indicators (technical)
----------------------
• Momentum Score  (0–100)  — price trend over last N ticks
• Order Book Imbalance     — buying vs selling pressure (-1 to +1)
• Volume Surge             — current volume vs rolling average
• Spread Filter            — only trade liquid markets

Fundamental Layer (new)
-----------------------
• NewsAnalyzer queries Claude + web search to estimate the TRUE probability
  of the event and computes an "edge" vs the current market price.
• Trades are only placed when BOTH technical AND fundamental signals agree.
• If news contradicts technicals, the trade is skipped.

Signal Confidence
-----------------
  base_confidence  = technical signals (momentum, OBI, volume)
  news_boost       = +0 to +25 points when news strongly confirms direction
  final_confidence = base_confidence + news_boost
  Minimum threshold to trade: 55 combined
"""
import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import config
from news_analyzer import NewsAnalyzer, MarketContext

logger = logging.getLogger(__name__)


@dataclass
class MarketSignal:
    ticker: str
    title: str
    action: str              # "buy_yes" | "buy_no"
    confidence: float        # 0–100 combined score
    entry_price: int         # cents per contract
    contracts: int
    cost: float              # dollars
    reasons: List[str]

    # Raw technical indicators
    momentum_score: float
    obi_score: float
    volume_surge: float
    spread_cents: int

    # Fundamental context (None if API unavailable)
    news_context: Optional[MarketContext] = None
    fundamental_edge: int = 0   # cents of pricing edge found


class StrategyEngine:
    def __init__(self, kalshi_client):
        self.client  = kalshi_client
        self.news    = NewsAnalyzer()
        self._prices:  Dict[str, List[int]] = {}
        self._volumes: Dict[str, List[int]] = {}

    # ── Technical Indicators ──────────────────────────────────────────────────

    def _momentum(self, prices: List[int]) -> float:
        """Score 0–100. >60 bullish, <40 bearish."""
        if len(prices) < 3:
            return 50.0
        roc    = (prices[-1] - prices[0]) / max(prices[0], 1) * 100
        ups    = sum(1 for i in range(1, len(prices)) if prices[i] > prices[i - 1])
        purity = ups / (len(prices) - 1) * 100
        raw    = (purity * 0.6) + (min(abs(roc), 50) / 50 * 100 * 0.4)
        if prices[-1] < prices[0]:
            raw = 100 - raw
        return max(0.0, min(100.0, raw))

    def _obi(self, orderbook: Dict) -> float:
        """Order Book Imbalance –1 to +1."""
        yes_vol = sum(e.get("quantity", 0) for e in orderbook.get("yes", [])[:5])
        no_vol  = sum(e.get("quantity", 0) for e in orderbook.get("no",  [])[:5])
        total   = yes_vol + no_vol
        return (yes_vol - no_vol) / total if total else 0.0

    def _volume_surge(self, ticker: str, current: int) -> float:
        hist = self._volumes.get(ticker, [])
        if len(hist) < 3:
            return 1.0
        avg = statistics.mean(hist[-10:])
        return current / avg if avg > 0 else 1.0

    def _spread_and_prices(self, ob: Dict) -> Tuple[int, int, int]:
        yes_bids = ob.get("yes", [])
        no_bids  = ob.get("no",  [])
        bid = yes_bids[0].get("price", 0)          if yes_bids else 0
        ask = 100 - no_bids[0].get("price", 100)   if no_bids  else 99
        return max(0, ask - bid), bid, ask

    # ── Position Sizing ───────────────────────────────────────────────────────

    def _size_position(self, entry_cents: int, balance: float, edge: int = 0) -> Tuple[int, float]:
        """
        Contracts needed for MIN_PROFIT_PER_TRADE.
        When a fundamental edge is detected we can size slightly larger
        because we have a second reason to be confident.
        """
        expected_move = max(10, abs(edge)) if edge != 0 else 10
        min_c = int(config.MIN_PROFIT_PER_TRADE * 100 / expected_move) + 1

        # Slightly larger position when edge > 15 cents (strong fundamental signal)
        size_pct = config.POSITION_SIZE_PCT * (1.2 if abs(edge) > 15 else 1.0)
        max_c    = int(balance * size_pct * 100 / max(entry_cents, 1))

        contracts = min(min_c, max(max_c, 0))
        return contracts, contracts * entry_cents / 100

    # ── Expiry Filter ─────────────────────────────────────────────────────────

    def _is_crypto_market(self, market: Dict) -> bool:
        """
        Returns True only if the market is for BTC, ETH, or LTC.
        Checks both the ticker and the title for any allowed keyword.
        """
        text = (
            market.get("ticker", "").lower() + " " +
            market.get("title",  "").lower() + " " +
            market.get("subtitle", "").lower()
        )
        return any(kw in text for kw in config.ALLOWED_ASSETS)

    def _expiry_ok(self, market: Dict) -> bool:
        """Market must expire in 15–60 minutes from now."""
        close_str = market.get("close_time", "")
        if not close_str:
            return False
        try:
            expiry     = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            hours_left = (expiry - datetime.now(timezone.utc)).total_seconds() / 3600
            return config.MIN_TIME_TO_EXPIRY_HOURS <= hours_left <= config.MAX_TIME_TO_EXPIRY_HOURS
        except Exception:
            return False

    def _update_history(self, ticker: str, price: int, volume: int):
        self._prices.setdefault(ticker, []).append(price)
        self._volumes.setdefault(ticker, []).append(volume)
        self._prices[ticker]  = self._prices[ticker][-20:]
        self._volumes[ticker] = self._volumes[ticker][-20:]

    # ── Core Analysis ─────────────────────────────────────────────────────────

    def analyze_market(self, market: Dict, balance: float) -> Optional[MarketSignal]:
        """
        Full two-layer analysis.
        Layer 1: technical indicators (fast, no API)
        Layer 2: news / fundamental (Claude + web search, cached 20 min)
        """
        ticker = market.get("ticker", "")
        title  = market.get("title", ticker)

        # Gate 0: BTC, ETH, LTC only — ignore everything else
        if not self._is_crypto_market(market):
            return None

        # Gate 1: must expire in 15–60 minutes
        if not self._expiry_ok(market):
            return None

        ob = self.client.get_orderbook(ticker)
        if not ob:
            return None

        spread, best_bid, best_ask = self._spread_and_prices(ob)
        if best_bid <= 0 or best_ask >= 100:
            return None

        mid = (best_bid + best_ask) // 2
        trades = self.client.get_trades(ticker, limit=20)
        vol    = sum(t.get("count", 0) for t in trades)

        self._update_history(ticker, mid, vol)

        prices  = self._prices.get(ticker, [mid])
        mom     = self._momentum(prices)
        obi     = self._obi(ob)
        vol_srg = self._volume_surge(ticker, vol)

        if spread > config.MAX_SPREAD_CENTS or vol < 5:
            return None

        # ── Layer 1: Technical Signal ─────────────────────────────────────────
        action     = "hold"
        base_conf  = 0.0
        reasons: List[str] = []

        if (mom > config.MIN_MOMENTUM_SCORE
                and obi > config.MIN_OBI_THRESHOLD
                and vol_srg > config.MIN_VOLUME_SURGE):
            action    = "buy_yes"
            base_conf = min(100, (mom - 50) * 1.5 + obi * 30 + vol_srg * 10)
            reasons   = [
                f"📈 Momentum {mom:.0f}/100 — bullish trend",
                f"📊 OBI +{obi:.2f} — buyers dominating",
                f"🔥 Volume surge {vol_srg:.1f}× normal",
            ]

        elif (mom < (100 - config.MIN_MOMENTUM_SCORE)
              and obi < -config.MIN_OBI_THRESHOLD
              and vol_srg > config.MIN_VOLUME_SURGE):
            action    = "buy_no"
            base_conf = min(100, (50 - mom) * 1.5 + abs(obi) * 30 + vol_srg * 10)
            reasons   = [
                f"📉 Momentum {mom:.0f}/100 — bearish trend",
                f"📊 OBI {obi:.2f} — sellers dominating",
                f"🔥 Volume surge {vol_srg:.1f}× normal",
            ]

        if action == "hold" or base_conf < 50:
            return None

        # ── Layer 2: Fundamental / News Check ────────────────────────────────
        entry_price = best_ask if action == "buy_yes" else (100 - best_bid)
        news_ctx    = self.news.analyze(ticker, title, mid)
        news_boost  = 0
        edge        = 0

        if news_ctx:
            edge = news_ctx.edge

            fundamental_agrees = (
                (action == "buy_yes" and news_ctx.recommendation == "buy_yes") or
                (action == "buy_no"  and news_ctx.recommendation == "buy_no")
            )
            fundamental_contradicts = (
                (action == "buy_yes" and news_ctx.recommendation == "buy_no") or
                (action == "buy_no"  and news_ctx.recommendation == "buy_yes")
            )

            if fundamental_contradicts:
                logger.info(f"Skip {ticker}: news contradicts technical signal.")
                return None

            if fundamental_agrees:
                # Boost confidence by up to 25 points based on edge & Claude confidence
                news_boost = int(min(25, abs(edge) * 0.8 + news_ctx.confidence * 0.1))
                reasons.append(
                    f"🗞 News confirms: edge {edge:+d}¢, "
                    f"rationality {news_ctx.rationality}/100"
                )
                reasons.append(f"📌 {news_ctx.event_summary}")
            # else: news is neutral / skip — proceed on technical alone

        final_conf = min(100, base_conf + news_boost)
        if final_conf < 55:
            return None

        contracts, cost = self._size_position(entry_price, balance, edge)
        if contracts <= 0 or cost > balance or cost <= 0:
            return None

        return MarketSignal(
            ticker=ticker, title=title, action=action,
            confidence=final_conf, entry_price=entry_price,
            contracts=contracts, cost=cost, reasons=reasons,
            momentum_score=mom, obi_score=obi, volume_surge=vol_srg,
            spread_cents=spread, news_context=news_ctx, fundamental_edge=edge,
        )

    # ── Scanner ───────────────────────────────────────────────────────────────

    def scan_markets(self, balance: float, max_results: int = 2) -> List[MarketSignal]:
        markets = self.client.get_markets(status="active", limit=100)
        signals: List[MarketSignal] = []

        for mkt in markets:
            try:
                sig = self.analyze_market(mkt, balance)
                if sig:
                    signals.append(sig)
            except Exception as e:
                logger.debug(f"Skip {mkt.get('ticker')}: {e}")

        signals.sort(key=lambda s: s.confidence, reverse=True)
        logger.info(f"Scan complete: {len(markets)} markets → {len(signals)} signals")
        return signals[:max_results]
