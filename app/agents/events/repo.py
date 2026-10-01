"""Events data access. Synchronous; callers run it in a thread."""
from datetime import datetime, timezone

from app.db.repository import CoreRepo


def _utc_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # 'Z' form avoids a '+' in the query string


class EventsRepo(CoreRepo):
    def list_events(self, city_code: str, start: datetime, end: datetime) -> list[dict]:
        """Events starting in [start, end) in a city, soonest first."""
        return (self.db.table("events").select("*").eq("city_code", city_code)
                .gte("starts_at", _utc_str(start)).lt("starts_at", _utc_str(end))
                .order("starts_at").limit(60).execute().data)

    def get_event(self, event_id: str) -> dict | None:
        rows = self.db.table("events").select("*").eq("id", event_id).limit(1).execute().data
        return rows[0] if rows else None
