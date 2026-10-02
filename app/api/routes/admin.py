"""Admin API for our visa team, plus the demo timer that moves applications along by itself."""
import asyncio
import hmac
import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.api.routes.whatsapp import get_concierge
from app.core.config import settings
from app.services.whatsapp_service import WhatsAppService

logger = logging.getLogger(__name__)
router = APIRouter()
DEMO_TICK_S = 30


class VisaStatusUpdate(BaseModel):
    status: str                 # in_review | approved | rejected
    note: str | None = None     # shown to the user (e.g. the rejection reason)
    file_url: str | None = None # approved: link to the visa PDF (https://...) which we forward to the user


class ForexStatusUpdate(BaseModel):
    status: str                 # fulfilled
    note: str | None = None     # shown to the user


def _check_token(token: str | None) -> None:
    if not settings.ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="Admin API is disabled (set ADMIN_TOKEN)")
    if not token or not hmac.compare_digest(token, settings.ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="Bad token")


@router.post("/visa/{ref}/status")
async def update_visa_status(ref: str, body: VisaStatusUpdate, x_admin_token: str | None = Header(default=None)):
    """The visa team calls this as an application moves along. The user is notified on WhatsApp."""
    _check_token(x_admin_token)
    try:
        number, messages = await get_concierge().agents["visa"].update_status(ref.upper(), body.status, body.note, body.file_url)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such application")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    for m in messages:
        await WhatsAppService.send(number, m)
    return {"status": "ok", "notified": number}


@router.post("/forex/{ref}/status")
async def update_forex_status(ref: str, body: ForexStatusUpdate, x_admin_token: str | None = Header(default=None)):
    """The forex desk calls this once cash is delivered or the card is issued. The user is notified on WhatsApp."""
    _check_token(x_admin_token)
    try:
        number, messages = await get_concierge().agents["forex"].update_status(ref.upper(), body.status, body.note)
    except LookupError:
        raise HTTPException(status_code=404, detail="No such order")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    for m in messages:
        await WhatsAppService.send(number, m)
    return {"status": "ok", "notified": number}


async def visa_demo_loop() -> None:
    """VISA_DEMO_AUTOPROGRESS=true: submitted -> in_review (90s) -> approved (4 min), so the journey can be demoed."""
    while True:
        try:
            for number, messages in await get_concierge().agents["visa"].advance_demo():
                for m in messages:
                    await WhatsAppService.send(number, m)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Visa demo loop failed")
        await asyncio.sleep(DEMO_TICK_S)
