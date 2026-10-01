import asyncio
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from fakes import Chat, FakeGateway, WA
from app.agents.visa import rules as R
from app.agents.visa.verifier import VerifyResult
from app.core.utils import now_ist
from app.services.whatsapp_service import WhatsAppService

NUMBER = "+" + WA


# ---------------------------------------------------------------- pure rules
def test_passport_must_outlive_the_trip_by_six_months():
    travel = date(2026, 11, 1)
    assert R.passport_issue(date(2027, 6, 1), travel, 15) is None            # valid until after 30 May 2027
    msg = R.passport_issue(date(2027, 3, 1), travel, 15)
    assert "expires on 01 Mar 2027" in msg and "valid until 16 May 2027" in msg
    assert R.add_months(date(2026, 8, 31), 6) == date(2027, 2, 28)           # month ends don't overflow


def test_timing_warning_only_for_visas_that_need_processing():
    rule = {"visa_type": "e-visa", "processing_days": 5}
    today = date(2026, 10, 1)
    assert "may not arrive in time" in R.timing_warning(rule, today + timedelta(days=2), today)
    assert "tight" in R.timing_warning(rule, today + timedelta(days=6), today)
    assert R.timing_warning(rule, today + timedelta(days=30), today) is None
    assert R.timing_warning({"visa_type": "visa-free", "processing_days": 0}, today, today) is None


def test_checklist_and_next_missing():
    docs = [{"doc_type": "passport", "status": "verified"}, {"doc_type": "photo", "status": "rejected"}]
    assert R.next_missing(["passport", "photo", "itinerary"], docs) == "photo"
    assert R.checklist(["passport", "photo"], docs).splitlines()[0].startswith("✅")


# ---------------------------------------------------------------- the conversation
def start_uae(c, days=14):
    c.send("visa for dubai")
    c.send(reply_id="vpur:tourist")
    c.send(reply_id=f"vdate:{(now_ist().date() + timedelta(days=days)).isoformat()}")
    c.send(reply_id="vstay:15")
    return c.send(reply_id="vgo:start")


def upload_all(c, n=None):
    """Passport (+ confirm), photo, itinerary."""
    c.send_file()
    c.send(reply_id="vdoc:ok")
    c.send_file()
    return c.send_file()


def test_visa_free_country_needs_no_application():
    c = Chat()
    assert c.send("do I need a visa for thailand")["type"] == "buttons"  # asks the purpose first
    c.send(reply_id="vpur:tourist")
    c.send(reply_id=c.ids()[1])  # a date
    assert "No visa needed" in c.last[0]["body"] and "vgo:start" not in c.ids()


def test_visa_free_when_asked_from_the_menu():
    c = Chat()
    c.send("hi"); c.send(reply_id="svc:visa"); c.send(reply_id="visa:new")
    assert c.ids()[0] == "vdest:AE" and "vdest:other" not in c.ids()  # 3 countries in the fake rules
    c.send(reply_id="vdest:TH"); c.send(reply_id="vpur:tourist"); c.send(reply_id=c.ids()[0])
    assert "No visa needed" in c.last[0]["body"]


