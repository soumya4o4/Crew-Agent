import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.api.routes import whatsapp as route
from app.core.config import settings
from app.main import app
from app.services.whatsapp_service import WhatsAppService

SECRET = "test-app-secret"
BODY = json.dumps({"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
    "messaging_product": "whatsapp", "metadata": {}, "contacts": [{"profile": {"name": "Aarav"}, "wa_id": "919876543210"}],
    "messages": [{"from": "919876543210", "id": "wamid.1", "timestamp": "1", "type": "text", "text": {"body": "hi"}}]}}]}]}).encode()


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(monkeypatch):
    """The real route with the Concierge and WhatsApp calls replaced, so we can see whether a request got through."""
    handled, sent = [], []

    class FakeConcierge:
        async def handle(self, *args):
            handled.append(args)
            return [{"type": "text", "body": "hello"}]

    async def fake_send(number, msg):
        sent.append((number, msg))

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", SECRET)
    monkeypatch.setattr(route, "get_concierge", lambda: FakeConcierge())
    monkeypatch.setattr(WhatsAppService, "send", staticmethod(fake_send))
    monkeypatch.setattr(WhatsAppService, "mark_read", staticmethod(noop))
    route._seen.clear()
    http = TestClient(app)
    http.handled, http.sent = handled, sent
    return http


def post(client, body=BODY, **headers):
    return client.post("/webhook/whatsapp", content=body, headers={"Content-Type": "application/json", **headers})


def test_a_correctly_signed_message_is_handled(client):
    r = post(client, **{"X-Hub-Signature-256": sign(BODY)})
    assert r.status_code == 200 and r.json() == {"status": "success"}
    assert len(client.handled) == 1 and client.handled[0][3] == "hi" and client.sent == [("919876543210", {"type": "text", "body": "hello"})]


@pytest.mark.parametrize("headers", [
    {},                                                      # no signature at all
    {"X-Hub-Signature-256": "sha256=" + "0" * 64},           # wrong signature
    {"X-Hub-Signature-256": sign(BODY, "someone-elses-secret")},
    {"X-Hub-Signature-256": sign(BODY)[len("sha256="):]},   # missing the "sha256=" prefix
])
def test_forged_or_unsigned_requests_are_rejected_before_anything_runs(client, headers):
    r = post(client, **headers)
    assert r.status_code == 403 and client.handled == [] and client.sent == []


def test_a_signature_for_a_different_body_is_rejected(client):
    tampered = BODY.replace(b'"hi"', b'"forget me"')
    assert post(client, tampered, **{"X-Hub-Signature-256": sign(BODY)}).status_code == 403 and client.handled == []


def test_without_a_configured_secret_everything_is_refused(client, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "")
    assert post(client, **{"X-Hub-Signature-256": sign(BODY, "")}).status_code == 403 and client.handled == []


def test_a_signed_body_that_is_not_json_is_ignored_not_crashed(client):
    junk = b"not json at all"
    r = post(client, junk, **{"X-Hub-Signature-256": sign(junk)})
    assert r.status_code == 200 and r.json() == {"status": "ignored"} and client.handled == []


def test_the_verification_handshake_still_works(client, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_VERIFY_TOKEN", "tok")
    r = client.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "tok", "hub.challenge": "12345"})
    assert r.status_code == 200 and r.text == "12345"


# ---- Meta signs the unicode-escaped form of the payload
EMOJI_BODY = json.dumps({"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
    "messaging_product": "whatsapp", "metadata": {}, "contacts": [{"profile": {"name": "Aarav"}, "wa_id": "919876543210"}],
    "messages": [{"from": "919876543210", "id": "wamid.2", "timestamp": "1", "type": "text", "text": {"body": "नमस्ते 😀 Goa"}}]}}]}]},
    ensure_ascii=False).encode()


def test_escaped_form_of_the_body_matches_what_meta_signs():
    escaped = WhatsAppService._escape_unicode(EMOJI_BODY)
    assert rb"\u0928\u092e" in escaped and rb"\ud83d\ude00" in escaped and all(b < 128 for b in escaped)   # lowercase hex, emoji as a surrogate pair
    assert json.loads(escaped) == json.loads(EMOJI_BODY)   # same data, just written differently


def test_messages_with_hindi_and_emoji_are_accepted_whichever_form_was_signed(client):
    raw_signed, escaped_signed = sign(EMOJI_BODY), sign(WhatsAppService._escape_unicode(EMOJI_BODY))
    for n, signature in enumerate((raw_signed, escaped_signed)):
        route._seen.clear()                                    # the same message again, as if it were a new one
        assert post(client, EMOJI_BODY, **{"X-Hub-Signature-256": signature}).status_code == 200
    assert len(client.handled) == 2 and client.handled[0][3] == "नमस्ते 😀 Goa"
    assert post(client, EMOJI_BODY, **{"X-Hub-Signature-256": sign(EMOJI_BODY, "other")}).status_code == 403   # still needs the secret


def test_the_rejection_log_says_why_without_leaking_anything(client, monkeypatch):
    why = WhatsAppService.signature_diagnosis
    assert "no X-Hub-Signature-256" in why(BODY, "") and "sha256=..." in why(BODY, "abc")
    assert "different Meta app" in why(BODY, sign(BODY, "other")) and SECRET not in why(BODY, sign(BODY, "other"))
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "")
    assert "not set" in why(BODY, sign(BODY))


def test_a_retried_message_is_handled_only_once(client):
    first = post(client, **{"X-Hub-Signature-256": sign(BODY)})
    retry = post(client, **{"X-Hub-Signature-256": sign(BODY)})            # Meta resending because we looked slow
    assert first.json() == {"status": "success"} and retry.json() == {"status": "duplicate"}
    assert len(client.handled) == 1 and len(client.sent) == 1
