"""Visa agent: check requirements, collect and verify documents on WhatsApp, take the fee, track the application.

Button ids: visa:*, vdest:, vpur:, vdate:, vstay:, vgo:, vdoc:, vapp:, vres:, vfile:, vpay:, vcan:
State lives in ctx["visa"]. After payment the application is handed to our visa team (status `submitted`); they
move it forward through the admin API (see app/api/routes/admin.py) and this agent messages the user on each change.
"""
import asyncio
import logging
import mimetypes
import time
from datetime import date, datetime, timedelta

from app.agents.base import Agent, Session
from app.agents.concierge.slots import find_country
from app.agents.visa import rules as R
from app.agents.visa.verifier import BasicVerifier
from app.core.messages import buttons_msg, cta_msg, document_msg, list_msg, text_msg
from app.core.places import COUNTRY_NAMES, country_of, is_international, visa_code_for
from app.core.utils import IST, inr, now_ist, parse_date, to_ist

logger = logging.getLogger(__name__)
PAYMENT_WINDOW_MIN = 20
DEMO_REVIEW_AFTER_S, DEMO_APPROVE_AFTER_S = 90, 240


def _fmt(d: date | str) -> str:
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d:%a, %d %b %Y}"


def trip_visa_code(trip: dict, to: str | None = None) -> str | None:
    """The visa_rules country of an international trip, worked out from the destination airport's country (no fixed list)."""
    if to:
        return visa_code_for(country_of(to))
    if not trip.get("to") or not is_international(trip.get("from", ""), trip["to"]):  # (older saved trips have no "intl")
        return None
    return visa_code_for(trip.get("country") or country_of(trip["to"]))


