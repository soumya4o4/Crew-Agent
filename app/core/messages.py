"""Outgoing message builders. Agents return lists of these plain dicts; WhatsAppService sends them.

WhatsApp limits: 3 reply buttons (title <= 20), list <= 10 rows (title <= 24, description <= 72).
"""


def text_msg(body: str) -> dict:
    return {"type": "text", "body": body}


def buttons_msg(body: str, buttons: list[tuple[str, str]], image: str | None = None) -> dict:
    """`image`: a public https link shown above the text (JPG or PNG, up to 5 MB)."""
    msg = {"type": "buttons", "body": body[:1024], "buttons": [(i, t[:20]) for i, t in buttons[:3]]}
    return {**msg, "image": image} if image else msg


def list_msg(body: str, button: str, rows: list[tuple[str, str, str]], section: str = "Options") -> dict:
    """rows are (id, title, description)."""
    return {
        "type": "list", "body": body[:1024], "button": button[:20], "section": section[:24],
        "rows": [(i, t[:24], d[:72]) for i, t, d in rows[:10]],
    }


def reaction_msg(message_id: str, emoji: str) -> dict:
    return {"type": "reaction", "emoji": emoji, "message_id": message_id}


def cta_msg(body: str, button_text: str, url: str, image: str | None = None) -> dict:
    """A message with one button that opens a link (used for payment links)."""
    msg = {"type": "cta", "body": body[:1024], "button_text": button_text[:20], "url": url}
    return {**msg, "image": image} if image else msg


def image_msg(url: str, caption: str = "") -> dict:
    """A photo by public https link. The caption is kept under `body` so chat history logs it like any other text."""
    return {"type": "image", "url": url, "body": caption[:1024]}


def location_request_msg(body: str) -> dict:
    """A message with a "Send location" button. The user taps it and shares a pin; we get its latitude and longitude."""
    return {"type": "location_request", "body": body[:1024]}


def document_msg(url: str, filename: str, caption: str = "") -> dict:
    """Send a file (e.g. the approved visa) by public link."""
    return {"type": "document", "url": url, "filename": filename, "caption": caption[:1024]}
