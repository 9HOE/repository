"""
kalshi_client.py — Authenticated Kalshi REST API wrapper.

Handles RSA-PSS request signing, market data, orders, and portfolio endpoints.
"""
import time
import base64
import logging
import requests
from typing import Dict, List, Optional, Tuple

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

import config

logger = logging.getLogger(__name__)


class KalshiClient:
    def __init__(self):
        self.base_url = config.KALSHI_API_BASE
        self.api_key_id = config.KALSHI_API_KEY_ID
        self.private_key = self._load_private_key()
        self.session = requests.Session()

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _load_private_key(self):
        """Load RSA private key for request signing."""
        if not config.KALSHI_API_KEY_ID:
            return None
        try:
            with open(config.KALSHI_PRIVATE_KEY_PATH, "rb") as f:
                return serialization.load_pem_private_key(
                    f.read(), password=None, backend=default_backend()
                )
        except Exception as e:
            logger.warning(f"Private key not loaded: {e}")
            return None

    def _sign(self, method: str, path: str) -> Tuple[str, str]:
        """
        Returns (timestamp_ms, base64_signature).
        Signature = RSA-PSS-SHA256( timestamp + METHOD + /path )
        """
        ts = str(int(time.time() * 1000))
        if self.private_key:
            msg = f"{ts}{method.upper()}{path}".encode()
            sig = self.private_key.sign(
                msg,
                padding.PSS(
                    mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH,
                ),
                hashes.SHA256(),
            )
            return ts, base64.b64encode(sig).decode()
        return ts, ""

    def _headers(self, method: str, path: str) -> Dict:
        ts, sig = self._sign(method, path)
        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get(self, path: str, params: Dict = None) -> Optional[Dict]:
        try:
            r = self.session.get(
                f"{self.base_url}{path}",
                headers=self._headers("GET", path),
                params=params,
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"GET {path}: {e}")
            return None

    def _post(self, path: str, body: Dict) -> Optional[Dict]:
        try:
            r = self.session.post(
                f"{self.base_url}{path}",
                headers=self._headers("POST", path),
                json=body,
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"POST {path}: {e}")
            return None

    def _delete(self, path: str) -> Optional[Dict]:
        try:
            r = self.session.delete(
                f"{self.base_url}{path}",
                headers=self._headers("DELETE", path),
                timeout=10,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.error(f"DELETE {path}: {e}")
            return None

    # ── Market Data ───────────────────────────────────────────────────────────

    def get_markets(self, status: str = "active", limit: int = 100) -> List[Dict]:
        """List markets by status."""
        data = self._get("/markets", {"status": status, "limit": limit})
        return (data or {}).get("markets", [])

    def get_market(self, ticker: str) -> Optional[Dict]:
        """Single market details."""
        data = self._get(f"/markets/{ticker}")
        return (data or {}).get("market")

    def get_orderbook(self, ticker: str, depth: int = 10) -> Optional[Dict]:
        """Order book for a market (yes/no bid lists)."""
        data = self._get(f"/markets/{ticker}/orderbook", {"depth": depth})
        return (data or {}).get("orderbook")

    def get_trades(self, ticker: str, limit: int = 50) -> List[Dict]:
        """Recent trade history for a market."""
        data = self._get(f"/markets/{ticker}/trades", {"limit": limit})
        return (data or {}).get("trades", [])

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_order(
        self,
        ticker: str,
        action: str,   # "buy" | "sell"
        side: str,     # "yes" | "no"
        count: int,
        price: int,    # cents (1–99)
        order_type: str = "limit",
    ) -> Optional[Dict]:
        """Place a limit order. Returns the created order object."""
        body = {
            "ticker": ticker,
            "action": action,
            "side": side,
            "count": count,
            "type": order_type,
            "yes_price": price if side == "yes" else 100 - price,
        }
        data = self._post("/portfolio/orders", body)
        return (data or {}).get("order")

    def cancel_order(self, order_id: str) -> Optional[Dict]:
        return self._delete(f"/portfolio/orders/{order_id}")

    def get_orders(self, status: str = "open") -> List[Dict]:
        data = self._get("/portfolio/orders", {"status": status})
        return (data or {}).get("orders", [])

    # ── Portfolio ─────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Account balance in dollars (API returns cents)."""
        data = self._get("/portfolio/balance")
        return (data or {}).get("balance", 0) / 100

    def get_positions(self) -> List[Dict]:
        data = self._get("/portfolio/positions")
        return (data or {}).get("market_positions", [])

    def get_fills(self, limit: int = 50) -> List[Dict]:
        data = self._get("/portfolio/fills", {"limit": limit})
        return (data or {}).get("fills", [])
