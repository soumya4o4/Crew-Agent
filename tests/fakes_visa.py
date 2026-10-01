"""Visa fakes: in-memory VisaRepo (shares users/conversations with the main FakeRepo), a scripted verifier,
and a media downloader that never touches WhatsApp."""
import uuid

from app.agents.visa.verifier import VerifyResult
from app.core.utils import now_ist


class FakeVisaRepo:
    RULES = {
        ("AE", "tourist"): {"country_code": "AE", "country_name": "UAE", "flag": "🇦🇪", "purpose": "tourist", "visa_type": "e-visa",
                            "fee_inr": 6500, "processing_days": 4, "max_stay_days": 30, "passport_min_months": 6,
                            "docs": ["passport", "photo", "itinerary"], "notes": "Single entry."},
        ("GB", "tourist"): {"country_code": "GB", "country_name": "United Kingdom", "flag": "🇬🇧", "purpose": "tourist",
                            "visa_type": "sticker", "fee_inr": 15500, "processing_days": 15, "max_stay_days": 180,
                            "passport_min_months": 6, "docs": ["passport", "photo"], "notes": "Biometrics needed."},
        ("TH", "tourist"): {"country_code": "TH", "country_name": "Thailand", "flag": "🇹🇭", "purpose": "tourist",
                            "visa_type": "visa-free", "fee_inr": 0, "processing_days": 0, "max_stay_days": 60,
                            "passport_min_months": 6, "docs": [], "notes": "Visa-free."},
        ("AE", "business"): {"country_code": "AE", "country_name": "UAE", "flag": "🇦🇪", "purpose": "business", "visa_type": "e-visa",
                             "fee_inr": 7500, "processing_days": 5, "max_stay_days": 30, "passport_min_months": 6,
                             "docs": ["passport", "photo", "itinerary", "invitation"], "notes": "Sponsor details."},
    }

    def __init__(self, core):
        self.core, self.apps, self.docs, self.events, self.files, self.payments = core, {}, {}, [], {}, {}

    def __getattr__(self, name):  # users, conversations, chat history, interests...
        return getattr(self.core, name)

    def list_destinations(self):
        seen = {}
        for (code, _), r in self.RULES.items():
            seen.setdefault(code, {"country_code": code, "country_name": r["country_name"], "flag": r["flag"]})
        return list(seen.values())

    def get_rule(self, code, purpose):
        return self.RULES.get((code, purpose))

    def _with_user(self, app):
        user = next(u for u in self.core.users.values() if u["id"] == app["user_id"])
        return {**app, "users": {"phone": user["phone"], "name": user["name"]}}

    def create_application(self, user_id, rule, travel_date, stay_days):
        app = {"id": str(uuid.uuid4()), "ref": f"VS{len(self.apps) + 1:05d}", "user_id": user_id,
               "country_code": rule["country_code"], "purpose": rule["purpose"], "visa_type": rule["visa_type"],
               "travel_date": travel_date.isoformat(), "stay_days": stay_days, "fee_inr": rule["fee_inr"], "status": "draft",
               "applicant_name": None, "passport_no": None, "passport_expiry": None, "dob": None, "visa_file_path": None,
               "created_at": now_ist().isoformat()}
        self.apps[app["id"]] = app
        self.docs[app["id"]] = []
        return dict(app)

    def get_application(self, app_id, user_id=None):
        app = self.apps.get(app_id)
        return self._with_user(app) if app and (user_id is None or app["user_id"] == user_id) else None

    def get_application_by_ref(self, ref):
        return next((self._with_user(a) for a in self.apps.values() if a["ref"] == ref), None)

    def list_applications(self, user_id, limit=9):
        return [a for a in self.apps.values() if a["user_id"] == user_id]

    def update_application(self, app_id, patch):
        self.apps[app_id].update(patch)
        return self.apps[app_id]

    def active_applications(self):
        return [self._with_user(a) for a in self.apps.values() if a["status"] in ("submitted", "in_review")]

    def add_document(self, app_id, doc_type, storage_path, mime, status, issue=None, extracted=None):
        doc = {"id": str(uuid.uuid4()), "doc_type": doc_type, "storage_path": storage_path, "mime": mime,
               "status": status, "issue": issue, "extracted": extracted or {}}
        self.docs[app_id].append(doc)
        return doc

    def set_document_status(self, doc_id, status):
        for docs in self.docs.values():
            for d in docs:
                if d["id"] == doc_id:
                    d["status"] = status

    def list_documents(self, app_id):
        return list(self.docs[app_id])

    def log_event(self, app_id, status, note=None):
        self.events.append({"application_id": app_id, "status": status, "note": note, "created_at": now_ist().isoformat()})

    def list_events(self, app_id):
        return [e for e in self.events if e["application_id"] == app_id]

    def upload_file(self, path, data, mime):
        self.files[path] = (data, mime)

    def signed_url(self, path, seconds=86400):
        return path if path.startswith("http") else f"https://files.example/{path}"

    def create_payment(self, app_id, user_id, amount, link_id, short_url, expires_at):
        self.payments[link_id] = {"app_id": app_id, "status": "created"}

    def cancel_payments(self, app_id):
        for p in self.payments.values():
            if p["app_id"] == app_id and p["status"] == "created":
                p["status"] = "cancelled"

    def mark_paid(self, link_id):
        p = self.payments.get(link_id)
        if not p or p["status"] != "created":
            return None
        p["status"] = "paid"
        app = self.apps[p["app_id"]]
        if app["status"] not in ("draft", "payment_pending"):
            return None
        app["status"] = "submitted"
        self.log_event(app["id"], "submitted", "Payment received.")
        return self._with_user(app)


class FakeVerifier:
    """Queue results with .queue.append(VerifyResult(...)); otherwise everything 'looks fine'."""

    def __init__(self):
        self.queue, self.calls = [], []

    async def verify(self, doc_type, data, mime):
        self.calls.append(doc_type)
        if self.queue:
            return self.queue.pop(0)
        fields = {"name": "Rahul Mehta", "passport_no": "Z1234567", "dob": "1990-05-12", "expiry": "2033-01-01"} if doc_type == "passport" else {}
        return VerifyResult(True, [], fields)


async def fake_fetch_media(media_id):
    return b"x" * 40_000, "image/jpeg"
