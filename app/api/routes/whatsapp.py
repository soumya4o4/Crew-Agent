import json
import logging
from functools import lru_cache

from fastapi import APIRouter, Request, HTTPException, Response
from app.core.config import settings
from app.db.supabase_client import get_supabase
from app.agents.registry import build_concierge
from app.services.whatsapp_service import WhatsAppService

logger = logging.getLogger(__name__)
router = APIRouter()


@lru_cache
def get_concierge():
    return build_concierge(get_supabase())


@router.get("/whatsapp")
@router.get("")
def verify_webhook(request: Request):
    """
    Webhook verification for WhatsApp Cloud API.
    """
    verify_token = settings.WHATSAPP_VERIFY_TOKEN
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == verify_token:
            return Response(content=challenge, media_type="text/plain")

    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/whatsapp")
@router.post("")
async def receive_message(request: Request):
    """
    Receive incoming messages (typed text, button taps, list picks) and run the booking flow.
    Always answers 200 so WhatsApp doesn't keep retrying a message that errored.
    """
    body = await request.body()
    if not WhatsAppService.verify_signature(body, request.headers.get("X-Hub-Signature-256", "")):
        logger.warning("Rejected a webhook call: %s", WhatsAppService.signature_diagnosis(body, request.headers.get("X-Hub-Signature-256", "")))
        raise HTTPException(status_code=403, detail="Bad signature")
    try:
        payload = json.loads(body)
    except ValueError:
        return {"status": "ignored"}
    msg = WhatsAppService.extract_message_data(payload)
    if not msg:
        return {"status": "ignored"}

    try:
        await WhatsAppService.mark_read(msg["message_id"])  # blue ticks + typing bubble
        replies = await get_concierge().handle(
            msg["whatsapp_number"], msg["name"], msg["message_id"], msg["text"], msg["reply_id"], msg["media"], msg["location"]
        )
        for reply in replies:
            await WhatsAppService.send(msg["whatsapp_number"], reply)
    except Exception:
        logger.exception("Flow failed for %s", msg["whatsapp_number"])
        await WhatsAppService.send_message(
            msg["whatsapp_number"], "Oops, something went wrong. 🙏 Please try again in a bit, or type *menu* to start over."
        )
    return {"status": "success"}