def test_full_application_journey():
    gw = FakeGateway()
    c = Chat(gateway=gw)
    c.send("visa for dubai")
    assert c.last[0]["type"] == "buttons" and c.ids() == ["vpur:tourist", "vpur:business"]
    c.send(reply_id="vpur:tourist")
    assert c.last[0]["type"] == "list" and c.ids()[0].startswith("vdate:")
    c.send(reply_id=f"vdate:{(now_ist().date() + timedelta(days=14)).isoformat()}")
    assert c.ids() == ["vstay:7", "vstay:15", "vstay:30"]
    rules = c.send(reply_id="vstay:15")
    assert "e-Visa (online)" in rules["body"] and "₹6,500" in rules["body"] and "• Passport first page" in rules["body"]
    assert "vgo:start" in c.ids()

    ask = c.send(reply_id="vgo:start")
    assert "Passport first page" in ask["body"] and "⬜" in ask["body"]
    read = c.send_file()
    assert "Z1234567" in read["body"] and c.ids() == ["vdoc:ok", "vdoc:redo"]
    nxt = c.send(reply_id="vdoc:ok")
    assert "✅ *Passport* confirmed" in nxt["body"] and "Passport-size photo" in nxt["body"]
    assert "looks good" in c.send_file()["body"]               # photo
    review = c.send_file()                                      # itinerary
    assert "All documents are in" in review["body"] and c.ids() == ["vpay:pay", "vcan:ask"]

    pay = c.send(reply_id="vpay:pay")
    assert pay["type"] == "cta" and pay["button_text"] == "Pay ₹6,500" and gw.links["plink_1"]["ref"].startswith("VS")
    assert "haven't received" in c.send(reply_id="vpay:check")["body"]
    gw.paid.add("plink_1")
    done = c.send(reply_id="vpay:check")
    assert "Application submitted" in done["body"]
    app = next(iter(c.visa_repo.apps.values()))
    assert app["status"] == "submitted" and app["passport_no"] == "Z1234567" and app["applicant_name"] == "Rahul Mehta"
    assert len(c.visa_repo.files) == 3                          # all three uploads were stored


def test_a_bad_document_is_rejected_with_the_reason_and_asked_again():
    c = Chat()
    start_uae(c)
    c.verifier.queue.append(VerifyResult(False, ["Some of it is hard to read."]))
    out = c.send_file()
    assert "can't accept" in out["body"] and "hard to read" in out["body"]
    assert c.visa_repo.docs[next(iter(c.visa_repo.apps))][0]["status"] == "rejected"
    assert "Passport first page" in c.send("ok")["body"] or "photo or PDF" in c.last[0]["body"]  # still waiting for it
    assert "Z1234567" in c.send_file()["body"]                  # the retry works


def test_passport_expiring_too_soon_blocks_the_application():
    c = Chat()
    start_uae(c, days=14)
    c.verifier.queue.append(VerifyResult(True, [], {"name": "Rahul Mehta", "passport_no": "Z1", "dob": "1990-01-01",
                                                    "expiry": (now_ist().date() + timedelta(days=90)).isoformat()}))
    out = c.send_file()
    assert "can't accept" in out["body"] and "expires on" in out["body"] and "renew" in out["body"]


def test_redo_asks_for_a_clearer_passport_photo():
    c = Chat()
    start_uae(c)
    c.send_file()
    assert "clearer photo" in c.send(reply_id="vdoc:redo")["body"]
    assert "Z1234567" in c.send_file()["body"]


def test_flight_booking_is_attached_as_the_itinerary():
    c = Chat()
    c.send("hi")
    c.repo.convos[NUMBER]["context"]["trip"] = {"from": "BOM", "to": "DXB", "city": "Dubai",
                                                 "date": (now_ist().date() + timedelta(days=20)).isoformat()}
    out = c.send(reply_id="svc:visa")
    assert "vdest:AE" in c.ids() and "flying to *Dubai*" in out["body"]
    c.send(reply_id="vdest:AE"); c.send(reply_id="vpur:tourist")
    assert c.ids()[0].startswith("vdate:") and "My flight date" in c.last[0]["rows"][0][1]
    c.send(reply_id=c.ids()[0]); c.send(reply_id="vstay:7"); c.send(reply_id="vgo:start")
    c.send_file(); c.send(reply_id="vdoc:ok")
    review = c.send_file()                                      # photo was the last one: itinerary is already there
    assert "All documents are in" in review["body"] and c.verifier.calls == ["passport", "photo"]


