"""Forex agent: live rates, quotes, and orders for currency (cash, delivered) or a forex card, paid by Razorpay link.

Button ids: fx:* (menu, buy, rates, orders), fcur:, famt:, fprod:, fcfm:, fpay:, fbk:, fbkc:, fbkcy:
State lives in ctx["fx"] (currency, amount, product) and ctx["fxpay"] (the payment being waited for).
We buy at the live mid-market rate plus a margin (see quote.py); the order locks the rate we showed. After payment the
order is `confirmed` and our forex desk takes over (KYC, delivery or card issue), moving it to `fulfilled` through the admin API.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta

from app.agents.base import Agent, Session
from app.agents.forex import quote as Q
from app.agents.forex.currencies import (CURRENCIES, POPULAR, currency_for, find_currency, flag_of, name_of,
                                         parse_amount)
from app.core.messages import buttons_msg, cta_msg, list_msg, text_msg
from app.core.places import city, country_of, is_international
from app.core.utils import IST, inr, now_ist

logger = logging.getLogger(__name__)
PAYMENT_WINDOW_MIN = 20  # Razorpay needs payment links to live at least 15 min
STATUS_ICON = {"pending": "⏳", "confirmed": "✅", "fulfilled": "📦", "cancelled": "❌"}
BUDGETS_INR = (10_000, 25_000, 50_000, 100_000)
KYC_DOCS = "passport, your flight ticket (or visa), and PAN card"
TEXT_STEPS = ("awaiting_fx_currency", "awaiting_fx_amount")


def fmt_rate(mid: float) -> str:
    return f"₹{mid:,.2f}" if mid >= 1 else f"₹{mid:.4f}"


class ForexAgent(Agent):
    name = "forex"
    title = "Forex"
    emoji = "💱"
    menu_desc = "Currency, cards & live rates"
    owns = frozenset({"fx", "fcur", "famt", "fprod", "fcfm", "fpay", "fbk", "fbkc", "fbkcy"})

    def __init__(self, repo, payments=None, rates=None, advisor=None):
        self.repo = repo          # ForexRepo
        self.payments = payments  # RazorpayGateway (or None: orders confirm instantly)
        self.rates = rates        # RateService (or a fake): live rates
        self.advisor = advisor    # TravelAdvisor: currency of a country we don't know. None: skip

    async def _db(self, fn, *args):
        return await asyncio.to_thread(fn, *args)

    def reset(self, s: Session) -> None:
        s.ctx.pop("fx", None)

    def expects_text(self, s: Session) -> bool:
        return s.step in TEXT_STEPS

    async def _mid(self, code: str) -> float | None:
        return await self.rates.inr_per_unit(code) if self.rates else None

    async def _trip_currency(self, s: Session) -> tuple[str, str] | None:
        """The currency for the international trip they booked: (code, city), if there is one."""
        trip = s.ctx.get("trip") or {}
        if not trip.get("to") or not is_international(trip.get("from", ""), trip["to"]):  # (older saved trips have no "intl")
            return None
        country = trip.get("country") or country_of(trip["to"])
        code = currency_for(country) or (await self.advisor.currency_of(country) if self.advisor and country else None)
        return (code, trip.get("city") or city(trip["to"])) if code and code != "INR" else None

    # ------------------------------------------------------------------ entry points
    async def on_enter(self, s: Session) -> list[dict]:
        self.reset(s)
        s.step = "fx_menu"
        rows = [("fx:buy", "💱 Buy currency", "Cash or forex card"), ("fx:rates", "📈 Live rates", "Today's market rates"),
                ("fx:orders", "📋 My orders", "Track or cancel an order")]
        body = "💱 *Forex*\nBuy currency or a forex card at live rates, with a clear price before you pay."
        if trip := await self._trip_currency(s):
            code, where = trip
            rows.insert(0, (f"fcur:{code}", f"{flag_of(code)} {code} for {where}"[:24], "For your upcoming trip"))
            body += f"\n\n✈️ Flying to *{where}*? You'll want *{name_of(code)}*."
        return [list_msg(body, "Choose", rows, "Forex")]

    async def start(self, s: Session, slots: dict) -> list[dict]:
        """Free text like "100 usd in inr", "dirhams for dubai" or "forex for thailand": skip what we already heard."""
        cur = slots.get("currency") or currency_for(slots.get("country") or "") or \
            (currency_for(country_of(slots["to"])) if slots.get("to") else None)
        if not cur:
            return await self.on_enter(s)
        self.reset(s)
        if slots.get("amount"):
            return await self._convert(s, cur, float(slots["amount"]), bool(slots.get("amount_inr")))
        return await self._pick_currency(s, cur)

    async def process(self, s: Session, text: str, reply_id: str | None) -> list[dict]:
        if not reply_id:
            if s.step == "awaiting_fx_currency":
                return await self._on_currency_text(s, text)
            if s.step == "awaiting_fx_amount":
                return await self._on_amount_text(s, text)
            if parsed := find_currency(text):  # "convert 50 usd" typed while somewhere else in the menu
                return await self.start(s, {"currency": parsed, **({"amount": a[0]} if (a := parse_amount(text)) and a[1] != "INR" else {})})
            return await self.on_enter(s)
        kind, _, val = reply_id.partition(":")
        c = s.ctx.setdefault("fx", {})
        if kind == "fx":
            if val == "buy":
                return await self._ask_currency(s)
            if val == "rates":
                return await self._show_rates(s)
            if val == "orders":
                return await self._show_orders(s)
            if val == "go" and c.get("amount") and c.get("cur"):
                return await self._ask_product(s)
        elif kind == "fcur":
            if val == "more":
                s.step = "awaiting_fx_currency"
                return [text_msg("🌍 Type the currency, like *dollars*, *AED* or *yen*.")]
            return await self._pick_currency(s, val)
        elif kind == "famt" and c.get("cur"):
            return await self._on_amount_tap(s, val)
        elif kind == "fprod" and c.get("amount") and val in Q.MARGIN:
            c["product"] = val
            return await self._show_quote(s)
        elif kind == "fcfm" and c.get("product"):
            if val == "yes":
                return await self._do_order(s)
            if val == "amt":
                return await self._ask_amount(s)
            return await self.on_enter(s)
        elif kind == "fpay" and val in ("check", "cancel"):
            return await self._on_payment_tap(s, val)
        elif kind == "fbk":
            return await self._show_order(s, val)
        elif kind == "fbkc":
            return await self._ask_cancel(s, val)
        elif kind == "fbkcy":
            return await self._do_cancel(s, val)
        return await self.on_enter(s)

    # --------------------------------------------------------------------- rates
    async def _unavailable(self, s: Session) -> list[dict]:
        s.step = "fx_menu"
        return [buttons_msg("😕 I can't reach the live rates right now, and I won't quote a price I can't stand behind. "
                            "Please try again in a minute.", [("fx:buy", "🔄 Try again"), ("nav:menu", "🏠 Menu")])]

    async def _show_rates(self, s: Session) -> list[dict]:
        codes = [c for c in dict.fromkeys([*(await self._trip_currency(s) or ("",))[:1], *POPULAR]) if c]
        lines = []
        for code in codes[:9]:
            if mid := await self._mid(code):
                lines.append(f"{flag_of(code)} 1 {code} = *{fmt_rate(mid)}*  _{name_of(code)}_")
        if not lines:
            return await self._unavailable(s)
        ago = getattr(self.rates, "updated_ago_min", 0)
        s.step = "fx_menu"
        return [buttons_msg("📈 *Live rates* (market, in rupees)\n\n" + "\n".join(lines)
                            + f"\n\n_Updated {ago} min ago. Your order price adds a small margin, shown before you pay._",
                            [("fx:buy", "💱 Buy currency"), ("fx:orders", "📋 My orders"), ("nav:menu", "🏠 Menu")])]

    # ------------------------------------------------------------- choosing currency
    async def _ask_currency(self, s: Session) -> list[dict]:
        s.step = "awaiting_fx_currency"
        trip = await self._trip_currency(s)
        codes = [c for c in dict.fromkeys([*([trip[0]] if trip else []), *POPULAR])][:9]
        rows = []
        for code in codes:
            mid = await self._mid(code)
            extra = f" · {fmt_rate(mid)}" if mid else ""
            note = f"for {trip[1]}" if trip and code == trip[0] else name_of(code)
            rows.append((f"fcur:{code}", f"{flag_of(code)} {code}", f"{note}{extra}"[:72]))
        rows.append(("fcur:more", "Another currency…", "Type its name or code"))
        return [list_msg("💱 *Which currency do you need?*", "Choose currency", rows, "Currencies")]

    async def _on_currency_text(self, s: Session, text: str) -> list[dict]:
        word = text.strip()
        code = find_currency(word) or (word.upper() if len(word) == 3 and word.isalpha() else None)
        if not code or not await self._mid(code):
            return [text_msg("😕 I don't know that currency. Try a name like *dollars* or *yen*, or a 3-letter code like *AED*.")]
        return await self._pick_currency(s, code)

    async def _pick_currency(self, s: Session, code: str) -> list[dict]:
        if await self._mid(code) is None:
            return await self._unavailable(s) if not (self.rates and await self.rates.rates()) else \
                [text_msg(f"😕 I can't price *{code}* right now. Please pick another currency.")]
        s.ctx["fx"] = {"cur": code}
        return await self._ask_amount(s)

    # ------------------------------------------------------------------- amounts
    async def _ask_amount(self, s: Session) -> list[dict]:
        c = s.ctx.get("fx") or {}
        if not c.get("cur"):
            return await self._ask_currency(s)
        mid = await self._mid(c["cur"])
        if mid is None:
            return await self._unavailable(s)
        for k in ("amount", "product"):
            c.pop(k, None)
        s.step = "awaiting_fx_amount"
        rows = [(f"famt:{b}", inr(b), f"≈ {Q.fmt_amount(Q.round_amount(b / mid))} {c['cur']}") for b in BUDGETS_INR]
        return [list_msg(f"{flag_of(c['cur'])} *{name_of(c['cur'])}* · today {fmt_rate(mid)} per {c['cur']} (market rate)\n\n"
                         f"How much do you need? Pick a budget, or type an amount like *500* ({c['cur']}) or *₹40000*.",
                         "Choose amount", rows, "Budget")]

    async def _on_amount_tap(self, s: Session, val: str) -> list[dict]:
        c, mid = s.ctx["fx"], await self._mid(s.ctx["fx"]["cur"])
        if mid is None or not val.isdigit():
            return await self._ask_amount(s)
        return await self._set_amount(s, Q.round_amount(int(val) / mid))

    async def _on_amount_text(self, s: Session, text: str) -> list[dict]:
        c = s.ctx.get("fx") or {}
        if not c.get("cur"):
            return await self._ask_currency(s)
        parsed = parse_amount(text)
        if not parsed:
            return [text_msg(f"😕 Please type an amount like *500* ({c['cur']}) or *₹40000*.")]
        value, unit = parsed
        if unit and unit != "INR" and unit != c["cur"] and await self._mid(unit):  # "500 euro" while on dollars: switch
            c["cur"] = unit
        mid = await self._mid(c["cur"])
        if mid is None:
            return await self._unavailable(s)
        return await self._set_amount(s, Q.round_amount(value / mid) if unit == "INR" else value)

    async def _set_amount(self, s: Session, foreign: float) -> list[dict]:
        c = s.ctx["fx"]
        mid = await self._mid(c["cur"])
        if mid is None:
            return await self._unavailable(s)
        if problem := Q.limit_problem(foreign * mid, await self._mid("USD")):
            return [text_msg(f"😕 {problem}")]
        c["amount"] = foreign
        return await self._ask_product(s)

    async def _convert(self, s: Session, cur: str, amount: float, in_inr: bool) -> list[dict]:
        """"100 usd in inr": answer with the market value first, then offer to buy it."""
        mid = await self._mid(cur)
        if mid is None:
            return await self._unavailable(s)
        foreign = Q.round_amount(amount / mid) if in_inr else amount
        s.ctx["fx"] = {"cur": cur}
        s.step = "fx_menu"
        text = (f"💱 *{Q.fmt_amount(foreign)} {cur}* is about *{inr(round(foreign * mid))}* at today's market rate "
                f"({fmt_rate(mid)} per {cur}).\n\nWant to buy it? I'll show the exact price, with fees, before you pay.")
        s.ctx["fx"]["amount"] = foreign
        return [buttons_msg(text, [("fx:go", f"💱 Buy {cur}"), ("fx:rates", "📈 Live rates"), ("nav:menu", "🏠 Menu")])]

    # ------------------------------------------------------------------- product + quote
    async def _ask_product(self, s: Session) -> list[dict]:
        c = s.ctx["fx"]
        mid = await self._mid(c["cur"])
        usd = await self._mid("USD")
        s.step = "awaiting_fx_product"
        cash, card = Q.quote(mid, c["amount"], "cash"), Q.quote(mid, c["amount"], "card")
        cash_ok = Q.limit_problem(c["amount"] * mid, usd, "cash") is None
        lines = [f"💱 *{Q.fmt_amount(c['amount'])} {c['cur']}*: how would you like it?", ""]
        lines.append(f"💵 *Cash*: {inr(cash['total'])}, delivered to your door" if cash_ok else
                     "💵 *Cash*: not available for this amount (RBI cash limit)")
        lines.append(f"💳 *Forex card*: {inr(card['total'])}, reloadable and safer to carry")
        buttons = ([("fprod:cash", "💵 Cash")] if cash_ok else []) + [("fprod:card", "💳 Forex card")]
        return [buttons_msg("\n".join(lines), buttons)]

    async def _quote_for(self, s: Session) -> dict | None:
        c = s.ctx["fx"]
        mid = await self._mid(c["cur"])
        return Q.quote(mid, c["amount"], c["product"]) if mid else None

    async def _show_quote(self, s: Session) -> list[dict]:
        c = s.ctx["fx"]
        q = await self._quote_for(s)
        if q is None:
            return await self._unavailable(s)
        if problem := Q.limit_problem(c["amount"] * q["mid"], await self._mid("USD"), c["product"]):
            return [buttons_msg(f"😕 {problem}", [("fprod:card", "💳 Forex card"), ("fcfm:amt", "✏️ Change amount"), ("nav:menu", "🏠 Menu")])]
        s.step = "awaiting_fx_confirm"
        what = "delivery" if c["product"] == "cash" else "card issue and load"
        fee = f"{inr(q['fee'])} ({what})" if q["fee"] else f"free ({what}, order above {inr(Q.FREE_FEE_ABOVE_INR)})"
        return [buttons_msg(
            f"{Q.PRODUCT_ICON[c['product']]} *Your quote*\n━━━━━━━━━━━━━━━\n"
            f"{Q.PRODUCT_LABEL[c['product']]}: *{Q.fmt_amount(c['amount'])} {c['cur']}*\n"
            f"📈 Market rate: {fmt_rate(q['mid'])}\n🏷️ Our rate: {fmt_rate(q['rate'])} ({q['margin_pct']:.1f}% margin)\n"
            f"💱 Currency: {inr(q['subtotal'])}\n🚚 Fee: {fee}\n━━━━━━━━━━━━━━━\n💰 Total: *{inr(q['total'])}*\n\n"
            f"🔒 The rate is locked once you confirm. You'll need your {KYC_DOCS} for KYC, as RBI requires.",
            [("fcfm:yes", "✅ Confirm order"), ("fcfm:amt", "✏️ Change amount"), ("fcfm:no", "❌ Cancel")])]

    # ------------------------------------------------------------------- the order
    async def _do_order(self, s: Session) -> list[dict]:
        c = s.ctx["fx"]
        q = await self._quote_for(s)
        if q is None:
            return await self._unavailable(s)
        pay_online = self.payments is not None and self.payments.enabled
        order = await self._db(self.repo.create_order, s.user["id"], c["cur"], c["amount"], c["product"], q["mid"], q["rate"],
                               q["fee"], q["total"], "pending" if pay_online else "confirmed")
        self.reset(s)
        if pay_online:
            return await self._request_payment(s, order)
        s.step = "fx_menu"
        return self._confirmed(order)

    @staticmethod
    def _order_text(o: dict) -> str:
        return (f"🎫 Ref: *{o['ref']}*\n{Q.PRODUCT_ICON[o['product']]} {Q.PRODUCT_LABEL[o['product']]}: "
                f"*{Q.fmt_amount(float(o['foreign_amount']))} {o['currency']}*\n🏷️ Rate: {fmt_rate(float(o['rate']))} (locked)\n"
                f"💰 Total: *{inr(o['total_inr'])}*")

    def _confirmed(self, o: dict) -> list[dict]:
        how = "arrange delivery" if o["product"] == "cash" else "issue and load your card"
        return [text_msg(f"🎉 *Order confirmed!*\n━━━━━━━━━━━━━━━\n{self._order_text(o)}"),
                buttons_msg(f"📞 Our forex desk will message you here within 2 working hours to verify your KYC and {how}.\n\n"
                            f"🪪 Keep ready: {KYC_DOCS}.",
                            [("fx:orders", "📋 My orders"), ("fx:buy", "💱 Another currency"), ("nav:menu", "🏠 Menu")])]

    # ---------------------------------------------------------------------- payment
    async def _request_payment(self, s: Session, o: dict) -> list[dict]:
        try:
            link = await self.payments.create_link(o["total_inr"], o["ref"], f"Forex {o['foreign_amount']} {o['currency']} ({o['product']})",
                                                   s.phone, s.user.get("name") or "", PAYMENT_WINDOW_MIN)
            await self._db(self.repo.create_payment, o["id"], s.user["id"], o["total_inr"], link["id"], link["short_url"],
                           datetime.now(IST) + timedelta(minutes=PAYMENT_WINDOW_MIN + 1))  # +1 min grace for the webhook
        except Exception:
            logger.exception("Could not create a forex payment link")
            await self._db(self.repo.cancel_order, o)
            return [buttons_msg("😕 I couldn't set up the payment right now, so nothing was ordered. Please try again in a minute.",
                                [("fx:buy", "💱 Try again"), ("nav:menu", "🏠 Menu")])]
        s.ctx["fxpay"] = {"order_id": o["id"], "link_id": link["id"]}
        s.step = "awaiting_fx_payment"
        test = "\n🧪 Test mode: no real money is charged." if getattr(self.payments, "test_mode", False) else ""
        return [
            cta_msg(f"💳 *Almost done!* Pay *{inr(o['total_inr'])}* to confirm your order.\n{self._order_text(o)}\n"
                    f"⏳ The rate is held for {PAYMENT_WINDOW_MIN} minutes.{test}", f"Pay {inr(o['total_inr'])}", link["short_url"]),
            buttons_msg("I'll confirm your order here the moment the payment goes through ✅",
                        [("fpay:check", "✅ I've paid"), ("fpay:cancel", "❌ Cancel order")]),
        ]

    async def _on_payment_tap(self, s: Session, action: str) -> list[dict]:
        pay = s.ctx.get("fxpay")
        o = await self._db(self.repo.get_order_with_user, pay["order_id"]) if pay else None
        if not o:
            s.ctx.pop("fxpay", None)
            return await self.on_enter(s)
        if action == "cancel":
            if o["status"] != "pending":
                return [buttons_msg("This order is already paid, so I can't drop it here. Use *My orders* to cancel it.",
                                    [("fx:orders", "📋 My orders"), ("nav:menu", "🏠 Menu")])]
            await self._cancel_pending(o, pay["link_id"])
            s.ctx.pop("fxpay", None)
            return [buttons_msg("No problem, I've cancelled it. 👍", [("fx:buy", "💱 Buy currency"), ("nav:menu", "🏠 Menu")])]
        if o["status"] in ("confirmed", "fulfilled"):  # the webhook beat the tap
            s.ctx.pop("fxpay", None)
            return [buttons_msg(f"✅ Already confirmed! Your reference is *{o['ref']}*.",
                                [("fx:orders", "📋 My orders"), ("nav:menu", "🏠 Menu")])]
        if o["status"] == "cancelled":
            s.ctx.pop("fxpay", None)
            return [buttons_msg("⌛ The payment window ended and the rate expired. Want a fresh quote?",
                                [("fx:buy", "💱 Buy currency"), ("nav:menu", "🏠 Menu")])]
        try:
            paid = await self.payments.link_status(pay["link_id"]) == "paid"
        except Exception:
            paid = False
        confirmed = await self._db(self.repo.mark_paid, pay["link_id"]) if paid else None
        if not confirmed:
            return [buttons_msg("I haven't received the payment yet. If you've just paid, give it a few seconds and tap again. 🙏",
                                [("fpay:check", "✅ I've paid"), ("fpay:cancel", "❌ Cancel order")])]
        s.ctx.pop("fxpay", None)
        s.step = "fx_menu"
        return self._confirmed(confirmed)

    async def _cancel_pending(self, o: dict, link_id: str | None) -> None:
        await self._db(self.repo.cancel_payment, o["id"])
        await self._db(self.repo.cancel_order, o)
        if link_id and self.payments:
            try:
                await self.payments.cancel_link(link_id)
            except Exception:
                logger.warning("Could not cancel the payment link %s", link_id)

    async def confirm_payment(self, link_id: str) -> tuple[str, list[dict]] | None:
        """Razorpay webhook: the link was paid. Returns (whatsapp number, messages) to send, or None if it isn't ours."""
        o = await self._db(self.repo.mark_paid, link_id)
        if not o:
            return None
        phone = o["users"]["phone"]
        convo = await self._db(self.repo.get_conversation, phone)
        ctx = dict((convo or {}).get("context") or {})  # (keep ctx["fxpay"]: a late "I've paid" tap then gets a friendly answer)
        await self._db(self.repo.save_conversation, phone, "fx_menu", ctx)
        return phone.lstrip("+"), self._confirmed(o)

    async def release_expired(self) -> list[tuple[str, list[dict]]]:
        """Sweeper: cancel orders nobody paid for in time. Returns (whatsapp number, messages) to notify."""
        expired = await self._db(self.repo.expire_unpaid, datetime.now(IST))
        return [(o["users"]["phone"].lstrip("+"),
                 [buttons_msg(f"⌛ The payment window for your *{o['currency']}* order ({o['ref']}) ended, so the rate has expired. "
                              "Want a fresh quote?", [("fx:buy", "💱 Buy currency"), ("nav:menu", "🏠 Menu")])]) for o in expired]

    # -------------------------------------------------------------------- my orders
    async def _show_orders(self, s: Session) -> list[dict]:
        orders = await self._db(self.repo.list_user_orders, s.user["id"])
        if not orders:
            s.step = "fx_menu"
            return [buttons_msg("You have no forex orders yet. Your first one is just a few taps away! 🙂",
                                [("fx:buy", "💱 Buy currency"), ("fx:rates", "📈 Live rates"), ("nav:menu", "🏠 Menu")])]
        s.step = "fx_menu"
        rows = [(f"fbk:{o['id']}", f"{o['ref']} · {o['currency']}",
                 f"{STATUS_ICON.get(o['status'], '')} {o['status'].title()} · {Q.fmt_amount(float(o['foreign_amount']))} {o['currency']} · "
                 f"{inr(o['total_inr'])}") for o in orders]
        rows.append(("nav:menu", "🏠 Main menu", ""))
        return [list_msg("📋 *Your forex orders*\nPick one to see details 👇", "View orders", rows, "Recent orders")]

    @staticmethod
    def _can_cancel(o: dict) -> bool:
        return o["status"] in ("pending", "confirmed")

    async def _show_order(self, s: Session, order_id: str) -> list[dict]:
        o = await self._db(self.repo.get_order, order_id, s.user["id"])
        if not o:
            return await self.on_enter(s)
        text = f"{STATUS_ICON.get(o['status'], '')} *{o['status'].title()}*\n{self._order_text(o)}"
        if o["status"] == "pending":
            text += "\n\n⏳ Waiting for the payment. Use the payment link I sent, or it will be cancelled automatically."
        elif o["status"] == "confirmed":
            text += "\n\n📞 Our forex desk will contact you for KYC and delivery."
        elif o["status"] == "fulfilled":
            text += "\n\n📦 Delivered. Safe travels!"
        buttons = [("fx:orders", "📋 All orders"), ("nav:menu", "🏠 Menu")]
        if self._can_cancel(o):
            buttons.insert(0, (f"fbkc:{o['id']}", "❌ Cancel order"))
        return [buttons_msg(text, buttons)]

    async def _ask_cancel(self, s: Session, order_id: str) -> list[dict]:
        o = await self._db(self.repo.get_order, order_id, s.user["id"])
        if not o or not self._can_cancel(o):
            return await self.on_enter(s)
        refund = "Your payment will be refunded in 5-7 working days." if o["status"] == "confirmed" else "Nothing has been charged."
        return [buttons_msg(f"⚠️ Cancel order *{o['ref']}* ({Q.fmt_amount(float(o['foreign_amount']))} {o['currency']})?\n\n{refund}",
                            [(f"fbkcy:{o['id']}", "Yes, cancel it"), (f"fbk:{o['id']}", "No, keep it")])]

    async def _do_cancel(self, s: Session, order_id: str) -> list[dict]:
        o = await self._db(self.repo.get_order, order_id, s.user["id"])
        if not o or not self._can_cancel(o):
            return await self.on_enter(s)
        paid = o["status"] == "confirmed"
        await self._cancel_pending(o, (s.ctx.get("fxpay") or {}).get("link_id") if not paid else None)
        s.ctx.pop("fxpay", None)
        s.step = "fx_menu"
        refund = f"💸 Your {inr(o['total_inr'])} refund has been initiated." if paid else ""
        return [buttons_msg(f"😢 Order *{o['ref']}* has been cancelled.\n{refund}".strip(),
                            [("fx:orders", "📋 My orders"), ("fx:buy", "💱 Buy currency")])]

    # ------------------------------------------------------------ admin (forex desk)
    async def update_status(self, ref: str, status: str, note: str | None = None) -> tuple[str, list[dict]]:
        """The forex desk marks a paid order as delivered. Returns (whatsapp number, messages) for the customer."""
        o = await self._db(self.repo.get_order_by_ref, ref)
        if not o:
            raise LookupError(ref)
        if status != "fulfilled":
            raise ValueError("status must be 'fulfilled'")
        if not await self._db(self.repo.set_status, o, "fulfilled", ("confirmed",)):
            raise ValueError(f"order is {o['status']}, only a confirmed order can be fulfilled")
        how = "delivered" if o["product"] == "cash" else "issued and loaded"
        return o["users"]["phone"].lstrip("+"), [buttons_msg(
            f"📦 Your *{o['currency']}* order *{o['ref']}* has been {how}. Safe travels! ✈️" + (f"\n\n{note}" if note else ""),
            [("fx:orders", "📋 My orders"), ("nav:menu", "🏠 Menu")])]
