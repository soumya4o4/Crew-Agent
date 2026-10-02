"""Forex fakes: an in-memory ForexRepo (shares users/conversations with the main FakeRepo) and fixed exchange rates."""
import uuid
from datetime import datetime, timezone


class FakeRates:
    """Units of each currency per 1 INR. 1 USD = Rs 80, 1 AED = Rs 20, 1 EUR = Rs 100 keeps the sums easy to check."""

    def __init__(self, down=False):
        self.table, self.down, self.updated_ago_min = {"USD": 1 / 80, "AED": 1 / 20, "EUR": 1 / 100, "JPY": 1 / 0.5, "INR": 1.0}, down, 3

    async def rates(self):
        return None if self.down else self.table

    async def inr_per_unit(self, code):
        rate = None if self.down else self.table.get(code.upper())
        return 1 / rate if rate else None


class FakeForexRepo:
    def __init__(self, core):
        self.core, self.orders, self.payments = core, {}, {}
        self.n = 0

    def __getattr__(self, name):  # users, conversations, chat history...
        return getattr(self.core, name)

    def _with_user(self, o):
        user = next((u for u in self.core.users.values() if u["id"] == o["user_id"]), {"phone": "+919876543210", "name": "Aarav Sharma"})
        return {**o, "users": {"phone": user["phone"], "name": user["name"]}}

    def create_order(self, user_id, currency, foreign_amount, product, mid_rate, rate, fee_inr, total_inr, status="confirmed"):
        self.n += 1
        oid = str(uuid.uuid4())
        self.orders[oid] = {"id": oid, "ref": f"FX{self.n:05d}", "user_id": user_id, "currency": currency, "foreign_amount": foreign_amount,
                            "product": product, "mid_rate": mid_rate, "rate": rate, "fee_inr": fee_inr, "total_inr": total_inr, "status": status}
        return self._with_user(self.orders[oid])

    def get_order_with_user(self, order_id):
        return self._with_user(self.orders[order_id]) if order_id in self.orders else None

    def get_order(self, order_id, user_id):
        o = self.orders.get(order_id)
        return self._with_user(o) if o and o["user_id"] == user_id else None

    def get_order_by_ref(self, ref):
        return next((self._with_user(o) for o in self.orders.values() if o["ref"] == ref), None)

    def list_user_orders(self, user_id, limit=9):
        return [self._with_user(o) for o in reversed(list(self.orders.values())) if o["user_id"] == user_id][:limit]

    def set_status(self, order, status, allowed_from):
        o = self.orders[order["id"]]
        if o["status"] in allowed_from:
            o["status"] = status
            return True
        return False

    def cancel_order(self, order):
        return self.set_status(order, "cancelled", ("pending", "confirmed"))

    def create_payment(self, order_id, user_id, amount_inr, link_id, short_url, expires_at):
        self.payments[link_id] = {"order_id": order_id, "status": "created", "expires_at": expires_at}
        return self.payments[link_id]

    def mark_paid(self, link_id):
        p = self.payments.get(link_id)
        if not p or p["status"] != "created":
            return None
        p["status"] = "paid"
        o = self.orders[p["order_id"]]
        if o["status"] != "pending":
            return None
        o["status"] = "confirmed"
        return self._with_user(o)

    def cancel_payment(self, order_id):
        for p in self.payments.values():
            if p["order_id"] == order_id and p["status"] == "created":
                p["status"] = "cancelled"

    def expire_unpaid(self, now):
        out = []
        for p in self.payments.values():
            if p["status"] == "created" and p["expires_at"] < now:
                p["status"] = "expired"
                o = self.orders[p["order_id"]]
                if o["status"] == "pending":
                    o["status"] = "cancelled"
                    out.append(self._with_user(o))
        return out
