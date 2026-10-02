"""Forex agent data access: orders and their payments. Synchronous; callers run it in a thread."""
import random
import string
from datetime import datetime, timezone

from app.db.repository import CoreRepo

ORDER_SELECT = "*, users(phone, name)"


class ForexRepo(CoreRepo):
    def create_order(self, user_id: str, currency: str, foreign_amount: float, product: str, mid_rate: float, rate: float,
                     fee_inr: int, total_inr: int, status: str = "confirmed") -> dict:
        for _ in range(5):
            ref = "FX" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
            try:
                row = self.db.table("forex_orders").insert({
                    "ref": ref, "user_id": user_id, "currency": currency, "foreign_amount": foreign_amount, "product": product,
                    "mid_rate": mid_rate, "rate": rate, "fee_inr": fee_inr, "total_inr": total_inr, "status": status,
                }).execute().data[0]
                return self.get_order_with_user(row["id"])
            except Exception:
                continue  # ref collision, try another
        raise RuntimeError("Could not create forex order")

    def get_order_with_user(self, order_id: str) -> dict | None:
        rows = self.db.table("forex_orders").select(ORDER_SELECT).eq("id", order_id).limit(1).execute().data
        return rows[0] if rows else None

    def get_order(self, order_id: str, user_id: str) -> dict | None:
        rows = self.db.table("forex_orders").select(ORDER_SELECT).eq("id", order_id).eq("user_id", user_id).limit(1).execute().data
        return rows[0] if rows else None

    def get_order_by_ref(self, ref: str) -> dict | None:
        rows = self.db.table("forex_orders").select(ORDER_SELECT).eq("ref", ref).limit(1).execute().data
        return rows[0] if rows else None

    def list_user_orders(self, user_id: str, limit: int = 9) -> list[dict]:
        return (self.db.table("forex_orders").select(ORDER_SELECT).eq("user_id", user_id)
                .order("created_at", desc=True).limit(limit).execute().data)

    def set_status(self, order: dict, status: str, allowed_from: tuple[str, ...]) -> bool:
        """Move an order to `status` only from one of `allowed_from`. True if this call did it."""
        return bool(self.db.table("forex_orders").update({"status": status}).eq("id", order["id"])
                    .in_("status", list(allowed_from)).execute().data)

    def cancel_order(self, order: dict) -> bool:
        return self.set_status(order, "cancelled", ("pending", "confirmed"))

    # ---- payments (a pending order waits for its Razorpay link to be paid or to expire)
    def create_payment(self, order_id: str, user_id: str, amount_inr: int, link_id: str, short_url: str,
                       expires_at: datetime) -> dict:
        return self.db.table("payments").insert({
            "kind": "forex", "forex_order_id": order_id, "user_id": user_id, "amount_inr": amount_inr,
            "link_id": link_id, "short_url": short_url, "expires_at": expires_at.isoformat(),
        }).execute().data[0]

    def mark_paid(self, link_id: str) -> dict | None:
        """Payment received: confirm the order. Returns it, or None if it isn't ours or was already handled."""
        paid = (self.db.table("payments").update({"status": "paid", "paid_at": datetime.now(timezone.utc).isoformat()})
                .eq("link_id", link_id).eq("kind", "forex").eq("status", "created").execute().data)
        if not paid:
            return None
        confirmed = (self.db.table("forex_orders").update({"status": "confirmed"})
                     .eq("id", paid[0]["forex_order_id"]).eq("status", "pending").execute().data)
        return self.get_order_with_user(paid[0]["forex_order_id"]) if confirmed else None

    def cancel_payment(self, order_id: str) -> None:
        self.db.table("payments").update({"status": "cancelled"}).eq("forex_order_id", order_id) \
            .eq("kind", "forex").eq("status", "created").execute()

    def expire_unpaid(self, now: datetime) -> list[dict]:
        """Cancel pending orders whose payment window passed. Returns them."""
        due = (self.db.table("payments").select("forex_order_id, link_id").eq("kind", "forex").eq("status", "created")
               .lt("expires_at", now.isoformat()).limit(50).execute().data)
        expired = []
        for p in due:
            if not self.db.table("payments").update({"status": "expired"}).eq("link_id", p["link_id"]) \
                    .eq("status", "created").execute().data:
                continue  # paid or cancelled a moment ago
            order = self.get_order_with_user(p["forex_order_id"])
            if order and order["status"] == "pending":
                self.cancel_order(order)
                expired.append(order)
        return expired
