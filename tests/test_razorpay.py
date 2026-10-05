import asyncio
import hashlib
import hmac
import json

import httpx

from app.core.config import settings
from app.services.razorpay_service import RazorpayService


def test_webhook_signature(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "whsec")
    body = json.dumps({"event": "payment_link.paid"}).encode()
    good = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert RazorpayService.verify_webhook(body, good)
    assert not RazorpayService.verify_webhook(body, "deadbeef")
    assert not RazorpayService.verify_webhook(body + b" ", good)  # tampered body
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")
    assert not RazorpayService.verify_webhook(body, good)  # no secret configured -> never trust


def test_create_payment_link_sends_paise_and_reference(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_KEY_ID", "rzp_test_abc")
    monkeypatch.setattr(settings, "RAZORPAY_KEY_SECRET", "secret")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"], seen["auth"] = json.loads(request.content), request.headers["authorization"]
        return httpx.Response(200, json={"id": "plink_1", "short_url": "https://rzp.io/i/abc"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    link = asyncio.run(RazorpayService.create_payment_link(4310, "FO3K35", "Flight IDR-BOM", "+917000000001", client=client))
    assert link == {"id": "plink_1", "short_url": "https://rzp.io/i/abc"}
    assert seen["body"]["amount"] == 431000 and seen["body"]["reference_id"] == "FO3K35"
    assert seen["auth"].startswith("Basic ") and RazorpayService.is_test_mode()


def test_payment_link_text_has_no_emoji_razorpay_rejects():
    """Razorpay answers 400 "Conversion from collation ... impossible" to 4-byte characters like the hotel emoji."""
    import asyncio, json
    import httpx
    from app.services.razorpay_service import RazorpayService, clean

    assert clean("🏨 Ar Suites - Pune, 03 Oct ➜ 05 Oct + ✈️ Indore ➜ Goa") == "Ar Suites - Pune, 03 Oct ➜ 05 Oct + ✈ Indore ➜ Goa"
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={"id": "plink_1", "short_url": "https://rzp.io/i/x"})

    async def run():
        return await RazorpayService.create_payment_link(100, "HB1", "🏨 Hotel Taj ➜ stay", "+919876543210", "Aarav 🙂",
                                                         client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    asyncio.run(run())
    assert sent["description"] == "Hotel Taj ➜ stay" and sent["customer"]["name"] == "Aarav"
