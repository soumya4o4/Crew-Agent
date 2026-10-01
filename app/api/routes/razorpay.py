"""Razorpay webhook (payment_link.paid) and the sweeper that releases unpaid bookings."""
import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request

from app.api.routes.whatsapp import get_concierge
from app.services.whatsapp_service import WhatsAppService

logger = logging.getLogger(__name__)
router = APIRouter()
SWEEP_EVERY_S = 60


@router.post("/razorpay")
async def razorpay_webhook(request: Request):
    """Razorpay calls this when a payment link is paid. We confirm the booking and send the ticket on WhatsApp."""
    from app.services.razorpay_service import RazorpayService

    body = await request.body()
    if not RazorpayService.verify_webhook(body, request.headers.get("X-Razorpay-Signature", "")):
        raise HTTPException(status_code=400, detail="Bad signature")
    event = json.loads(body)
    if event.get("event") == "payment_link.paid":
        link_id = event["payload"]["payment_link"]["entity"]["id"]
        try:
            result = await get_concierge().confirm_payment(link_id)
            if result:  # None means we had already handled it (Razorpay retries webhooks)
                number, messages = result
                for m in messages:
                    await WhatsAppService.send(number, m)
        except Exception:
            logger.exception("Could not confirm payment %s", link_id)
            raise HTTPException(status_code=500, detail="Try again")  # Razorpay will retry
    return {"status": "ok"}


async def sweep_unpaid_bookings() -> None:
    """Every minute: cancel flight and hotel bookings whose payment window ended and tell the user."""
    while True:
        try:
            for number, messages in await get_concierge().release_expired():
                for m in messages:
                    await WhatsAppService.send(number, m)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Payment sweeper failed")
        await asyncio.sleep(SWEEP_EVERY_S)
