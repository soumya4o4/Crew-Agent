"""The trip roadmap: pure functions, no I/O. Given where, when and what the traveller's entry rules are, it works out
the steps of the whole journey, from leaving home to getting back home, in the order they should be done, with the date each
one is due. Lead times are rules of thumb (visas take the longest, so they set the pace); the agent shows them as advice,
never as a promise."""
from dataclasses import dataclass
from datetime import date, timedelta

# How far ahead of the trip each step should be done (days).
LEAD = {"flight_intl": 45, "flight_dom": 21, "hotel_intl": 30, "hotel_dom": 14, "insurance": 14, "forex": 10, "plan": 10,
        "rides": 2, "pack": 2, "visa_arrival": 7, "return": 3}
VISA_LEAD_DEFAULT = {"e_visa": 7, "required": 30, None: 30}  # days a visa usually takes when our rules table doesn't say
VISA_BUFFER = 7                                                  # spare days before the visa is needed
STATE_ICON = {"done": "✅", "progress": "🔄", "todo": "⬜"}


@dataclass
class Step:
    id: str
    title: str
    why: str
    due: date
    state: str = "todo"            # todo | progress | done
    note: str = ""                 # what we found, e.g. "Visa application is in review"


def visa_lead_days(status: str | None, processing_days: int | None) -> int:
    """How many days before the trip the visa must be started."""
    base = processing_days if processing_days else VISA_LEAD_DEFAULT.get(status, 30)
    return base + VISA_BUFFER


def build_steps(*, travel: date, days: int = 7, intl: bool, round_trip: bool = True, visa_status: str | None,
                visa_processing_days: int | None = None) -> list[Step]:
    """Every step of the journey, soonest due first. `visa_status` is None when we couldn't tell (then a visa check is a step).
    The last step, the return day, only exists for a round trip."""
    d = lambda n: travel - timedelta(days=n)
    back = travel + timedelta(days=days)
    steps: list[Step] = []
    if intl:
        lead = visa_lead_days(visa_status, visa_processing_days)
        steps.append(Step("passport", "Check your passport", "It must be valid for at least 6 months after you return and have "
                          "blank pages. Renewing takes weeks, so check first.", d(max(lead + 7, LEAD["flight_intl"] + 5))))
        if visa_status in ("e_visa", "required", None):
            what = {"e_visa": "Apply for your e-Visa", "required": "Apply for your visa", None: "Check visa rules"}[visa_status]
            why = {"e_visa": "You apply online before you fly.", "required": "You apply at the embassy or visa centre, so start early.",
                   None: "Entry rules depend on your passport. Find out before you pay for anything."}[visa_status]
            steps.append(Step("visa", what, why + (f" It usually takes about {visa_processing_days} days." if visa_processing_days else ""), d(lead)))
        elif visa_status == "on_arrival":
            steps.append(Step("visa_arrival", "Prepare for visa on arrival", "You get the visa at the airport. Carry the fee in cash, "
                              "passport photos, your hotel booking and a return ticket.", d(LEAD["visa_arrival"])))
    flights_why = ("Book the way there and the way back together: it is usually cheaper, and many countries want to see a return ticket."
                   if round_trip else "Fares usually rise as the date gets closer, so book early.")
    steps.append(Step("flights", "Book your flights" if not round_trip else "Book flights, there and back", flights_why,
                      d(LEAD["flight_intl" if intl else "flight_dom"])))
    steps.append(Step("hotel", "Book your stay", f"Check-in {travel:%a %d %b}" + (f", check-out {back:%a %d %b}" if round_trip else "")
                      + ". Book near what you want to see, with free cancellation if you can.", d(LEAD["hotel_intl" if intl else "hotel_dom"])))
    if intl:
        steps.append(Step("insurance", "Buy travel insurance", "It covers medical bills, cancellations and lost bags. Some countries "
                          "ask for it at the border.", d(LEAD["insurance"])))
        steps.append(Step("forex", "Get foreign currency", "Take a mix: a forex card for most spending and a little cash for small things.",
                          d(LEAD["forex"])))
    else:
        steps.append(Step("plan", "Plan what to do", "Pick a few places and food to try, so the days don't slip away.", d(LEAD["plan"])))
    steps.append(Step("rides", "Plan your rides", "From your home to the airport and from the airport to your stay"
                      + (", then back again on the way home." if round_trip else "."), d(LEAD["rides"])))
    steps.append(Step("pack", "Pack and check your documents", "Passport and tickets, hotel booking, chargers and adapters, "
                      "medicines, and a copy of everything on your phone.", d(LEAD["pack"])))
    if round_trip:
        steps.append(Step("return", "Return day checklist", "Check out on time and settle your bills, use up leftover currency, keep your "
                          "boarding pass and receipts, and leave plenty of time to reach the airport. A ride home from the airport "
                          "is worth booking too.", back - timedelta(days=LEAD["return"])))
    return sorted(steps, key=lambda s: s.due)


def apply_progress(steps: list[Step], done: set[str], progress: dict, round_trip: bool = True) -> list[Step]:
    """Mark what is already done: things the traveller ticked off, and things we can see in their bookings.
    progress = {"flight": bool (there), "flight_back": bool, "hotel": bool, "forex": bool, "visa": None | "approved" | other status}."""
    for s in steps:
        if s.id in done:
            s.state = "done"
        elif s.id == "flights":
            out, back = progress.get("flight"), progress.get("flight_back")
            if out and (back or not round_trip):
                s.state, s.note = "done", "Booked with us" + (", there and back" if round_trip else "")
            elif out or back:
                s.state, s.note = "progress", "Way there booked, return still to book" if out else "Return booked, way there still to book"
        elif s.id in ("hotel", "forex") and progress.get(s.id):
            s.state, s.note = "done", {"hotel": "Stay booked with us", "forex": "Order placed"}[s.id]
        elif s.id == "visa" and progress.get("visa"):
            approved = progress["visa"] == "approved"
            s.state, s.note = ("done", "Visa approved") if approved else ("progress", f"Application: {progress['visa'].replace('_', ' ')}")
    return steps


def next_step(steps: list[Step]) -> Step | None:
    return next((s for s in steps if s.state != "done"), None)


def when_text(due: date, today: date, state: str = "todo") -> str:
    """"by Tue 06 Oct" / "overdue" / "today" — short enough for a list row."""
    if state == "done":
        return "Done"
    days = (due - today).days
    if days < 0:
        return "⚠️ Do this now"
    if days == 0:
        return "⏰ Today"
    return f"by {due:%a %d %b}" + (" ⏰" if days <= 3 else "")


def timeline_warning(steps: list[Step], today: date, travel: date) -> str | None:
    """A heads-up when the trip is closer than the steps need (mostly visas), so the date can be moved while it is cheap."""
    late = [s for s in steps if s.due < today and s.state != "done" and s.id in ("visa", "passport", "flights")]
    if not late:
        return None
    days = (travel - today).days
    names = " and ".join(s.title.lower() for s in late[:2])
    return (f"⚠️ You travel in {days} days, and normally *{names}* should already be underway. It may still work out, but "
            "consider moving your date if the visa can't arrive in time.")