def test_too_tight_timing_is_flagged():
    c = Chat()
    c.send("visa for dubai"); c.send(reply_id="vpur:tourist")
    c.send(reply_id=f"vdate:{(now_ist().date() + timedelta(days=2)).isoformat()}"); c.send(reply_id="vstay:7")
    assert "may not arrive in time" in c.last[0]["body"]


def test_typed_country_and_date_and_unknown_country():
    c = Chat()
    c.send("hi"); c.send(reply_id="svc:visa"); c.send(reply_id="visa:new")
    c.send(reply_id="vdest:AE")
    c.send(reply_id="vpur:business")
    c.send(reply_id="vdate:more")
    assert "couldn't read that date" in c.send("whenever")["body"]
    assert c.send("20/12")["type"] == "buttons"                  # stay choice


def test_pause_and_resume_from_my_applications():
    c = Chat()
    start_uae(c)
    c.send_file(); c.send(reply_id="vdoc:ok")
    c.send(reply_id="vdoc:pause")
    assert "Saved" in c.last[0]["body"]
    apps = c.send(reply_id="visa:apps")
    assert apps["rows"][0][1].startswith("VS") and "Draft" in apps["rows"][0][2]
    detail = c.send(reply_id=apps["rows"][0][0])
    assert c.ids()[0].startswith("vres:")
    resumed = c.send(reply_id=c.ids()[0])
    assert "Continuing" in resumed["body"] and "Passport-size photo" in resumed["body"]


def test_cancelling_a_draft():
    c = Chat()
    start_uae(c)
    ask = c.send(reply_id="vcan:ask")
    assert "Cancel application" in ask["body"]
    done = c.send(reply_id=c.ids()[0])
    assert "is cancelled" in done["body"] and next(iter(c.visa_repo.apps.values()))["status"] == "cancelled"


def test_without_razorpay_keys_the_application_is_submitted_straight_away():
    c = Chat()                                                    # no gateway
    start_uae(c); upload_all(c)
    assert "Application submitted" in c.send(reply_id="vpay:pay")["body"]
    assert next(iter(c.visa_repo.apps.values()))["status"] == "submitted"


def test_webhook_submits_the_application_once():
    gw = FakeGateway()
    c = Chat(gateway=gw)
    start_uae(c); upload_all(c)
    c.send(reply_id="vpay:pay")
    number, messages = asyncio.run(c.concierge.confirm_payment("plink_1"))
    assert number == WA and "Application submitted" in messages[0]["body"]
    assert asyncio.run(c.concierge.confirm_payment("plink_1")) is None
    assert "visa" not in c.repo.convos[NUMBER]["context"]
    assert asyncio.run(c.concierge.confirm_payment("plink_unknown")) is None


# ------------------------------------------------------ the visa team's side
def submitted_app(c):
    start_uae(c); upload_all(c)
    c.send(reply_id="vpay:pay")
    return next(iter(c.visa_repo.apps.values()))


def test_status_updates_notify_the_user_and_approved_visa_can_be_downloaded():
    c = Chat()
    app = submitted_app(c)
    visa = c.concierge.agents["visa"]
    number, msgs = asyncio.run(visa.update_status(app["ref"], "in_review", "Documents received"))
    assert number == WA and "under review" in msgs[0]["body"]
    number, msgs = asyncio.run(visa.update_status(app["ref"], "approved", None, "https://files.example/visa.pdf"))
    assert "Visa approved" in msgs[0]["body"] and f"vfile:{app['id']}" in [i for i, _ in msgs[0]["buttons"]]
    doc = c.send(reply_id=f"vfile:{app['id']}")
    assert doc["type"] == "document" and doc["url"] == "https://files.example/visa.pdf"
    detail = c.send(reply_id=f"vapp:{app['id']}")
    assert "Approved" in detail["body"] and "Submitted" in detail["body"] and "In review" in detail["body"]
    with pytest.raises(ValueError):
        asyncio.run(visa.update_status(app["ref"], "in_review"))  # can't go backwards
    with pytest.raises(LookupError):
        asyncio.run(visa.update_status("VSNOPE", "approved"))


