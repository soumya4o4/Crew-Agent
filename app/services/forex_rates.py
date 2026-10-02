"""Live exchange rates from open.er-api.com (free, no API key, about 160 currencies, refreshed daily by the provider).
One call returns every rate, so we fetch once and keep it for a while. If the service is down we keep serving the last rates
we had (and say how old they are); with none at all, callers say rates are unavailable instead of guessing."""
import logging
import time

import httpx

logger = logging.getLogger(__name__)
URL = "https://open.er-api.com/v6/latest/INR"
TTL_S = 30 * 60
STALE_LIMIT_S = 24 * 3600  # never quote money from rates older than a day


class RateService:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport  # tests pass httpx.MockTransport
        self._rates: dict[str, float] | None = None
        self._at = 0.0

    async def rates(self) -> dict[str, float] | None:
        """{code: units of that currency per 1 INR}, or None if there are no usable rates."""
        age = time.monotonic() - self._at
        if self._rates and age < TTL_S:
            return self._rates
        try:
            async with httpx.AsyncClient(timeout=8, transport=self._transport) as http:
                resp = await http.get(URL)
                resp.raise_for_status()
                data = resp.json()
            rates = {k: float(v) for k, v in (data.get("rates") or {}).items() if isinstance(v, (int, float)) and v > 0}
            if data.get("result") == "success" and rates:
                self._rates, self._at = rates, time.monotonic()
                return rates
        except Exception as exc:
            logger.warning("Rate service failed: %s", type(exc).__name__)
        return self._rates if self._rates and age < STALE_LIMIT_S else None

    async def inr_per_unit(self, code: str) -> float | None:
        """The mid-market price of one unit of `code` in rupees."""
        rates = await self.rates()
        rate = (rates or {}).get(code.upper())
        return 1 / rate if rate else None

    @property
    def updated_ago_min(self) -> int:
        return int((time.monotonic() - self._at) // 60) if self._at else 0
