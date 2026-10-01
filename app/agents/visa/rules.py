"""Visa rules logic: pure functions, no I/O. Rules come from the `visa_rules` table (indicative data)."""
import calendar
from datetime import date, timedelta

DOC_LABELS = {
    "passport": "Passport first page (photo page)",
    "photo": "Passport-size photo (white background)",
    "bank_statement": "Last 6 months bank statement",
    "itinerary": "Flight tickets / itinerary",
    "hotel_booking": "Hotel booking confirmation",
    "invitation": "Business invitation letter",
}
DOC_TIPS = {
    "passport": "Photo page flat on a table, all four corners visible, no glare.",
    "photo": "Face clearly visible, plain white background, no cap or sunglasses.",
    "bank_statement": "Must show your name and balance, from the last 3 months.",
    "itinerary": "Flight tickets showing your name and dates.",
    "hotel_booking": "Booking confirmation with your name and stay dates.",
    "invitation": "Letter on company letterhead from your host, with dates and purpose.",
}
STATUS_LABEL = {
    "draft": "📝 Draft", "payment_pending": "💳 Awaiting payment", "submitted": "📨 Submitted",
    "in_review": "🔍 In review", "approved": "✅ Approved", "rejected": "❌ Rejected", "cancelled": "🚫 Cancelled",
}
VISA_TYPE_LABEL = {"e-visa": "e-Visa (online)", "sticker": "Sticker visa (embassy / VFS)",
                   "visa-free": "Visa-free", "on-arrival": "Visa on arrival"}


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    year, month = d.year + m // 12, m % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def needs_application(rule: dict) -> bool:
    return rule["visa_type"] in ("e-visa", "sticker")


def passport_issue(expiry: date, travel: date, stay_days: int, min_months: int = 6) -> str | None:
    """Passports must stay valid for `min_months` after the trip ends."""
    needed = add_months(travel + timedelta(days=stay_days), min_months)
    if expiry >= needed:
        return None
    return (f"Your passport expires on {expiry:%d %b %Y}, but it must be valid until {needed:%d %b %Y} "
            f"({min_months} months after your trip). Please renew it before applying.")


def timing_warning(rule: dict, travel: date, today: date) -> str | None:
    if not needs_application(rule):
        return None
    days, need = (travel - today).days, rule["processing_days"]
    if days < need:
        return (f"⚠️ Processing takes about {need} days, but you travel in {days}. "
                "It may not arrive in time. Consider moving your travel date.")
    if days < need + 3:
        return f"⏰ That's tight: processing takes about {need} days and you travel in {days}. Apply today."
    return None


def checklist(required: list[str], docs: list[dict]) -> str:
    """✅/⬜ lines. A document counts once it is verified (or attached by us)."""
    done = {d["doc_type"] for d in docs if d["status"] in ("verified", "auto")}
    return "\n".join(f"{'✅' if code in done else '⬜'} {DOC_LABELS.get(code, code)}" for code in required)


def next_missing(required: list[str], docs: list[dict]) -> str | None:
    done = {d["doc_type"] for d in docs if d["status"] in ("verified", "auto")}
    return next((code for code in required if code not in done), None)
