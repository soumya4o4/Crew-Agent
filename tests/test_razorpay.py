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
