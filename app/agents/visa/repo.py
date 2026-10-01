"""Visa agent data access: rules, applications, documents, timeline, file storage, payments."""
import random
import string
from datetime import date, datetime

from app.db.repository import CoreRepo

BUCKET = "visa-docs"
POPULAR = ["AE", "SG", "TH", "GB", "US", "SCHENGEN", "JP", "VN", "ID"]  # shown first; the rest can be typed


class VisaRepo(CoreRepo):
    # ---- rules
    def list_destinations(self) -> list[dict]:
        rows = self.db.table("visa_rules").select("country_code, country_name, flag").execute().data
        unique = {r["country_code"]: r for r in rows}
        order = {c: i for i, c in enumerate(POPULAR)}
        return sorted(unique.values(), key=lambda r: (order.get(r["country_code"], 99), r["country_name"]))

    def get_rule(self, country_code: str, purpose: str) -> dict | None:
        rows = (self.db.table("visa_rules").select("*").eq("country_code", country_code)
                .eq("purpose", purpose).limit(1).execute().data)
        return rows[0] if rows else None

    # ---- applications
    def create_application(self, user_id: str, rule: dict, travel_date: date, stay_days: int) -> dict:
        for _ in range(5):
            ref = "VS" + "".join(random.choices(string.ascii_uppercase + string.digits, k=5))
            try:
                return self.db.table("visa_applications").insert({
                    "ref": ref, "user_id": user_id, "country_code": rule["country_code"], "purpose": rule["purpose"],
                    "visa_type": rule["visa_type"], "travel_date": travel_date.isoformat(), "stay_days": stay_days,
                    "fee_inr": rule["fee_inr"],
                }).execute().data[0]
            except Exception:
                continue  # ref collision
        raise RuntimeError("Could not create application")

    def get_application(self, app_id: str, user_id: str | None = None) -> dict | None:
        q = self.db.table("visa_applications").select("*, users(phone, name)").eq("id", app_id)
        rows = (q.eq("user_id", user_id) if user_id else q).limit(1).execute().data
        return rows[0] if rows else None

    def get_application_by_ref(self, ref: str) -> dict | None:
        rows = self.db.table("visa_applications").select("*, users(phone, name)").eq("ref", ref).limit(1).execute().data
        return rows[0] if rows else None

    def list_applications(self, user_id: str, limit: int = 9) -> list[dict]:
        return (self.db.table("visa_applications").select("*").eq("user_id", user_id)
                .order("created_at", desc=True).limit(limit).execute().data)

    def update_application(self, app_id: str, patch: dict) -> dict:
        return self.db.table("visa_applications").update(patch).eq("id", app_id).execute().data[0]

    def active_applications(self) -> list[dict]:
        return (self.db.table("visa_applications").select("*, users(phone, name)")
                .in_("status", ["submitted", "in_review"]).limit(100).execute().data)

    # ---- documents and timeline
    def add_document(self, app_id: str, doc_type: str, storage_path: str | None, mime: str | None, status: str,
                     issue: str | None = None, extracted: dict | None = None) -> dict:
        return self.db.table("visa_documents").insert({
            "application_id": app_id, "doc_type": doc_type, "storage_path": storage_path, "mime": mime,
            "status": status, "issue": issue, "extracted": extracted or {},
        }).execute().data[0]

    def set_document_status(self, doc_id: str, status: str) -> None:
        self.db.table("visa_documents").update({"status": status}).eq("id", doc_id).execute()

    def list_documents(self, app_id: str) -> list[dict]:
        return (self.db.table("visa_documents").select("*").eq("application_id", app_id)
                .order("created_at").execute().data)

    def log_event(self, app_id: str, status: str, note: str | None = None) -> None:
        self.db.table("visa_events").insert({"application_id": app_id, "status": status, "note": note}).execute()

    def list_events(self, app_id: str) -> list[dict]:
        return (self.db.table("visa_events").select("*").eq("application_id", app_id)
                .order("created_at").execute().data)

    # ---- files
    def upload_file(self, path: str, data: bytes, mime: str) -> None:
        self.db.storage.from_(BUCKET).upload(path, data, {"content-type": mime or "application/octet-stream"})

    def signed_url(self, path: str, seconds: int = 86400) -> str:
        if path.startswith("http"):  # ops can also attach an external link
            return path
        return self.db.storage.from_(BUCKET).create_signed_url(path, seconds)["signedURL"]

    # ---- payments (shared `payments` table, kind = 'visa')
    def create_payment(self, app_id: str, user_id: str, amount_inr: int, link_id: str, short_url: str,
                       expires_at: datetime) -> dict:
        return self.db.table("payments").insert({
            "kind": "visa", "visa_application_id": app_id, "user_id": user_id, "amount_inr": amount_inr,
            "link_id": link_id, "short_url": short_url, "expires_at": expires_at.isoformat(),
        }).execute().data[0]

    def cancel_payments(self, app_id: str) -> None:
        self.db.table("payments").update({"status": "cancelled"}).eq("visa_application_id", app_id) \
            .eq("kind", "visa").eq("status", "created").execute()

    def mark_paid(self, link_id: str) -> dict | None:
        """Payment received: the application is now submitted. Returns it, or None if already handled."""
        paid = (self.db.table("payments").update({"status": "paid", "paid_at": datetime.now().astimezone().isoformat()})
                .eq("link_id", link_id).eq("kind", "visa").eq("status", "created").execute().data)
        if not paid:
            return None
        app_id = paid[0]["visa_application_id"]
        moved = (self.db.table("visa_applications").update({"status": "submitted"})
                 .eq("id", app_id).in_("status", ["draft", "payment_pending"]).execute().data)
        if not moved:
            return None
        self.log_event(app_id, "submitted", "Payment received. Application handed to our visa team.")
        return self.get_application(app_id)
