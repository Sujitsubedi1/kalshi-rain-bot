import base64
import os
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from datetime import timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

load_dotenv()


@dataclass
class KalshiConfig:
    env: str
    key_id: str
    base_url: str  # includes /trade-api/v2
    private_key_path: str = ""
    private_key_pem: str = ""


class KalshiClient:
    """
    Minimal Kalshi REST client using RSA-PSS signing per Kalshi docs.
    - Sign: f"{timestamp}{METHOD}{path_without_query}"
    - Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE
    """
    MIN_REQUEST_INTERVAL = 0.35  # seconds -- Basic tier's per-second cap is easy
                                  # to burst past since each round fires several
                                  # calls back-to-back with no natural spacing.

    def __init__(self, cfg: KalshiConfig):
        self.cfg = cfg
        if cfg.private_key_pem:
            self._private_key = serialization.load_pem_private_key(
                cfg.private_key_pem.encode("utf-8"), password=None
            )
        else:
            self._private_key = self._load_private_key(cfg.private_key_path)
        self._last_request_time = 0.0
        self.session = requests.Session()
        self._clock_offset_ms = self._detect_clock_offset()

    def _detect_clock_offset(self) -> int:
        """Corrects for local OS clock drift by diffing against Kalshi's
        own server time (HTTP Date header) instead of relying on NTP."""
        try:
            local_before_ms = int(time.time() * 1000)
            resp = self.session.get(f"{self.cfg.base_url}/exchange/status", timeout=5)
            local_after_ms = int(time.time() * 1000)
            server_dt = parsedate_to_datetime(resp.headers["Date"])
            if server_dt.tzinfo is None:
                server_dt = server_dt.replace(tzinfo=timezone.utc)
            server_ms = int(server_dt.timestamp() * 1000)
            local_mid_ms = (local_before_ms + local_after_ms) // 2
            return server_ms - local_mid_ms
        except Exception:
            return 0

    @staticmethod
    def from_env() -> "KalshiClient":
        env = (os.getenv("KALSHI_ENV") or "demo").lower()
        key_id = os.getenv("KALSHI_KEY_ID") or ""
        key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH") or ""
        key_pem = os.getenv("KALSHI_PRIVATE_KEY_PEM") or ""

        if not key_id:
            raise RuntimeError("Missing KALSHI_KEY_ID in .env")
        if not key_path and not key_pem:
            raise RuntimeError("Missing KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY_PEM in .env")

        # Per docs: demo root is https://demo-api.kalshi.co/trade-api/v2
        # Production docs commonly use https://api.elections.kalshi.com/trade-api/v2
        if env == "demo":
            base = "https://demo-api.kalshi.co/trade-api/v2"
        else:
            base = "https://api.elections.kalshi.com/trade-api/v2"

        return KalshiClient(
            KalshiConfig(
                env=env, key_id=key_id, base_url=base,
                private_key_path=key_path, private_key_pem=key_pem.replace("\\n", "\n"),
            )
        )

    @staticmethod
    def _load_private_key(path: str):
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    @staticmethod
    def _path_only(url: str) -> str:
        # We sign only the path (no scheme/host), and no query params.
        parsed = urlparse(url)
        path = parsed.path
        return path

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        # Strip query params (docs requirement)
        path_wo_query = path.split("?")[0]
        msg = f"{timestamp_ms}{method.upper()}{path_wo_query}".encode("utf-8")

        sig_bytes = self._private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig_bytes).decode("utf-8")

    def _auth_headers(self, method: str, full_url: str) -> Dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000) + self._clock_offset_ms)
        path = self._path_only(full_url)
        signature = self._sign(timestamp_ms, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.cfg.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
        auth: bool = False,
        timeout: int = 30,
    ) -> requests.Response:
        url = self.cfg.base_url + path

        # A round has a full 15 minutes of slack before the next one is due,
        # so there's no cost to being very patient here -- observed 429s
        # have persisted through 30s of retrying already, which points to
        # something outside our own call pattern (e.g. a shared egress IP
        # on Render getting rate limited by Kalshi), not a burst we caused.
        backoff_seconds = 3.0
        backoff_cap = 30.0
        max_attempts = 9
        for attempt in range(1, max_attempts + 1):
            # Enforce a minimum gap between any two outgoing requests --
            # several calls fired back-to-back with no natural spacing is
            # what actually trips Basic tier's per-second cap, not overall
            # volume.
            since_last = time.time() - self._last_request_time
            if since_last < self.MIN_REQUEST_INTERVAL:
                time.sleep(self.MIN_REQUEST_INTERVAL - since_last)

            # Re-signed fresh each attempt -- a stale timestamp from before
            # a retry's sleep would otherwise get rejected on its own.
            headers: Dict[str, str] = {}
            if auth:
                headers.update(self._auth_headers(method, url))
            if json is not None:
                headers["Content-Type"] = "application/json"

            resp = self.session.request(
                method=method.upper(),
                url=url,
                params=params,
                json=json,
                headers=headers,
                timeout=timeout,
            )
            self._last_request_time = time.time()
            if resp.status_code not in (429, 503) or attempt == max_attempts:
                return resp

            wait = float(resp.headers.get("Retry-After", backoff_seconds))
            print(f"{resp.status_code} on {method} {path} (attempt {attempt}/{max_attempts}), retrying in {wait}s")
            time.sleep(wait)
            backoff_seconds = min(backoff_seconds * 2, backoff_cap)
        return resp

    # Convenience methods
    def get(self, path: str, *, params=None, auth=False) -> requests.Response:
        return self.request("GET", path, params=params, auth=auth)

    def post(self, path: str, *, json=None, auth=False) -> requests.Response:
        return self.request("POST", path, json=json, auth=auth)

    def delete(self, path: str, *, auth=False) -> requests.Response:
        return self.request("DELETE", path, auth=auth)
