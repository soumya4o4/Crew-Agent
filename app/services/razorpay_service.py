"""Razorpay payment links: create one per booking, verify the webhook that says it was paid."""
import hashlib
import hmac

import httpx

from app.core.config import settings

API = "https://api.razorpay.com/v1"


class RazorpayService:
    @staticmethod
    def configured() -> bool:
        return bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)

    @staticmethod
    def is_test_mode() -> bool:
        return settings.RAZORPAY_KEY_ID.startswith("rzp_test_")

    @staticmethod
    async def create_payment_link(amount_inr: int, reference_id: str, description: str, phone: str,
                                  name: str = "", expire_minutes: int = 20, client: httpx.AsyncClient | None = None) -> dict:
        """Returns {"id", "short_url"}. reference_id comes back in the webhook, so it ties a payment to a booking.
        Razorpay needs expire_by to be at least 15 minutes away."""
        import time
        payload = {
            "amount": amount_inr * 100,  # paise
            "currency": "INR",
            "reference_id": reference_id,
            "description": description[:255],
            "customer": {"contact": phone, **({"name": name} if name else {})},
            "notify": {"sms": False, "email": False},  # we deliver the link on WhatsApp ourselves
            "expire_by": int(time.time()) + max(expire_minutes, 16) * 60,
        }
        auth = (settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET)
        async with (client or httpx.AsyncClient(timeout=15)) as http:
            resp = await http.post(f"{API}/payment_links", json=payload, auth=auth)
            resp.raise_for_status()
            data = resp.json()
        return {"id": data["id"], "short_url": data["short_url"]}

    @staticmethod
    def verify_webhook(body: bytes, signature: str) -> bool:
        """Razorpay signs the raw request body with HMAC-SHA256 using the webhook secret."""
        if not settings.RAZORPAY_WEBHOOK_SECRET or not signature:
            return False
        expected = hmac.new(settings.RAZORPAY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)


class RazorpayGateway:
    """What the Flight agent talks to. Tests swap in a fake with the same three methods."""

    @property
    def enabled(self) -> bool:
        return RazorpayService.configured()

    @property
    def test_mode(self) -> bool:
        return RazorpayService.is_test_mode()

    async def create_link(self, amount_inr: int, reference_id: str, description: str, phone: str, name: str,
                          expire_minutes: int) -> dict:
        return await RazorpayService.create_payment_link(amount_inr, reference_id, description, phone, name, expire_minutes)

    async def link_status(self, link_id: str) -> str:
        """'created', 'paid', 'cancelled' or 'expired'."""
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(f"{API}/payment_links/{link_id}", auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
            resp.raise_for_status()
            return resp.json()["status"]

    async def cancel_link(self, link_id: str) -> None:
        async with httpx.AsyncClient(timeout=15) as http:
            await http.post(f"{API}/payment_links/{link_id}/cancel", auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))
