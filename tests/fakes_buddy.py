"""Buddy fakes: an in-memory BuddyRepo (shares users/messages with the main FakeRepo) and a scripted brain."""
import uuid
from datetime import timedelta

from app.agents.buddy.brain import BuddyReply
from app.core.utils import now_ist, to_ist


class FakeBuddyRepo:
    def __init__(self, core):
        self.core, self.memories = core, []

    def __getattr__(self, name):  # users, conversations, chat history...
        return getattr(self.core, name)

    def list_memories(self, user_id, limit=40):
        mine = [m for m in self.memories if m["user_id"] == user_id]
        return [{"id": m["id"], "category": m["category"], "content": m["content"]} for m in reversed(mine)][:limit]

    def add_memories(self, user_id, items):
        have = {m["content"].lower() for m in self.memories if m["user_id"] == user_id}
        added = 0
        for it in items:
            if it["content"].lower() not in have:
                have.add(it["content"].lower())
                self.memories.append({"id": str(uuid.uuid4()), "user_id": user_id, **it})
                added += 1
        return added

    stay = None  # set a hotel booking dict (with hotels, hotel_rooms) to test stay help

    def upcoming_flights(self, user_id):
        now = now_ist()
        return [b for b in self.core.bookings.values() if b["user_id"] == user_id and b["status"] == "confirmed"
                and now - timedelta(hours=3) <= to_ist(b["flights"]["departure_time"]) <= now + timedelta(hours=48)]

    def current_stay(self, user_id):
        return self.stay

    stays = None  # a list of hotel bookings for user_stays; defaults to the single `stay`

    def user_stays(self, user_id, limit=3):
        return self.stays if self.stays is not None else ([self.stay] if self.stay else [])

    paid = None  # a list of {amount_inr, kind, paid_at} for paid_payments

    def paid_payments(self, user_id, limit=40):
        return (self.paid or [])[:limit]

    def forget_user(self, user_id):
        self.memories = [m for m in self.memories if m["user_id"] != user_id]
        self.core.messages = [m for m in self.core.messages if m[0] != user_id]


class FakeBrain:
    """Queue replies with .queue.append(BuddyReply(...)); otherwise Buddy says something generic. Every call is recorded."""

    def __init__(self):
        self.queue, self.calls, self.fail = [], [], False

    async def respond(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("openai down")
        return self.queue.pop(0) if self.queue else BuddyReply("Main yahin hoon, bolo.")
