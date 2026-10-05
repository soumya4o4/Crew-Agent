"""Checkout: one payment for everything the traveller picked (flight, hotel...), with their contact details.

The service agents do not hold stock or make payment links while the traveller is still choosing. When they confirm,
an agent hands Checkout a cart item (plain data) and Checkout moves on to the next service the traveller asked for.
When the cart is complete it collects name, email and phone once, then asks every service to hold its stock, makes ONE
Razorpay link for the total, and attaches that link to each held booking. The webhook (or "I've paid") then confirms
all of them together.

An agent that can be bundled sets `bundleable = True` and implements:
    create_pending(s, item) -> booking dict or None   hold the stock as a pending booking
    attach_payment(s, booking, link, expires_at)       record the shared payment link against that booking
    release_booking(booking_id)                        give the stock back
    booking_state(booking_id) -> status or None        pending / confirmed / cancelled
"""
import logging
import re
from datetime import datetime, timedelta

from app.agents.base import Agent, Session
from app.core.messages import buttons_msg, cta_msg, text_msg
from app.core.utils import IST, inr

logger = logging.getLogger(__name__)
PAYMENT_WINDOW_MIN = 20  # Razorpay needs payment links to live at least 15 min
EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<![\w@.])(?:\+?\d[\d\s-]{8,15}\d)(?![\w@])")
NAME = re.compile(r"[A-Za-z][A-Za-z .'\-]{1,59}")
SAME = re.compile(r"\b(same|this number|whatsapp number|this one|yahi)\b", re.I)


def parse_contact(text: str, whatsapp: str = "") -> dict:
    """"Rahul Verma, rahul@mail.com, 98765 43210" -> {name, email, phone}; only what the message holds."""
    found: dict = {}
    rest = text
    if m := EMAIL.search(rest):
        found["email"] = m.group(0).lower()
        rest = rest.replace(m.group(0), " ")
    if m := PHONE.search(rest):
        digits = re.sub(r"\D", "", m.group(0))
        if 10 <= len(digits) <= 13:
            found["phone"] = digits
            rest = rest.replace(m.group(0), " ")
    elif whatsapp and SAME.search(rest):
        found["phone"] = re.sub(r"\D", "", whatsapp)
    rest = SAME.sub(" ", rest)
    rest = re.sub(r"(?i)\b(name|email|e-mail|mail|phone|mobile|number|my|is|and)\b\s*:?", " ", rest)
    parts = [p.strip(" ,;:-") for p in re.split(r"[,;\n]", rest) if p.strip(" ,;:-")]
    name = " ".join(" ".join(parts).split())
    if NAME.fullmatch(name):
        found["name"] = name.title()
    return found


