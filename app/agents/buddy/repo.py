"""Buddy data access: what we remember about a user and the trips they have booked. Synchronous; callers run it in a thread."""
from datetime import timedelta

from app.core.utils import now_ist, to_ist
from app.db.repository import CoreRepo

CATEGORIES = ("about", "people", "plans", "worries", "likes")
MAX_MEMORIES = 60  # the oldest are dropped beyond this


class BuddyRepo(CoreRepo):
    def list_memories(self, user_id: str, limit: int = 40) -> list[dict]:
        """Newest first: [{id, category, content}]."""
        return (self.db.table("user_memories").select("id, category, content").eq("user_id", user_id)
                .order("created_at", desc=True).limit(limit).execute().data)

    def add_memories(self, user_id: str, items: list[dict]) -> int:
        """Store new facts ({category, content}), skipping ones we already have. Returns how many were added."""
        have = {m["content"].strip().lower() for m in self.list_memories(user_id, MAX_MEMORIES)}
        rows = []
        for it in items:
            key = it["content"].strip().lower()
            if key and key not in have:
                have.add(key)
                rows.append({"user_id": user_id, "category": it["category"], "content": it["content"].strip()})
        if rows:
            self.db.table("user_memories").insert(rows).execute()
            old = (self.db.table("user_memories").select("id").eq("user_id", user_id).order("created_at", desc=True)
                   .range(MAX_MEMORIES, MAX_MEMORIES + 200).execute().data)
            if old:
                self.db.table("user_memories").delete().in_("id", [r["id"] for r in old]).execute()
        return len(rows)

    # ---- the user's trip (their own bookings, read-only)
    def upcoming_flights(self, user_id: str) -> list[dict]:
        """Confirmed flight bookings (with `flights`) that left up to 3 hours ago or leave within the next 48 hours."""
        now = now_ist()
        rows = (self.db.table("bookings").select("*, flights(*)").eq("user_id", user_id).eq("status", "confirmed")
                .order("created_at", desc=True).limit(30).execute().data)
        return [b for b in rows if now - timedelta(hours=3) <= to_ist(b["flights"]["departure_time"]) <= now + timedelta(hours=48)]

    def current_stay(self, user_id: str) -> dict | None:
        """A confirmed hotel stay that is on now or starts within two days."""
        today = now_ist().date()
        rows = (self.db.table("hotel_bookings").select("*, hotels(*), hotel_rooms(*)").eq("user_id", user_id)
                .eq("status", "confirmed").gte("check_out", today.isoformat()).lte("check_in", (today + timedelta(days=2)).isoformat())
                .order("check_in").limit(1).execute().data)
        return rows[0] if rows else None

    def user_stays(self, user_id: str, limit: int = 3) -> list[dict]:
        """Hotel stays the user has booked that are not over yet (confirmed, or waiting for payment), soonest first."""
        today = now_ist().date()
        return (self.db.table("hotel_bookings").select("*, hotels(*), hotel_rooms(*)").eq("user_id", user_id)
                .in_("status", ["confirmed", "pending"]).gte("check_out", today.isoformat())
                .order("check_in").limit(limit).execute().data)

    def paid_payments(self, user_id: str, limit: int = 40) -> list[dict]:
        """The user's paid payments, newest first: [{amount_inr, kind, paid_at}]. kind is flight, hotel, visa or forex."""
        return (self.db.table("payments").select("amount_inr, kind, paid_at").eq("user_id", user_id).eq("status", "paid")
                .order("paid_at", desc=True).limit(limit).execute().data)

    def forget_user(self, user_id: str) -> None:
        """Delete everything we remember and the stored chat history. Bookings and payments are business records and stay."""
        self.db.table("user_memories").delete().eq("user_id", user_id).execute()
        self.db.table("messages").delete().eq("user_id", user_id).execute()
