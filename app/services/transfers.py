import hashlib
import time
import httpx
from datetime import datetime

from app.core.config import settings

class TransfersError(Exception):
    pass

class TransfersClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    @staticmethod
    def configured() -> bool:
        return bool(settings.HOTELBEDS_TRANSFERS_API_KEY and settings.HOTELBEDS_TRANSFERS_SECRET)

    @staticmethod
    def _headers() -> dict[str, str]:
        raw = f"{settings.HOTELBEDS_TRANSFERS_API_KEY}{settings.HOTELBEDS_TRANSFERS_SECRET}{int(time.time())}"
        return {
            "Api-key": settings.HOTELBEDS_TRANSFERS_API_KEY,
            "X-Signature": hashlib.sha256(raw.encode()).hexdigest(),
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    async def _call(self, method: str, path: str, **kw) -> dict:
        async with httpx.AsyncClient(timeout=30, transport=self._transport) as http:
            resp = await http.request(
                method, 
                f"{settings.HOTELBEDS_BASE_URL}/transfer-api/1.0/{path}", 
                headers=self._headers(), 
                **kw
            )
        if resp.status_code >= 400:
            try:
                message = resp.json().get("error", {}).get("message", resp.text)
            except Exception:
                message = resp.text[:200]
            raise TransfersError(f"{resp.status_code}: {message}")
        return resp.json()

    async def search_transfers(self, iata_code: str, hotel_code: str, pickup_time: datetime, guests: int):
        """
        Search for available transfers from Airport (IATA) to a specific Hotel (Hotelbeds Code).
        """
        body = {
            "language": "en",
            "from": {
                "type": "IATA",
                "code": iata_code
            },
            "to": {
                "type": "ATLAS", # Hotelbeds internal hotel code type
                "code": hotel_code
            },
            "outbound": {
                "date": pickup_time.strftime("%Y-%m-%d"),
                "time": pickup_time.strftime("%H:%M:%S")
            },
            "occupancy": {
                "adults": guests,
                "children": 0,
                "infants": 0
            }
        }
        
        raw = await self._call("POST", "availability", json=body)
        return raw.get("services", [])