class CheckoutAgent(Agent):
    name = "checkout"
    title = "Checkout"
    in_menu = False
    owns = frozenset({"chk"})

    def __init__(self, payments=None):
        self.payments = payments  # RazorpayGateway (or a fake); None or disabled: services confirm instantly
        self.agents: dict[str, Agent] = {}  # filled in by the Concierge

    @property
    def online(self) -> bool:
        return self.payments is not None and self.payments.enabled

    def reset(self, s: Session) -> None:
        s.ctx.pop("cart", None)  # a half-chosen bundle is dropped; a payment link already made lives on in ctx["checkout"]

    def expects_text(self, s: Session) -> bool:
        return s.step == "awaiting_contact"

    # ------------------------------------------------------------------ the cart
    def bundleable(self) -> list[str]:
        return [n for n, a in self.agents.items() if getattr(a, "bundleable", False)]

    async def add(self, s: Session, item: dict, hint: dict | None = None) -> list[dict]:
        """A service hands over what the traveller confirmed. Next: the next requested service, or the payment."""
        cart = [i for i in s.ctx.get("cart") or [] if i["kind"] != item["kind"]] + [item]
        s.ctx["cart"] = cart
        have = {i["kind"] for i in cart}
        queue = [n for n in s.ctx.get("queue") or [] if n in self.bundleable() and n not in have]
        if queue and hint:
            nxt = queue[0]
            s.ctx["queue"] = [n for n in s.ctx["queue"] if n != nxt]
            s.ctx["agent"] = nxt
            intro = text_msg(f"✅ Added to your bundle: {item['label']} · {inr(item['amount'])}.\n"
                             f"Now your {self.agents[nxt].title.lower()}. I'll take one payment for everything at the end.")
            return [intro] + await self.agents[nxt].start(s, hint)
        return await self.begin(s)

    @staticmethod
    def _lines(cart: list[dict]) -> str:
        return "\n".join(f"• {i['label']} · {inr(i['amount'])}" for i in cart)

    # ------------------------------------------------------------------ contact details
    async def begin(self, s: Session) -> list[dict]:
        """Everything is chosen: collect the contact details if we don't have them yet, then pay."""
        s.ctx["agent"] = self.name
        contact = s.ctx.get("contact") or {}
        if all(contact.get(k) for k in ("name", "email", "phone")):
            return await self._pay(s)
        return self._ask_contact(s, contact)

    def _ask_contact(self, s: Session, have: dict) -> list[dict]:
        s.step = "awaiting_contact"
        cart = s.ctx.get("cart") or []
        total = sum(i["amount"] for i in cart)
        missing = [w for w, k in (("full name", "name"), ("email", "email"), ("phone number", "phone")) if not have.get(k)]
        ask = ", ".join(missing[:-1]) + (" and " if len(missing) > 1 else "") + missing[-1]
        example = "Rahul Verma, rahul@mail.com, 9876543210"
        return [text_msg(f"🧾 *Your bundle*\n{self._lines(cart)}\n💰 Total *{inr(total)}*\n\n"
                         f"To make your payment link, please send your {ask} in one message, like:\n*{example}*\n"
                         "(Reply *same* to use this WhatsApp number as your phone.)")]

    async def _on_contact_text(self, s: Session, text: str) -> list[dict]:
        if not s.ctx.get("cart"):
            return [text_msg("I don't have anything waiting for payment. Tell me what you'd like to book. ✈️")]
        contact = {**(s.ctx.get("contact") or {}), **parse_contact(text, s.phone)}
        s.ctx["contact"] = contact
        if all(contact.get(k) for k in ("name", "email", "phone")):
            return await self._pay(s)
        return [text_msg("😕 I still need your " + " and ".join(
            w for w, k in (("full name", "name"), ("email", "email"), ("phone number", "phone")) if not contact.get(k)) + ".")]

    # ------------------------------------------------------------------ the payment
    async def _pay(self, s: Session) -> list[dict]:
        cart, contact = s.ctx.get("cart") or [], s.ctx["contact"]
        held: list[tuple[Agent, dict, dict]] = []  # (agent, item, booking)
        for item in cart:
            agent = self.agents[item.get("agent") or item["kind"]]
            booking = await agent.create_pending(s, item)
            if not booking:
                for a, _, b in held:
                    await a.release_booking(b["id"])
                s.ctx["cart"] = [i for i in cart if i is not item]
                s.step = "menu"
                return [buttons_msg(f"😕 Sorry, {item['label']} is no longer available, so I've held nothing. "
                                    "Tell me your trip again with other dates and I'll look again.", [("nav:menu", "🏠 Menu")])]
            held.append((agent, item, booking))
        total = sum(b_amount(b, i) for _, i, b in held)
        label = " + ".join(i["label"] for _, i, _ in held)[:200]
        try:
            link = await self.payments.create_link(total, held[0][2].get("pnr") or held[0][2].get("ref"), label,
                                                   "+" + contact["phone"].lstrip("+"), contact["name"], PAYMENT_WINDOW_MIN,
                                                   contact["email"])
            expires = datetime.now(IST) + timedelta(minutes=PAYMENT_WINDOW_MIN + 1)  # +1 min grace for the webhook
            for agent, _, booking in held:
                await agent.attach_payment(s, booking, link, expires)
        except Exception:
            logger.exception("Could not create the bundle payment link")
            for agent, _, booking in held:
                await agent.release_booking(booking["id"])
            s.step = "menu"
            return [buttons_msg("😕 I couldn't set up the payment right now, and I've released everything I was holding. "
                                "Please try again in a minute.", [("nav:menu", "🏠 Menu")])]
        s.ctx["checkout"] = {"link_id": link["id"], "items": [{"kind": i["kind"], "booking_id": b["id"]} for _, i, b in held]}
        s.ctx["bundle_kinds"] = [i["kind"] for _, i, _ in held]
        s.ctx.pop("cart", None)
        s.step = "awaiting_payment"
        test = "\n\n🧪 Test mode — no real money will be charged." if getattr(self.payments, "test_mode", False) else ""
        if len(held) == 1 and held[0][1]["kind"] == "hotel":
            item = held[0][1]
            body = (f"💳 Your hotel stay is ready\n\n"
                    f"{item.get('summary_text', self._lines([item]))}\n"
                    f"💰 {inr(total)} total{test}\n\nProceed to payment?")
        else:
            body = (f"💳 *Almost done!* Pay *{inr(total)}* to confirm everything:\n{self._lines([i for _, i, _ in held])}\n"
                    f"⏳ Held for {PAYMENT_WINDOW_MIN} minutes.{test}")
                    
        return [
            cta_msg(body, f"Pay {inr(total)}", link["short_url"]),
            buttons_msg("I'll confirm your booking here the moment the payment goes through ✅",
                        [("chk:check", "✅ I've paid"), ("chk:cancel", "❌ Cancel")])]

    # ------------------------------------------------------------------ taps and words while paying
    async def process(self, s: Session, text: str, reply_id: str | None) -> list[dict]:
        if reply_id:
            return await self._on_tap(s, reply_id.partition(":")[2])
        if s.step == "awaiting_contact":
            return await self._on_contact_text(s, text)
        if s.ctx.get("checkout"):
            return [buttons_msg("Your payment is waiting. Tap *I've paid* once you have paid, or cancel to release everything.",
                                [("chk:check", "✅ I've paid"), ("chk:cancel", "❌ Cancel")])]
        return [text_msg("Tell me what you'd like to book and I'll take care of it. ✈️")]

    async def abandon(self, s: Session) -> None:
        """The traveller walked away from a payment (cancelled, or said "reset"): give back everything still held and kill
        the payment link. Bookings that are already paid stay booked."""
        co = s.ctx.get("checkout")
        if not co:
            return
        try:  # paid a moment ago, webhook not here yet: leave the bookings for the webhook to confirm
            paid = await self.payments.link_status(co["link_id"]) == "paid"
        except Exception:
            paid = False
        if not paid and "confirmed" not in [await self.agents[i.get("agent") or i["kind"]].booking_state(i["booking_id"]) for i in co["items"]]:
            for i in co["items"]:
                await self.agents[i.get("agent") or i["kind"]].release_booking(i["booking_id"])
            try:
                await self.payments.cancel_link(co["link_id"])
            except Exception:
                logger.warning("Could not cancel the payment link %s", co["link_id"])
        self._clear(s.ctx)

    async def _on_tap(self, s: Session, action: str) -> list[dict]:
        co = s.ctx.get("checkout")
        if not co:
            s.step = "menu"
            return [buttons_msg("There's no payment waiting. If you've already paid, your confirmation is above. ✅", [("nav:menu", "🏠 Menu")])]
        states = [await self.agents[i.get("agent") or i["kind"]].booking_state(i["booking_id"]) for i in co["items"]]
        if action == "cancel":
            if "confirmed" in states:
                return [buttons_msg("This is already paid and confirmed, so I can't drop it here. Use My Bookings to cancel it.",
                                    [("menu:bookings", "📋 My Bookings"), ("nav:menu", "🏠 Menu")])]
            await self.abandon(s)
            s.step = "menu"
            return [buttons_msg("No problem, I've cancelled it and released everything. 👍", [("nav:menu", "🏠 Menu")])]
        # "I've paid"
        if all(st == "confirmed" for st in states):  # the webhook beat the tap
            self._clear(s.ctx)
            s.step = "menu"
            return [buttons_msg("✅ Already confirmed! Your tickets are above.", [("menu:bookings", "📋 My Bookings"), ("nav:menu", "🏠 Menu")])]
        if all(st == "cancelled" or st is None for st in states):
            self._clear(s.ctx)
            s.step = "menu"
            return [buttons_msg("⌛ The payment window ended and everything was released. Want to try again?", [("nav:menu", "🏠 Menu")])]
        try:
            paid = await self.payments.link_status(co["link_id"]) == "paid"
        except Exception:
            paid = False
        out: list[dict] = []
        if paid:
            for i in co["items"]:
                if result := await self.agents[i.get("agent") or i["kind"]].confirm_payment(co["link_id"]):
                    out += result[1]
        if not out:
            return [buttons_msg("I haven't received the payment yet. If you've just paid, give it a few seconds and tap again. 🙏",
                                [("chk:check", "✅ I've paid"), ("chk:cancel", "❌ Cancel")])]
        self._clear(s.ctx)
        s.step = "menu"
        return out

    @staticmethod
    def _clear(ctx: dict) -> None:
        for k in ("checkout", "cart"):
            ctx.pop(k, None)

    async def confirm_payment_all(self, agents: dict[str, Agent], link_id: str) -> tuple[str, list[dict]] | None:
        """Razorpay webhook: ask every service whether this link holds one of its bookings; all their messages go out together."""
        number, messages = None, []
        for agent in agents.values():
            if hasattr(agent, "confirm_payment") and agent is not self and (result := await agent.confirm_payment(link_id)):
                number = number or result[0]
                messages += result[1]
        return (number, messages) if number else None


def b_amount(booking: dict, item: dict) -> int:
    return int(booking.get("total_price_inr") or item["amount"])