class VisaAgent(Agent):
    name = "visa"
    title = "Visa"
    emoji = "🛂"
    menu_desc = "Check rules, apply & track"
    owns = frozenset({"visa", "vdest", "vpur", "vdate", "vstay", "vgo", "vdoc", "vapp", "vres", "vfile", "vpay", "vcan"})

    def __init__(self, repo, payments=None, verifier=None, fetch_media=None):
        self.repo = repo                              # VisaRepo
        self.payments = payments                      # RazorpayGateway (None: applications submit without payment)
        self.verifier = verifier or BasicVerifier()
        self._fetch_media = fetch_media               # async (media_id) -> (bytes, mime)

    async def _db(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    async def fetch_media(self, media_id: str) -> tuple[bytes, str]:
        if self._fetch_media:
            return await self._fetch_media(media_id)
        from app.services.whatsapp_service import WhatsAppService
        return await WhatsAppService.download_media(media_id)

    def reset(self, s: Session) -> None:
        s.ctx.pop("visa", None)

    def expects_text(self, s: Session) -> bool:
        return s.step in ("awaiting_visa_country", "awaiting_visa_date", "awaiting_visa_doc")

    def expects_media(self, s: Session) -> bool:
        return s.step == "awaiting_visa_doc"

    # ------------------------------------------------------------------ entry points
    async def on_enter(self, s: Session) -> list[dict]:
        self.reset(s)
        s.step = "visa_menu"
        buttons = [("visa:new", "🛂 Check & Apply"), ("visa:apps", "📋 My Applications")]
        body = ("🛂 *Visa*\nI'll tell you if you need a visa, what documents to send, and handle the application "
                "from start to finish, right here on WhatsApp.")
        trip = s.ctx.get("trip") or {}
        if (code := trip_visa_code(trip)):
            body += f"\n\n✈️ You're flying to *{trip['city']}* on {_fmt(trip['date'])}. Shall I check the visa for it?"
            buttons.insert(0, (f"vdest:{code}", f"✅ {COUNTRY_NAMES[code]} visa"))
        return [buttons_msg(body, buttons)]

    async def start(self, s: Session, slots: dict) -> list[dict]:
        """Free text like "visa for dubai" / "thailand visa": skip the destination question."""
        self.reset(s)
        code = slots.get("country") or trip_visa_code({}, slots.get("to"))
        known = {d["country_code"] for d in await self._db(self.repo.list_destinations)}
        if code in known:
            s.ctx["visa"] = {"country": code}
            return self._ask_purpose(s)
        if code:  # a country we don't cover yet
            return [buttons_msg(f"I don't have visa rules for *{COUNTRY_NAMES.get(code, code)}* yet. Want to check another country?",
                                [("visa:new", "🔎 Another country"), ("nav:menu", "🏠 Menu")])]
        return await self._ask_dest(s)

    async def process(self, s: Session, text: str, reply_id: str | None) -> list[dict]:
        if not reply_id:
            if s.step == "awaiting_visa_country":
                return await self._on_country_text(s, text)
            if s.step == "awaiting_visa_date":
                return await self._on_date_text(s, text)
            if s.step == "awaiting_visa_doc":
                return [buttons_msg("📎 Please send the document as a *photo or PDF*.",
                                    [("vdoc:list", "📋 Checklist"), ("vdoc:pause", "⏸️ Continue later"), ("vcan:ask", "❌ Cancel")])]
            return await self.on_enter(s)
        kind, _, val = reply_id.partition(":")
        c = s.ctx.setdefault("visa", {})
        if kind == "visa":
            if val == "new":
                return await self._ask_dest(s)
            if val == "apps":
                return await self._show_apps(s)
        elif kind == "vdest":
            if val == "other":
                s.step = "awaiting_visa_country"
                return [text_msg("🌍 Type the country, like *Japan* or *Vietnam*.")]
            c.clear()
            c["country"] = val
            return self._ask_purpose(s)
        elif kind == "vpur" and c.get("country"):
            c["purpose"] = val
            return self._ask_date(s)
        elif kind == "vdate" and c.get("purpose"):
            if val == "more":
                s.step = "awaiting_visa_date"
                return [text_msg("📅 Type your travel date, like *15/11* or *20 Nov*.")]
            return await self._after_date(s, date.fromisoformat(val))
        elif kind == "vstay" and c.get("travel_date"):
            c["stay_days"] = int(val)
            return await self._show_rules(s)
        elif kind == "vgo":
            if val == "start" and c.get("travel_date"):
                return await self._start_application(s)
            if val == "another":
                return await self._ask_dest(s)
        elif kind == "vdoc" and c.get("app_id"):
            return await self._on_doc_tap(s, val)
        elif kind == "vapp":
            return await self._show_app(s, val)
        elif kind == "vres":
            return await self._resume(s, val)
        elif kind == "vfile":
            return await self._send_visa(s, val)
        elif kind == "vpay" and c.get("app_id"):
            return await self._on_pay_tap(s, val)
        elif kind == "vcan":
            return await self._on_cancel(s, val)
        return await self.on_enter(s)

    # -------------------------------------------------------------- questions
    async def _ask_dest(self, s: Session) -> list[dict]:
        s.ctx.pop("visa", None)
        s.step = "awaiting_visa_dest"
        dests = await self._db(self.repo.list_destinations)
        rows = [(f"vdest:{d['country_code']}", f"{d['flag']} {d['country_name']}", "") for d in dests[:9]]
        if len(dests) > 9:
            rows.append(("vdest:other", "🌍 Another country…", "Type its name"))
        return [list_msg("🌍 *Which country are you travelling to?*", "Choose country", rows, "Destinations")]

    async def _on_country_text(self, s: Session, text: str) -> list[dict]:
        code = find_country(text) or next((c for c, n in COUNTRY_NAMES.items() if n.lower() == text.strip().lower()), None)
        known = {d["country_code"] for d in await self._db(self.repo.list_destinations)}
        if code not in known:
            names = ", ".join(COUNTRY_NAMES[c] for c in COUNTRY_NAMES if c in known)
            return [text_msg(f"😕 I don't have rules for that one yet. I cover: {names}.")]
        s.ctx["visa"] = {"country": code}
        return self._ask_purpose(s)

    def _ask_purpose(self, s: Session) -> list[dict]:
        s.step = "awaiting_visa_purpose"
        name = COUNTRY_NAMES.get(s.ctx["visa"]["country"], s.ctx["visa"]["country"])
        return [buttons_msg(f"✈️ *{name}*, great choice! What's the purpose of your trip?",
                            [("vpur:tourist", "🏖️ Tourism"), ("vpur:business", "💼 Business")])]

    def _ask_date(self, s: Session) -> list[dict]:
        s.step = "awaiting_visa_date"
        today, rows = now_ist().date(), []
        trip = s.ctx.get("trip") or {}
        if trip_visa_code(trip) == s.ctx["visa"]["country"] and date.fromisoformat(trip["date"]) > today:
            rows.append((f"vdate:{trip['date']}", "✈️ My flight date", _fmt(trip["date"])))
        for label, days in (("In 1 week", 7), ("In 2 weeks", 14), ("In 3 weeks", 21), ("In 1 month", 30), ("In 2 months", 60)):
            d = today + timedelta(days=days)
            rows.append((f"vdate:{d.isoformat()}", f"{label} · {d:%d %b}", ""))
        rows.append(("vdate:more", "Another date…", "Type a date"))
        return [list_msg("📅 *When do you plan to travel?* (or type a date, e.g. 15/11)", "Choose date", rows, "Travel date")]

    async def _on_date_text(self, s: Session, text: str) -> list[dict]:
        if not s.ctx.get("visa", {}).get("purpose"):
            return await self.on_enter(s)
        today = now_ist().date()
        d = parse_date(text, today)
        if d is None or d < today or d > today + timedelta(days=365):
            return [text_msg("😕 I couldn't read that date. Try *15/11* or *20 Nov* (within the next year).")]
        return await self._after_date(s, d)

    async def _after_date(self, s: Session, travel: date) -> list[dict]:
        c = s.ctx["visa"]
        c["travel_date"] = travel.isoformat()
        rule = await self._db(self.repo.get_rule, c["country"], c["purpose"])
        if rule and R.needs_application(rule):
            s.step = "awaiting_visa_stay"
            stays = [d for d in (7, 15, 30) if d <= rule["max_stay_days"]] or [rule["max_stay_days"]]
            return [buttons_msg("🗓️ How long will you stay?", [(f"vstay:{d}", f"Up to {d} days") for d in stays])]
        c["stay_days"] = min(15, rule["max_stay_days"]) if rule else 15
        return await self._show_rules(s)

    # ----------------------------------------------------------------- rules
    async def _show_rules(self, s: Session) -> list[dict]:
        c = s.ctx["visa"]
        rule = await self._db(self.repo.get_rule, c["country"], c["purpose"])
        if not rule:
            return [buttons_msg("😕 I don't have rules for that combination yet.", [("visa:new", "🔎 Another country"), ("nav:menu", "🏠 Menu")])]
        travel, today = date.fromisoformat(c["travel_date"]), now_ist().date()
        head = f"{rule['flag']} *{rule['country_name']}* · {'Tourism' if rule['purpose'] == 'tourist' else 'Business'}"
        s.step = "visa_rules"
        disclaimer = "\n\n_Rules and fees change often. This is indicative, and the embassy has the final say._"
        if not R.needs_application(rule):
            free = rule["visa_type"] == "visa-free"
            text = (f"{head}\n\n{'🎉 *No visa needed!*' if free else '🛬 *Visa on arrival*'}\n"
                    f"📅 Stay up to {rule['max_stay_days']} days\n"
                    + (f"💰 About {inr(rule['fee_inr'])} payable at the airport\n" if rule["fee_inr"] else "")
                    + f"📘 Keep your passport valid for {rule['passport_min_months']}+ months\n\nℹ️ {rule['notes']}" + disclaimer)
            return [text_msg(text), buttons_msg("Anything else I can check?", [("visa:new", "🔎 Check another"), ("nav:menu", "🏠 Menu")])]
        docs = "\n".join(f"• {R.DOC_LABELS.get(d, d)}" for d in rule["docs"])
        warn = R.timing_warning(rule, travel, today)
        text = (f"{head}\n\n🛂 *{R.VISA_TYPE_LABEL[rule['visa_type']]}*\n💰 {inr(rule['fee_inr'])} (government + service fee)\n"
                f"⏱ About {rule['processing_days']} days\n📅 Stay up to {rule['max_stay_days']} days\n"
                f"📘 Passport valid {rule['passport_min_months']}+ months after your trip\n\n📄 *Documents needed*\n{docs}\n\n"
                f"ℹ️ {rule['notes']}" + (f"\n\n{warn}" if warn else "") + disclaimer)
        return [text_msg(text), buttons_msg("Shall I start your application? I'll check every document for you. ✅",
                                            [("vgo:start", "✅ Start application"), ("vgo:another", "🔎 Check another"), ("nav:menu", "🏠 Menu")])]

    # ----------------------------------------------------------- application
    async def _start_application(self, s: Session) -> list[dict]:
        c = s.ctx["visa"]
        rule = await self._db(self.repo.get_rule, c["country"], c["purpose"])
        app = await self._db(self.repo.create_application, s.user["id"], rule, date.fromisoformat(c["travel_date"]), c["stay_days"])
        await self._db(self.repo.log_event, app["id"], "draft", "Application started")
        c["app_id"] = app["id"]
        trip = s.ctx.get("trip") or {}
        if "itinerary" in rule["docs"] and trip_visa_code(trip) == c["country"]:  # we already hold the flight
            await self._db(self.repo.add_document, app["id"], "itinerary", None, None, "auto", None, trip)
        return await self._ask_next_doc(s, f"📝 Application *{app['ref']}* started.\n\n")

    async def _load(self, s: Session) -> tuple[dict, dict, list[dict]]:
        app = await self._db(self.repo.get_application, s.ctx["visa"]["app_id"], s.user["id"])
        rule = await self._db(self.repo.get_rule, app["country_code"], app["purpose"])
        docs = await self._db(self.repo.list_documents, app["id"])
        return app, rule, docs

    async def _ask_next_doc(self, s: Session, prefix: str = "") -> list[dict]:
        app, rule, docs = await self._load(s)
        missing = R.next_missing(rule["docs"], docs)
        if missing is None:
            return await self._review(s, prefix)
        s.ctx["visa"]["pending_doc"] = missing
        s.step = "awaiting_visa_doc"
        return [buttons_msg(
            f"{prefix}{R.checklist(rule['docs'], docs)}\n\n📎 Please send your *{R.DOC_LABELS.get(missing, missing)}* now (photo or PDF).\n"
            f"💡 {R.DOC_TIPS.get(missing, '')}",
            [("vdoc:list", "📋 Checklist"), ("vdoc:pause", "⏸️ Continue later"), ("vcan:ask", "❌ Cancel")])]

    async def on_media(self, s: Session, media: dict, text: str) -> list[dict]:
        c = s.ctx.get("visa", {})
        pending = c.get("pending_doc")
        if not pending or not c.get("app_id"):
            return await self.on_enter(s)
        try:
            data, mime = await self.fetch_media(media["id"])
        except ValueError:
            return [text_msg("😕 That file is too big (max 8 MB). Please send a smaller photo or PDF.")]
        except Exception:
            logger.exception("Could not download media")
            return [text_msg("😕 I couldn't download that file. Please send it again.")]
        mime = media.get("mime") or mime
        if not (mime.startswith("image/") or mime == "application/pdf"):
            return [text_msg("📎 I can read photos (JPG/PNG) and PDFs. Please send one of those.")]

        app, rule, docs = await self._load(s)
        label = R.DOC_LABELS.get(pending, pending)
        result = await self.verifier.verify(pending, data, mime)
        passport = None
        if pending == "passport" and result.ok:
            try:
                expiry = date.fromisoformat(result.fields["expiry"])
                if problem := R.passport_issue(expiry, date.fromisoformat(app["travel_date"]), app["stay_days"], rule["passport_min_months"]):
                    result.ok, result.issues = False, [problem]
                else:
                    passport = result.fields
            except (KeyError, ValueError, TypeError):
                result.ok, result.issues = False, ["I couldn't read the passport expiry date. Please retake the photo."]

        ext = mimetypes.guess_extension(mime) or ".bin"
        path = f"{s.user['id']}/{app['ref']}/{pending}-{int(time.time())}{ext.replace('.jpe', '.jpg')}"
        await self._db(self.repo.upload_file, path, data, mime)
        if not result.ok:
            await self._db(self.repo.add_document, app["id"], pending, path, mime, "rejected", "; ".join(result.issues), None)
            issues = "\n".join(f"• {i}" for i in result.issues)
            return [buttons_msg(f"❌ I can't accept this {label.lower()}:\n{issues}\n\nPlease send it again 📎",
                                [("vdoc:list", "📋 Checklist"), ("vdoc:pause", "⏸️ Continue later"), ("vcan:ask", "❌ Cancel")])]
        if passport:
            doc = await self._db(self.repo.add_document, app["id"], pending, path, mime, "uploaded", None, passport)
            c["confirm_doc"], c["passport"] = doc["id"], passport
            s.step = "awaiting_visa_confirm"
            return [buttons_msg(f"📖 Here's what I read from your passport:\n\n👤 {passport['name'] or '?'}\n🔢 {passport['passport_no']}\n"
                                f"🎂 {_fmt(passport['dob']) if passport.get('dob') else '?'}\n⏳ Expires {_fmt(passport['expiry'])}\n\nIs this correct?",
                                [("vdoc:ok", "✅ Correct"), ("vdoc:redo", "📷 Re-upload")])]
        await self._db(self.repo.add_document, app["id"], pending, path, mime, "verified", None, result.fields)
        return await self._ask_next_doc(s, f"✅ *{label}* looks good!{(' ' + result.note) if result.note else ''}\n\n")

    async def _on_doc_tap(self, s: Session, action: str) -> list[dict]:
        c = s.ctx["visa"]
        if action == "ok" and c.get("confirm_doc"):
            p = c.pop("passport", {})
            await self._db(self.repo.set_document_status, c.pop("confirm_doc"), "verified")
            await self._db(self.repo.update_application, c["app_id"], {
                "applicant_name": p.get("name"), "passport_no": p.get("passport_no"),
                "passport_expiry": p.get("expiry"), "dob": p.get("dob")})
            return await self._ask_next_doc(s, "✅ *Passport* confirmed!\n\n")
        if action == "redo" and c.get("confirm_doc"):
            await self._db(self.repo.set_document_status, c.pop("confirm_doc"), "rejected")
            c.pop("passport", None)
            c["pending_doc"], s.step = "passport", "awaiting_visa_doc"
            return [text_msg("📷 No problem. Please send a clearer photo of the passport's photo page.")]
        if action == "list":
            app, rule, docs = await self._load(s)
            return [buttons_msg(f"📋 *Application {app['ref']}*\n\n{R.checklist(rule['docs'], docs)}",
                                [("vdoc:back", "📎 Send next"), ("vdoc:pause", "⏸️ Continue later"), ("vcan:ask", "❌ Cancel")])]
        if action == "back":
            return await self._ask_next_doc(s)
        if action == "pause":
            s.step = "menu"
            return [buttons_msg("⏸️ Saved! Continue anytime from *My Applications*.", [("visa:apps", "📋 My Applications"), ("nav:menu", "🏠 Menu")])]
        return await self._ask_next_doc(s)

    async def _review(self, s: Session, prefix: str = "") -> list[dict]:
        app, rule, docs = await self._load(s)
        s.step = "awaiting_visa_pay"
        name = next((n for n in (app.get("applicant_name"), s.user.get("name")) if n), "Traveller")
        return [buttons_msg(
            f"{prefix}🎉 All documents are in and verified!\n\n👤 {name}\n🌍 {rule['flag']} {rule['country_name']} · "
            f"{'Tourism' if app['purpose'] == 'tourist' else 'Business'}\n📅 Travel: {_fmt(app['travel_date'])}\n"
            f"💰 Fee: *{inr(app['fee_inr'])}*\n\nPay now and I'll hand your application to our visa team.",
            [("vpay:pay", f"💳 Pay {inr(app['fee_inr'])}"), ("vcan:ask", "❌ Cancel")])]

    # --------------------------------------------------------------- payment
    async def _on_pay_tap(self, s: Session, action: str) -> list[dict]:
        app = await self._db(self.repo.get_application, s.ctx["visa"]["app_id"], s.user["id"])
        if not app:
            return await self.on_enter(s)
        if app["status"] in ("submitted", "in_review", "approved", "rejected"):
            return self._already_submitted(app)
        if action == "pay":
            if self.payments is None or not self.payments.enabled:  # no Razorpay keys: submit straight away
                await self._db(self.repo.update_application, app["id"], {"status": "submitted"})
                await self._db(self.repo.log_event, app["id"], "submitted", "Application handed to our visa team.")
                s.ctx.pop("visa", None)
                s.step = "menu"
                return self._submitted_messages(app)
            return await self._request_payment(s, app)
        if action == "check":
            link_id = s.ctx["visa"].get("link_id")
            try:
                paid = bool(link_id) and await self.payments.link_status(link_id) == "paid"
            except Exception:
                paid = False
            done = await self._db(self.repo.mark_paid, link_id) if paid else None
            if done:
                s.ctx.pop("visa", None)
                s.step = "menu"
                return self._submitted_messages(done)
            return [buttons_msg("I haven't received the payment yet. If you've just paid, give it a few seconds and tap again. 🙏",
                                [("vpay:check", "✅ I've paid"), ("vcan:ask", "❌ Cancel")])]
        return await self.on_enter(s)

    async def _request_payment(self, s: Session, app: dict) -> list[dict]:
        try:
            await self._db(self.repo.cancel_payments, app["id"])  # an old link must not stay payable
            link = await self.payments.create_link(app["fee_inr"], app["ref"], f"Visa {app['country_code']} {app['purpose']}",
                                                   s.phone, app.get("applicant_name") or s.user.get("name") or "", PAYMENT_WINDOW_MIN)
            await self._db(self.repo.create_payment, app["id"], s.user["id"], app["fee_inr"], link["id"], link["short_url"],
                           datetime.now(IST) + timedelta(minutes=PAYMENT_WINDOW_MIN + 1))
        except Exception:
            logger.exception("Could not create a visa payment link")
            return [buttons_msg("😕 I couldn't set up the payment right now. Please try again in a minute.",
                                [("vpay:pay", "🔁 Try again"), ("nav:menu", "🏠 Menu")])]
        await self._db(self.repo.update_application, app["id"], {"status": "payment_pending"})
        await self._db(self.repo.log_event, app["id"], "payment_pending", "Waiting for payment")
        s.ctx["visa"]["link_id"] = link["id"]
        s.step = "awaiting_visa_pay"
        test = "\n🧪 Test mode: no real money is charged." if getattr(self.payments, "test_mode", False) else ""
        return [cta_msg(f"💳 Pay *{inr(app['fee_inr'])}* to submit your {app['country_code']} visa application.\n🎫 Ref: {app['ref']}\n"
                        f"⏳ The link works for {PAYMENT_WINDOW_MIN} minutes.{test}", f"Pay {inr(app['fee_inr'])}", link["short_url"]),
                buttons_msg("I'll submit your application the moment the payment goes through ✅",
                            [("vpay:check", "✅ I've paid"), ("vcan:ask", "❌ Cancel")])]

    def _submitted_messages(self, app: dict) -> list[dict]:
        return [buttons_msg(f"🎉 *Application submitted!*\n\n🎫 Ref: *{app['ref']}*\n"
                            f"Our visa team files it with the authorities now. I'll message you at every step, so you don't need to chase anyone. 🔔",
                            [(f"vapp:{app['id']}", "🔎 Track status"), ("nav:menu", "🏠 Menu")])]

    def _already_submitted(self, app: dict) -> list[dict]:
        return [buttons_msg(f"✅ Application *{app['ref']}* is already {R.STATUS_LABEL[app['status']].split(' ', 1)[1].lower()}.",
                            [(f"vapp:{app['id']}", "🔎 Track status"), ("nav:menu", "🏠 Menu")])]

    async def confirm_payment(self, link_id: str) -> tuple[str, list[dict]] | None:
        """Razorpay webhook: a visa payment link was paid."""
        app = await self._db(self.repo.mark_paid, link_id)
        if not app:
            return None
        phone = app["users"]["phone"]
        convo = await self._db(self.repo.get_conversation, phone)
        ctx = dict((convo or {}).get("context") or {})
        ctx.pop("visa", None)
        await self._db(self.repo.save_conversation, phone, "menu", ctx)
        return phone.lstrip("+"), self._submitted_messages(app)

    # ------------------------------------------------------------ my applications
    async def _show_apps(self, s: Session) -> list[dict]:
        apps = await self._db(self.repo.list_applications, s.user["id"])
        if not apps:
            return [buttons_msg("You haven't applied for any visas yet. Want to start? 🙂",
                                [("visa:new", "🛂 Check & Apply"), ("nav:menu", "🏠 Menu")])]
        s.step = "awaiting_visa_app"
        rows = [(f"vapp:{a['id']}", f"{a['ref']} · {COUNTRY_NAMES.get(a['country_code'], a['country_code'])}",
                 f"{R.STATUS_LABEL.get(a['status'], a['status'])} · {date.fromisoformat(a['travel_date']):%d %b}") for a in apps]
        rows.append(("nav:menu", "🏠 Main menu", ""))
        return [list_msg("🛂 *Your visa applications*\nPick one to track 👇", "View applications", rows, "Applications")]

    async def _show_app(self, s: Session, app_id: str) -> list[dict]:
        app = await self._db(self.repo.get_application, app_id, s.user["id"])
        if not app:
            return await self.on_enter(s)
        events = await self._db(self.repo.list_events, app["id"])
        timeline = "\n".join(f"• {to_ist(e['created_at']):%d %b, %H:%M}  {R.STATUS_LABEL.get(e['status'], e['status'])}"
                             + (f"\n   _{e['note']}_" if e.get("note") else "") for e in events[-6:])
        text = (f"🛂 *{COUNTRY_NAMES.get(app['country_code'], app['country_code'])}* · {app['purpose'].title()}\n"
                f"🎫 {app['ref']} · {R.STATUS_LABEL.get(app['status'], app['status'])}\n📅 Travel: {_fmt(app['travel_date'])}\n"
                f"💰 {inr(app['fee_inr'])}\n\n{timeline}")
        buttons = [("visa:apps", "📋 All applications"), ("nav:menu", "🏠 Menu")]
        if app["status"] in ("draft", "payment_pending"):
            buttons = [(f"vres:{app['id']}", "▶️ Continue"), (f"vcan:y:{app['id']}", "❌ Cancel")] + buttons[:1]
        elif app["status"] == "approved":
            buttons.insert(0, (f"vfile:{app['id']}", "📄 Get my visa"))
        return [buttons_msg(text, buttons)]

    async def _resume(self, s: Session, app_id: str) -> list[dict]:
        app = await self._db(self.repo.get_application, app_id, s.user["id"])
        if not app or app["status"] not in ("draft", "payment_pending"):
            return await self.on_enter(s)
        s.ctx["visa"] = {"country": app["country_code"], "purpose": app["purpose"], "travel_date": app["travel_date"],
                         "stay_days": app["stay_days"], "app_id": app["id"]}
        return await self._ask_next_doc(s, f"▶️ Continuing *{app['ref']}*.\n\n")

    async def _send_visa(self, s: Session, app_id: str) -> list[dict]:
        app = await self._db(self.repo.get_application, app_id, s.user["id"])
        if not app or app["status"] != "approved":
            return await self.on_enter(s)
        if not app.get("visa_file_path"):
            return [buttons_msg("Your approval is in! I'm preparing the visa copy and will send it shortly. 📄",
                                [("visa:apps", "📋 My Applications"), ("nav:menu", "🏠 Menu")])]
        url = await self._db(self.repo.signed_url, app["visa_file_path"])
        return [document_msg(url, f"visa-{app['ref']}.pdf", f"🛂 Your {COUNTRY_NAMES.get(app['country_code'], '')} visa")]

    async def _on_cancel(self, s: Session, val: str) -> list[dict]:
        c = s.ctx.get("visa", {})
        if val == "ask" or val.startswith("y:"):
            app_id = val[2:] if val.startswith("y:") else c.get("app_id")
            app = await self._db(self.repo.get_application, app_id, s.user["id"]) if app_id else None
            if not app or app["status"] not in ("draft", "payment_pending"):
                return [buttons_msg("This application can't be cancelled here because it's already with our visa team.",
                                    [("visa:apps", "📋 My Applications"), ("nav:menu", "🏠 Menu")])]
            return [buttons_msg(f"Cancel application *{app['ref']}*? Your documents will be deleted.",
                                [(f"vcan:go:{app['id']}", "Yes, cancel it"), (f"vapp:{app['id']}", "No, keep it")])]
        if val.startswith("go:"):
            app = await self._db(self.repo.get_application, val[3:], s.user["id"])
            if app and app["status"] in ("draft", "payment_pending"):
                await self._db(self.repo.cancel_payments, app["id"])
                await self._db(self.repo.update_application, app["id"], {"status": "cancelled"})
                await self._db(self.repo.log_event, app["id"], "cancelled", "Cancelled by the user")
                s.ctx.pop("visa", None)
                s.step = "menu"
                return [buttons_msg(f"🚫 Application *{app['ref']}* is cancelled.", [("visa:new", "🛂 Start a new one"), ("nav:menu", "🏠 Menu")])]
        return await self.on_enter(s)

    # --------------------------------------------- status updates from our visa team
    async def update_status(self, ref: str, status: str, note: str | None = None,
                            file_url: str | None = None) -> tuple[str, list[dict]]:
        """Called by the admin API when the visa team moves an application. Returns (whatsapp number, messages)."""
        app = await self._db(self.repo.get_application_by_ref, ref)
        if not app:
            raise LookupError(f"No application {ref}")
        if status not in ("in_review", "approved", "rejected") or app["status"] not in ("submitted", "in_review"):
            raise ValueError(f"Can't move {app['status']} to {status}")
        patch = {"status": status}
        if status == "approved" and file_url:
            patch["visa_file_path"] = file_url
        await self._db(self.repo.update_application, app["id"], patch)
        await self._db(self.repo.log_event, app["id"], status, note)
        country = COUNTRY_NAMES.get(app["country_code"], app["country_code"])
        if status == "in_review":
            msgs = [buttons_msg(f"🔍 Your *{country}* visa application *{ref}* is now under review. I'll update you as soon as there's news.",
                                [(f"vapp:{app['id']}", "🔎 Track status"), ("nav:menu", "🏠 Menu")])]
        elif status == "approved":
            msgs = [buttons_msg(f"🎉 *Visa approved!* Your *{country}* visa ({ref}) is ready. Have an amazing trip! ✈️",
                                [(f"vfile:{app['id']}", "📄 Get my visa"), ("menu:book", "✈️ Book a Flight"), ("nav:menu", "🏠 Menu")])]
        else:
            msgs = [buttons_msg(f"😔 Your *{country}* visa application *{ref}* wasn't approved." + (f"\nReason: {note}" if note else "")
                                + "\n\nOur team can help with the next steps.", [(f"vapp:{app['id']}", "🔎 Details"), ("nav:menu", "🏠 Menu")])]
        return app["users"]["phone"].lstrip("+"), msgs

    async def advance_demo(self) -> list[tuple[str, list[dict]]]:
        """Demo mode only: moves submitted -> in_review -> approved on a timer so you can see the whole journey."""
        out, now = [], datetime.now(IST)
        for app in await self._db(self.repo.active_applications):
            events = await self._db(self.repo.list_events, app["id"])
            age = (now - to_ist(events[-1]["created_at"])).total_seconds() if events else 0
            if app["status"] == "submitted" and age >= DEMO_REVIEW_AFTER_S:
                out.append(await self.update_status(app["ref"], "in_review", "Documents received by the authority"))
            elif app["status"] == "in_review" and age >= DEMO_APPROVE_AFTER_S:
                out.append(await self.update_status(app["ref"], "approved", "Approved (demo mode)"))
        return out
