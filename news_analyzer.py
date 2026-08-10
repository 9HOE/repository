"""
news_analyzer.py — Fundamental awareness layer for the Kalshi bot.

Before placing any trade the bot now asks Claude (with live web search):
  1. What exactly is this market predicting?
  2. What does recent news/data say about this event?
  3. What is the TRUE probability — and how far is the current price from it?
  4. Is the market being rational, or is there an exploitable edge?

Results are cached per ticker for CACHE_MINUTES to avoid hammering the API.
"""
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import anthropic
import config

logger = logging.getLogger(__name__)

CACHE_MINUTES = 20   # Re-analyse the same ticker at most every 20 min


@dataclass
class MarketContext:
    # What is being predicted
    event_summary: str

    # Claude's estimate of the true probability (0–100)
    true_prob: int

    # true_prob minus current market price (cents).
    # Positive  → market UNDERPRICED → edge for BUY YES
    # Negative  → market OVERPRICED  → edge for BUY NO
    edge: int

    # How rational is the current price? (0–100, 100 = fully rational)
    rationality: int

    # Overall news sentiment
    sentiment: str          # "bullish" | "bearish" | "neutral" | "unclear"

    # Key facts / headlines that drove the assessment
    key_factors: List[str] = field(default_factory=list)

    # How confident is Claude in this analysis? (0–100)
    confidence: int = 50

    # Final recommendation from fundamental view alone
    recommendation: str = "skip"  # "buy_yes" | "buy_no" | "skip"

    # Whether this result came from cache
    cached: bool = False


class NewsAnalyzer:
    def __init__(self):
        if not config.ANTHROPIC_API_KEY:
            self._client = None
            logger.warning("ANTHROPIC_API_KEY not set — news analysis disabled.")
        else:
            self._client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        # ticker → (cached_at: datetime, context: MarketContext)
        self._cache: Dict[str, Tuple[datetime, MarketContext]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def analyze(self, ticker: str, title: str, current_price: int) -> Optional[MarketContext]:
        """
        Returns a MarketContext for the given market, or None if analysis fails.
        current_price is in cents (1–99).
        """
        if not self._client:
            return None

        # Serve from cache if fresh enough
        cached = self._cache.get(ticker)
        if cached:
            cached_at, ctx = cached
            if datetime.utcnow() - cached_at < timedelta(minutes=CACHE_MINUTES):
                ctx.cached = True
                return ctx

        ctx = self._fetch(ticker, title, current_price)
        if ctx:
            self._cache[ticker] = (datetime.utcnow(), ctx)
        return ctx

    def format_for_telegram(self, ctx: MarketContext) -> str:
        """One-block Telegram summary of the fundamental analysis."""
        icon = {"bullish": "📈", "bearish": "📉", "neutral": "➡️"}.get(ctx.sentiment, "❓")
        edge_icon = "🟢" if ctx.edge > 0 else ("🔴" if ctx.edge < 0 else "⚪")
        factors = "\n".join(f"  • {f}" for f in ctx.key_factors[:3])
        cached_note = " _(cached)_" if ctx.cached else ""

        return (
            f"🗞 *Fundamental Analysis*{cached_note}\n"
            f"{icon} _{ctx.event_summary}_\n\n"
            f"True prob estimate : `{ctx.true_prob}¢`\n"
            f"{edge_icon} Edge vs market    : `{ctx.edge:+d}¢`\n"
            f"Market rationality : `{ctx.rationality}/100`\n"
            f"Claude confidence  : `{ctx.confidence}%`\n\n"
            f"*Key factors:*\n{factors}"
        )

    # ── Internal ──────────────────────────────────────────────────────────────

    def _fetch(self, ticker: str, title: str, current_price: int) -> Optional[MarketContext]:
        prompt = f"""You are a prediction-market analyst. A trading bot needs to decide
whether to trade the following Kalshi market.

MARKET TICKER : {ticker}
MARKET TITLE  : {title}
CURRENT PRICE : {current_price}¢  (implies the market gives a {current_price}% probability)

YOUR TASKS:
1. Use web search to find recent news, data, polls, or expert opinion relevant to this event.
2. Based purely on real-world evidence, estimate the TRUE probability this event occurs.
3. Assess whether the current Kalshi price is rational or whether there is a pricing edge.

Respond with ONLY a raw JSON object — no markdown fences, no extra text:
{{
  "event_summary": "<one sentence: what is being predicted>",
  "true_prob_estimate": <integer 0-100>,
  "rationality_score": <integer 0-100, 100=fully rational>,
  "sentiment": "<bullish|bearish|neutral|unclear>",
  "key_factors": ["<fact 1>", "<fact 2>", "<fact 3>"],
  "confidence": <integer 0-100>,
  "recommendation": "<buy_yes|buy_no|skip>"
}}

recommendation rules:
  buy_yes  → if true_prob_estimate > current_price + 8  (market significantly underpriced)
  buy_no   → if true_prob_estimate < current_price - 8  (market significantly overpriced)
  skip     → otherwise (fairly priced or too uncertain)"""

        raw = self._call_claude(prompt)
        if not raw:
            return None

        return self._parse(raw, current_price)

    def _call_claude(self, prompt: str) -> Optional[str]:
        """Call Claude with web search enabled. Returns the final text block."""
        messages = [{"role": "user", "content": prompt}]

        try:
            for _ in range(6):   # allow up to 6 turns for tool use
                resp = self._client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=900,
                    tools=[{"type": "web_search_20250305", "name": "web_search"}],
                    messages=messages,
                )

                if resp.stop_reason == "end_turn":
                    # Collect all text blocks
                    return "".join(
                        b.text for b in resp.content if hasattr(b, "text")
                    ).strip()

                if resp.stop_reason == "tool_use":
                    # Let Claude continue — add its response and empty tool results
                    messages.append({"role": "assistant", "content": resp.content})
                    tool_results = [
                        {
                            "type": "tool_result",
                            "tool_use_id": b.id,
                            "content": "",
                        }
                        for b in resp.content
                        if hasattr(b, "type") and b.type == "tool_use"
                    ]
                    if tool_results:
                        messages.append({"role": "user", "content": tool_results})
                    continue

                break  # unexpected stop_reason

        except Exception as e:
            logger.error(f"Claude API call failed: {e}")

        return None

    def _parse(self, raw: str, current_price: int) -> Optional[MarketContext]:
        """Parse Claude's JSON response into a MarketContext."""
        # Strip any accidental markdown fences
        text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract the first {...} block
            start = text.find("{")
            end   = text.rfind("}") + 1
            if start != -1 and end > start:
                try:
                    data = json.loads(text[start:end])
                except json.JSONDecodeError:
                    logger.warning(f"Could not parse Claude JSON: {text[:200]}")
                    return None
            else:
                logger.warning(f"No JSON block found in response: {text[:200]}")
                return None

        true_prob = int(data.get("true_prob_estimate", current_price))
        edge      = true_prob - current_price

        return MarketContext(
            event_summary  = str(data.get("event_summary", "Unknown event")),
            true_prob      = true_prob,
            edge           = edge,
            rationality    = int(data.get("rationality_score", 50)),
            sentiment      = str(data.get("sentiment", "unclear")),
            key_factors    = [str(f) for f in data.get("key_factors", [])],
            confidence     = int(data.get("confidence", 50)),
            recommendation = str(data.get("recommendation", "skip")),
        )
