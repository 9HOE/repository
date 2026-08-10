"""
config.py — Loads all settings from .env
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ─── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_USER_ID   = int(os.getenv("ALLOWED_USER_ID", "0"))

# ─── Kalshi API ───────────────────────────────────────────────────────────────
KALSHI_API_BASE          = "https://api.kalshi.com/trade-api/v2"
KALSHI_API_KEY_ID        = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH  = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi_key.pem")

# ─── Anthropic ────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ─── Goals ────────────────────────────────────────────────────────────────────
START_BALANCE         = float(os.getenv("START_BALANCE",         "5.00"))
TARGET_BALANCE        = float(os.getenv("TARGET_BALANCE",        "25.00"))
MIN_PROFIT_PER_TRADE  = float(os.getenv("MIN_PROFIT_PER_TRADE",  "0.20"))
STOP_LOSS_PER_TRADE   = float(os.getenv("STOP_LOSS_PER_TRADE",   "0.15"))

# ─── Risk ─────────────────────────────────────────────────────────────────────
MAX_TRADES_PER_DAY     = int(os.getenv("MAX_TRADES_PER_DAY",     "15"))
POSITION_SIZE_PCT      = float(os.getenv("POSITION_SIZE_PCT",    "0.40"))
MAX_HOLD_MINUTES       = int(os.getenv("MAX_HOLD_MINUTES",       "30"))
SCAN_INTERVAL_SECONDS  = int(os.getenv("SCAN_INTERVAL_SECONDS",  "60"))

# ─── Strategy Indicators ──────────────────────────────────────────────────────
MIN_MOMENTUM_SCORE         = float(os.getenv("MIN_MOMENTUM_SCORE",         "60"))
MIN_OBI_THRESHOLD          = float(os.getenv("MIN_OBI_THRESHOLD",          "0.15"))
MIN_VOLUME_SURGE           = float(os.getenv("MIN_VOLUME_SURGE",           "1.5"))
MAX_SPREAD_CENTS           = int(os.getenv("MAX_SPREAD_CENTS",             "6"))
MIN_TIME_TO_EXPIRY_HOURS   = float(os.getenv("MIN_TIME_TO_EXPIRY_HOURS",  "0.25"))  # 15 min
MAX_TIME_TO_EXPIRY_HOURS   = float(os.getenv("MAX_TIME_TO_EXPIRY_HOURS",  "1.0"))   # 60 min

# Crypto assets the bot is allowed to trade (all others are ignored)
ALLOWED_ASSETS = ["btc", "bitcoin", "eth", "ethereum", "ltc", "litecoin"]
