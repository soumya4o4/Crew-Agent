import hashlib
import hmac
import re

from app.schemas.whatsapp import WhatsAppWebhookPayload
from app.core.config import settings
import httpx

GRAPH_URL = "https://graph.facebook.com/v19.0"


_media_ids: dict[str, str] = {}  # photo link -> WhatsApp media id (ids stay valid for 30 days; kept for the life of the process)
MAX_PHOTO_BYTES = 5_000_000


class WhatsAppService:
    @staticmethod
    def _escape_unicode(body: bytes) -> bytes:
        """Meta computes the signature over the payload with every non-ASCII character written as a lowercase unicode escape
        (backslash, u and four hex digits; an emoji becomes a surrogate pair), even when the bytes it sends carry the characters."""
        def esc(m):
            cp = ord(m.group(0))
            if cp > 0xFFFF:
                cp -= 0x10000
                return "\\u%04x\\u%04x" % (0xD800 + (cp >> 10), 0xDC00 + (cp & 0x3FF))
            return "\\u%04x" % cp
        return re.sub(r"[^\x00-\x7f]", esc, body.decode("utf-8", "replace")).encode()

    @staticmethod
    def _signatures(body: bytes) -> list[str]:
        """The signatures a genuine call can carry: over the bytes as received, and over the unicode-escaped form."""
        secret = settings.WHATSAPP_APP_SECRET.encode()
        forms = [body] + ([WhatsAppService._escape_unicode(body)] if any(b > 127 for b in body) else [])
        return [hmac.new(secret, f, hashlib.sha256).hexdigest() for f in forms]

    @staticmethod
    def verify_signature(body: bytes, header: str) -> bool:
        """Meta signs every webhook call: X-Hub-Signature-256 = "sha256=" + HMAC-SHA256(app secret, payload).
        Without this anyone who finds the URL could send the bot fake messages. No secret configured means we refuse everything."""
        if not settings.WHATSAPP_APP_SECRET or not header.startswith("sha256="):
            return False
        return any(hmac.compare_digest(sig, header[len("sha256="):]) for sig in WhatsAppService._signatures(body))

    @staticmethod
    def signature_diagnosis(body: bytes, header: str) -> str:
        """Why a call was rejected, for the log. Never contains the secret or any signature."""
        if not settings.WHATSAPP_APP_SECRET:
            return "WHATSAPP_APP_SECRET is not set"
        if not header:
            return "no X-Hub-Signature-256 header, so this call is not from Meta"
        if not header.startswith("sha256="):
            return "the signature header is not in the sha256=... form"
        return (f"signature does not match ({len(body)} bytes, {'with' if any(b > 127 for b in body) else 'without'} non-ASCII text): "
                "the app secret in .env is most likely from a different Meta app than the one sending this webhook")

    @staticmethod
    def extract_message_data(payload: dict):
        """
        Extracts relevant information from WhatsApp Cloud API webhook payload.
        Returns text for typed messages, and reply_id/text for button and list taps.
        """
        try:
            webhook_data = WhatsAppWebhookPayload(**payload)
        except Exception as e:
            print("Failed to parse WhatsApp payload:", e)
            return None

        if not webhook_data.entry:
            return None

        changes = webhook_data.entry[0].changes
        if not changes:
            return None

        value = changes[0].value
        if not value.messages:
            return None

        message = value.messages[0]
        contact = value.contacts[0] if value.contacts else None

        text, reply_id, media, location = "", None, None, None
        if message.type == "text" and message.text:
            text = message.text.body
        elif message.type == "interactive" and message.interactive:
            reply = message.interactive.get("button_reply") or message.interactive.get("list_reply") or {}
            reply_id, text = reply.get("id"), reply.get("title", "")
            if not reply_id:
                return None
        elif message.type in ("image", "document", "video") and (message.image or message.document or message.video):
            m = message.image or message.document or message.video
            media = {"id": m["id"], "mime": m.get("mime_type", ""), "filename": m.get("filename", ""), "kind": message.type}
            text = m.get("caption", "") or ""
        elif message.type == "location" and message.location:
            try:
                lat, lon = float(message.location["latitude"]), float(message.location["longitude"])
            except (KeyError, TypeError, ValueError):
                return None
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                return None
            location = {"lat": lat, "lon": lon, "name": message.location.get("name", "") or "",
                        "address": message.location.get("address", "") or ""}
        else:
            return None  # audio, video, stickers etc. aren't handled

        return {
            "whatsapp_number": message.from_,
            "name": contact.profile.name if contact and contact.profile else "Unknown",
            "message_id": message.id,
            "text": text,
            "reply_id": reply_id,
            "media": media,
            "location": location,
            "timestamp": message.timestamp
        }

    @staticmethod
    async def _post(to_number: str | None, payload: dict) -> bool:
        """True if Meta accepted the message."""
        phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
        if not phone_number_id:
            print("WhatsApp Phone Number ID not configured.")
            return False

        url = f"{GRAPH_URL}/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}",
            "Content-Type": "application/json"
        }
        data = {"messaging_product": "whatsapp", **({"to": to_number} if to_number else {}), **payload}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(url, headers=headers, json=data)
                response.raise_for_status()
                return True
            except httpx.HTTPStatusError as e:
                print(f"Failed to send message to {to_number}: {e.response.text}")
            except Exception as e:
                print(f"Failed to send message to {to_number}: {e}")
        return False

    @staticmethod
    async def download_media(media_id: str, max_bytes: int = 8_000_000) -> tuple[bytes, str]:
        """Fetch a file the user sent. Returns (bytes, mime type). Raises ValueError if it is too big."""
        headers = {"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
        async with httpx.AsyncClient(timeout=30) as client:
            meta = await client.get(f"{GRAPH_URL}/{media_id}", headers=headers)
            meta.raise_for_status()
            info = meta.json()
            if int(info.get("file_size") or 0) > max_bytes:
                raise ValueError("file too large")
            file = await client.get(info["url"], headers=headers)
            file.raise_for_status()
            if len(file.content) > max_bytes:
                raise ValueError("file too large")
            return file.content, info.get("mime_type", "")

    @staticmethod
    async def send_message(to_number: str, text: str):
        await WhatsAppService._post(to_number, {"type": "text", "text": {"body": text}})

    @staticmethod
    async def mark_read(message_id: str):
        """Blue ticks + the 'typing…' bubble while we work. Cosmetic, so failures are ignored."""
        await WhatsAppService._post(None, {
            "status": "read", "message_id": message_id, "typing_indicator": {"type": "text"},
        })

    @staticmethod
    async def send(to_number: str, msg: dict):
        """Send one message built with text_msg / buttons_msg / list_msg, or an emoji reaction.
        A photo Meta cannot fetch (a dead link) must never cost the user the message itself: it is resent without the photo."""
        if await WhatsAppService._send(to_number, msg):
            return
        if msg["type"] == "image":
            if msg["body"]:
                await WhatsAppService._send(to_number, {"type": "text", "body": msg["body"]})
        elif msg.get("image"):
            await WhatsAppService._send(to_number, {k: v for k, v in msg.items() if k != "image"})

    @staticmethod
    async def _media_id(url: str) -> str | None:
        """Download a public photo and upload it to WhatsApp, once. Sending "by link" is accepted by Meta at once and fetched
        later, so a photo it cannot fetch just never shows up; an upload fails right here, where we can still send the text."""
        if url in _media_ids:
            return _media_ids[url]
        if not settings.WHATSAPP_PHONE_NUMBER_ID:
            return None
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                photo = await client.get(url)
                photo.raise_for_status()
                mime = photo.headers.get("content-type", "").split(";")[0].strip()
                if mime not in ("image/jpeg", "image/png") or len(photo.content) > MAX_PHOTO_BYTES:
                    raise ValueError(f"not a usable photo ({mime or 'unknown type'}, {len(photo.content)} bytes)")
                up = await client.post(
                    f"{GRAPH_URL}/{settings.WHATSAPP_PHONE_NUMBER_ID}/media",
                    headers={"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"},
                    data={"messaging_product": "whatsapp", "type": mime},
                    files={"file": ("photo" + (".png" if mime == "image/png" else ".jpg"), photo.content, mime)})
                up.raise_for_status()
                _media_ids[url] = up.json()["id"]
                return _media_ids[url]
        except Exception as e:
            print(f"Could not upload the photo {url}: {getattr(getattr(e, 'response', None), 'text', None) or e}")
            return None

    @staticmethod
    async def _send(to_number: str, msg: dict) -> bool:
        kind = msg["type"]
        photo = msg["url"] if kind == "image" else msg.get("image")
        media = None
        if photo:
            media_id = await WhatsAppService._media_id(photo)
            if not media_id:
                return False
            media = {"id": media_id}
        header = {"header": {"type": "image", "image": media}} if media and kind != "image" else {}
        if kind == "image":
            payload = {"type": "image", "image": {**media, **({"caption": msg["body"]} if msg.get("body") else {})}}
        elif kind == "reaction":
            payload = {"type": "reaction", "reaction": {"message_id": msg["message_id"], "emoji": msg["emoji"]}}
        elif kind == "document":
            payload = {"type": "document", "document": {"link": msg["url"], "filename": msg["filename"],
                                                        **({"caption": msg["caption"]} if msg.get("caption") else {})}}
        elif kind == "cta":
            payload = {"type": "interactive", "interactive": {
                "type": "cta_url", **header,
                "body": {"text": msg["body"]},
                "action": {"name": "cta_url", "parameters": {"display_text": msg["button_text"], "url": msg["url"]}},
            }}
        elif kind == "location_request":
            payload = {"type": "interactive", "interactive": {
                "type": "location_request_message", "body": {"text": msg["body"]}, "action": {"name": "send_location"}}}
        elif kind == "text":
            payload = {"type": "text", "text": {"body": msg["body"]}}
        elif kind == "buttons":
            payload = {"type": "interactive", "interactive": {
                "type": "button", **header,
                "body": {"text": msg["body"]},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": i, "title": t}} for i, t in msg["buttons"]
                ]},
            }}
        elif kind == "list":
            payload = {"type": "interactive", "interactive": {
                "type": "list",
                "body": {"text": msg["body"]},
                "action": {"button": msg["button"], "sections": [{
                    "title": msg["section"],
                    "rows": [{"id": i, "title": t, **({"description": d} if d else {})}
                             for i, t, d in msg["rows"]],
                }]},
            }}
        else:
            raise ValueError(f"Unknown message type: {kind}")
        return await WhatsAppService._post(to_number, payload)