def test_rejection_includes_the_reason():
    c = Chat()
    app = submitted_app(c)
    _, msgs = asyncio.run(c.concierge.agents["visa"].update_status(app["ref"], "rejected", "Bank statement unclear"))
    assert "wasn't approved" in msgs[0]["body"] and "Bank statement unclear" in msgs[0]["body"]


def test_demo_mode_walks_the_application_forward():
    c = Chat()
    app = submitted_app(c)
    visa = c.concierge.agents["visa"]
    assert asyncio.run(visa.advance_demo()) == []                 # too early
    for e in c.visa_repo.events:
        e["created_at"] = (now_ist() - timedelta(seconds=120)).isoformat()
    assert "under review" in asyncio.run(visa.advance_demo())[0][1][0]["body"]
    c.visa_repo.events[-1]["created_at"] = (now_ist() - timedelta(minutes=10)).isoformat()
    assert "Visa approved" in asyncio.run(visa.advance_demo())[0][1][0]["body"]
    assert c.visa_repo.apps[app["id"]]["status"] == "approved"


def test_admin_api_needs_the_token(monkeypatch):
    from app.api.routes import admin
    from app.core.config import settings
    from app.main import app as api

    c = Chat()
    app = submitted_app(c)
    sent = []

    async def fake_send(number, msg):
        sent.append((number, msg["body"]))

    monkeypatch.setattr(admin, "get_concierge", lambda: c.concierge)
    monkeypatch.setattr(WhatsAppService, "send", staticmethod(fake_send))
    client = TestClient(api)
    url = f"/admin/visa/{app['ref']}/status"
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "")
    assert client.post(url, json={"status": "in_review"}).status_code == 503
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "s3cret")
    assert client.post(url, json={"status": "in_review"}).status_code == 401
    assert client.post(url, json={"status": "in_review"}, headers={"X-Admin-Token": "wrong"}).status_code == 401
    ok = client.post(url, json={"status": "in_review", "note": "x"}, headers={"X-Admin-Token": "s3cret"})
    assert ok.status_code == 200 and sent and "under review" in sent[0][1]
    assert client.post(f"/admin/visa/VSNOPE/status", json={"status": "approved"}, headers={"X-Admin-Token": "s3cret"}).status_code == 404
    assert client.post(url, json={"status": "submitted"}, headers={"X-Admin-Token": "s3cret"}).status_code == 409


# --------------------------------------------------------------- media plumbing
def test_files_outside_the_visa_flow_get_a_polite_answer():
    c = Chat()
    out = c.send_file()
    assert "can't use files" in out["body"] and out["type"] == "list"


def test_only_photos_and_pdfs_are_accepted():
    c = Chat()
    start_uae(c)
    assert "photos (JPG/PNG) and PDFs" in c.send_file(mime="video/mp4")["body"]
    assert "Z1234567" in c.send_file(mime="application/pdf", kind="document")["body"]


def test_extract_message_data_reads_images_and_documents():
    def payload(kind, body):
        return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {}, "contacts": [{"wa_id": "919", "profile": {"name": "Rahul"}}],
            "messages": [{"from": "919", "id": "wamid.1", "timestamp": "1", "type": kind, kind: body}]}}]}]}

    img = WhatsAppService.extract_message_data(payload("image", {"id": "m1", "mime_type": "image/jpeg", "caption": "my passport"}))
    assert img["media"] == {"id": "m1", "mime": "image/jpeg", "filename": "", "kind": "image"} and img["text"] == "my passport"
    doc = WhatsAppService.extract_message_data(payload("document", {"id": "m2", "mime_type": "application/pdf", "filename": "bank.pdf"}))
    assert doc["media"]["filename"] == "bank.pdf" and doc["media"]["kind"] == "document"
    assert WhatsAppService.extract_message_data(payload("audio", {"id": "m3"})) is None
