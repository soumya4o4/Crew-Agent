import asyncio
import json
import logging
import time
from functools import lru_cache

from fastapi import APIRouter, BackgroundTasks, Request, HTTPException, Response
from app.core.config import settings
from app.db.supabase_client import get_supabase
from app.agents.registry import build_concierge
from app.services.whatsapp_service import WhatsAppService

logger = logging.getLogger(__name__)
router = APIRouter()
SEEN_TTL_S = 15 * 60
_seen: dict[str, float] = {}            # message id -> when we first got it
_locks: dict[str, asyncio.Lock] = {}    # one conversation at a time per WhatsApp number


def is_duplicate(message_id: str) -> bool:
    """Meta resends a webhook it thinks we were too slow to answer, and our reply can take a few seconds (the AI, maps).
    Remember every message id the moment it arrives, so a retry never runs the flow a second time."""
    now = time.monotonic()
    for key in [k for k, t in _seen.items() if now - t > SEEN_TTL_S]:
        del _seen[key]
    if message_id in _seen:
        return True
    _seen[message_id] = now
    return False


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


async def process_message(msg: dict) -> None:
    """Run the flow for one message and send the replies. Messages from the same person go one after another."""
    if msg.get("media") and msg["media"]["kind"] == "audio" and settings.OPENAI_API_KEY:
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
            audio_bytes, _ = await WhatsAppService.download_media(msg["media"]["id"])
            resp = await client.audio.transcriptions.create(model="whisper-1", file=("audio.ogg", audio_bytes, "audio/ogg"))
            transcribed_text = (getattr(resp, "text", "") or "")
            if transcribed_text:
                msg["text"] = transcribed_text
                msg["media"] = None  # treat as pure text
        except Exception as e:
            logger.error(f"Failed to transcribe audio: {e}")

    lock = _locks.setdefault(msg["whatsapp_number"], asyncio.Lock())
    async with lock:
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


@router.post("/whatsapp")
@router.post("")
async def receive_message(request: Request, background: BackgroundTasks):
    """
    Receive incoming messages (typed text, button taps, list picks). Answers 200 straight away and does the work after
    the response, so Meta never times out and retries. Always 200, so WhatsApp doesn't keep retrying a message that errored.
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
    if msg["message_id"] and is_duplicate(msg["message_id"]):
        return {"status": "duplicate"}
    background.add_task(process_message, msg)
    return {"status": "success"}
