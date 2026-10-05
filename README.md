# WhatsApp Travel Concierge

A WhatsApp bot (Meta Cloud API + FastAPI + Supabase) that books flights, hotels and cabs, runs visa applications, finds
events and places near you, is a friend on the road (Buddy), turns a travel reel into a bookable trip, and will handle forex. One **Concierge** understands the user and hands each
request to a specialist **agent**.

## How a message flows
```
WhatsApp ─▶ api/routes/whatsapp.py ─▶ Concierge ─▶ specialist agent ─▶ messages (text / buttons / lists)
                                          │
                       state + chat history in Supabase (conversations, messages)
```
- **Button taps** carry ids like `from:IDR`. Each agent declares the id prefixes it `owns`, so the Concierge routes taps without guessing.
- **Free text** ("Indore to Goa tomorrow", "I need a hotel") goes through the router: Claude if `ANTHROPIC_API_KEY` is set, otherwise keyword rules. If the LLM fails, keywords take over.
- **Memory:** `conversations` keeps the current step and context per phone. `messages` keeps the chat history. `ctx["trip"]` is shared between agents (a flight booking tells the cab/hotel/events agents the destination and date).

## Layout
```
app/
  main.py                      FastAPI app
  api/routes/whatsapp.py       webhook (verify + receive)
  core/                        config, shared helpers (places, time utils, message builders)
  db/                          Supabase client + CoreRepo (users, conversations, messages)
  schemas/whatsapp.py          WhatsApp payload models
  services/                    whatsapp_service.py (send/receive), razorpay_service.py (payment links, refunds, webhook check),
                               duffel.py (live flights), hotelbeds.py (live hotels)
  agents/
    base.py                    Agent + Session contracts
    registry.py                wires every agent into the Concierge
    concierge/                 the router: concierge.py, router.py, classifiers.py, slots.py
    flight/                    LIVE (Duffel): agent.py, repo.py (mirrored flights/bookings), formatting.py, travellers.py (names + birth dates)
    cab/                       LIVE: agent.py, repo.py (places/fares/drivers/rides), pricing.py
    hotel/                     LIVE: agent.py, repo.py (hotels/rooms/stays), formatting.py
    visa/                      LIVE: see "Visa assistant" below
    nearby/                    LIVE: "Around Me": places near the user + directions (see below)
    events/                    LIVE: events near the user, from the `events` table (see below)
    buddy/                     LIVE (needs OPENAI_API_KEY): agent.py, brain.py (OpenAI call), trip.py (trip facts), repo.py
    planner/                   LIVE (needs OPENAI_API_KEY): reel -> trip plan. agent.py, analyzer.py (OpenAI), media.py (ffmpeg, links)
    forex/                     coming-soon agent
supabase/                      SQL migrations + dummy-data seed script
tests/                         pytest; fakes.py simulates a user without WhatsApp or Supabase
```

## Add or build out an agent
1. Create `app/agents/<name>/agent.py` with a class extending `Agent` (see `flight/agent.py`) or `ComingSoonAgent`.
   Set `name`, `title`, `emoji`, `owns` (its button-id prefixes) and implement `on_enter` / `process`.
2. Add its keywords to `PATTERNS` in `concierge/classifiers.py` (and the intent list for the LLM).
3. Add it to the list in `agents/registry.py`. It appears in the main menu automatically.
4. Tables for the new service go in a new file under `supabase/migrations/`.

## Run
```
python -m venv .venv && .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000     # then: .\ngrok.exe http 8000
pytest
```
Setup of the database and dummy data: see [supabase/README.md](supabase/README.md). Required `.env` keys are in `.env.example`.

**Webhook security:** `POST /webhook/whatsapp` only accepts calls signed by Meta (`X-Hub-Signature-256`, checked against
`WHATSAPP_APP_SECRET` = Meta app dashboard → Settings → Basic → App secret). Unsigned, wrongly signed or any call while the secret is
empty gets a 403, so nobody can send the bot fake messages. The Razorpay webhook is verified the same way with `RAZORPAY_WEBHOOK_SECRET`.

## Payments (Razorpay)
Flight bookings are paid online. Tapping *Confirm* prices the flight again with the airline, holds it as a `pending` booking and
sends a Razorpay payment link (valid 20 minutes). When the link is paid the booking becomes `confirmed`, **the airline ticket is issued
through Duffel** and the ticket (with the airline's PNR) is sent on WhatsApp. If the airline refuses the ticket after payment, the booking
is cancelled and the money is refunded on the same Razorpay payment automatically.
- **Webhook (recommended):** Razorpay dashboard → Settings → Webhooks → URL `https://<your-ngrok-url>/webhook/razorpay`, active event **Payment Link → paid**.
  Put the secret you choose there in `.env` as `RAZORPAY_WEBHOOK_SECRET`.
- **Without the webhook:** the *I've paid* button checks the payment with Razorpay, so bookings still confirm.
- **Unpaid bookings:** a background task (started with the app) cancels them after the window.
- **No keys in `.env`:** bookings confirm instantly, no payment. With `rzp_test_` keys no real money moves.
- Cabs are still pay-the-driver (cash or UPI).

## Hotels
`agents/hotel/` finds a stay on WhatsApp: city, check-in date, nights and guests (all four can come from one sentence like
*"hotel in Goa tomorrow"*, or from the flight just booked), then a list of hotels with the cheapest and top-rated tagged,
a room choice (Standard / Deluxe / Suite), the guest name, a summary, and payment.
- **Availability:** a room type has `rooms_total` rooms; a stay uses one room for its nights. Pending and confirmed stays both count,
  so an unpaid stay holds its room until the payment window ends. A booking is inserted first and the overlap re-counted, so two people
  racing for the last room can never both get it.
- **Payment:** same as flights. A Razorpay link (20 minutes), the webhook (`payment_link.paid`) confirms the stay and sends the
  voucher, *I've paid* is the backup, and the sweeper releases unpaid rooms. No keys in `.env`: stays confirm instantly.
- **Cancellation:** from *My Stays*. Free until 24 hours before check-in (2 PM); after that no refund is shown.
  The bot only tells the user a refund is initiated: no Razorpay refund is triggered yet.
- **Data:** live from Hotelbeds (`app/services/hotelbeds.py`): search, photos and amenities, booking and cancelling. Every city in the
  `airports` table (with coordinates) can be searched. Keys: `HOTELBEDS_API_KEY`, `HOTELBEDS_SECRET` in `.env`. The hotels and rooms tables
  are only a mirror of what Hotelbeds returned, so bookings and payments have rows to point at.

## Buddy (a friend for any problem)
`agents/buddy/` is the catch-all: when a message isn't a travel request (feelings, a worry, advice, chit-chat), the Concierge
hands it to Buddy instead of showing the menu. Buddy answers in the user's language (Hinglish by default), remembers what
matters, and offers Flights / Hotels / Cabs / Visa buttons when they genuinely fit. It exists only when `OPENAI_API_KEY` is set
(the whole project uses OpenAI; `OPENAI_MODEL` picks the model).
- **One call per message** (`brain.py`): returns the reply, up to 3 facts to remember, services to suggest, and a safety flag.
  The model's JSON is validated; unknown services, categories and risk values are dropped.
- **Memory:** short facts in `user_memories` (newest 60 kept, max 40 sent to the model, wrapped in `<notes>` as untrusted data).
  The prompt forbids storing passwords, OTPs, card/ID numbers and health diagnoses.
- **Control:** *what do you remember about me* lists the facts; *forget me* (after a Yes tap) deletes them and the stored chat
  history. Bookings and payments stay, they are business records. These commands work from anywhere in the bot.
- **Safety:** `core/safety.py` matches self-harm phrases (English and Hinglish) before anything else, without the LLM, and replies with
  Tele-MANAS (14416), iCall and 112. If the model flags `self_harm` or `danger` it appends the same help. Logs hold only the user id,
  never the message. **Re-check the helpline numbers before launch.** Not built yet: alerting your team (comes with human handoff).
- **Staying in the chat:** while the step is `buddy_chat`, typed text keeps going to Buddy, unless the user clearly asks for a service
  (*"flight to Goa"*) or says *my bookings* / *help*.

## Trip Planner: share a reel, get a trip
`agents/planner/` turns a travel reel into a trip. The user sends any of: a **link** (Instagram, YouTube, TikTok), a **video** (the
reel itself or a screen recording), a **screenshot**, or a few words (*"Goa 3 din"*). The bot finds where it was filmed and what to do
there, asks how many days, writes a day-by-day plan in Hinglish, and for the 11 cities we book it carries on into **flights, then
hotel (with the right number of nights), cab and events** using the same queue as a normal booking.
- **How a reel is read** (`media.py`, `analyzer.py`): ffmpeg takes 6 frames and the audio; Whisper transcribes the speech; one OpenAI
  vision call gets frames + transcript + caption and returns the place, spots, activities, vibe, best season and a suggested length
  (validated JSON; it may say "can't tell", which asks the user for a screenshot or the place name). A second, text-only call writes
  the plan. ffmpeg is the system binary if present, else the one bundled with `imageio-ffmpeg`; without either it works from the caption.
- **Links:** we never download a video from a link. YouTube and TikTok give a public title through oEmbed; Instagram's public page tags are
  tried and often refuse (login wall), in which case the bot asks for the video or a screenshot. Only https links to these sites are fetched.
- **Background job:** reading a video takes 20-30 seconds, so the bot answers at once ("Reel mil gayi...") and sends the result when ready;
  the conversation state is saved for it, like the Razorpay webhook does. Videos up to 16 MB (WhatsApp's limit).
- **Coverage:** a place outside our 11 cities still gets a plan and a Google Maps button, but no booking.
- **Privacy:** the video, frames and speech are sent to OpenAI for the analysis and are not stored; only the result (place, spots, days)
  stays in the conversation. Say so in your privacy text.
- **Without `OPENAI_API_KEY`** the Trip Planner stays a "coming soon" stub.

## On the go: location, Around Me, Events, trip companion
- **Location:** WhatsApp bots cannot receive a live GPS stream. The user shares a *location pin* (a one-tap "Send location" button
  the bot sends, or the 📎 menu) and we get its latitude/longitude. It is kept in the conversation (`ctx["loc"]`) for 3 hours, then
  dropped; *forget me* removes it too. Coordinates are sent to OpenStreetMap services (Nominatim, Overpass, OSRM), say so in your privacy text.
- **Directions:** a Google Maps button (`maps_link`). It needs no API key and starts from the phone's own GPS, so it works even if the
  user never shared a location. Drive times (no live traffic) come from OSRM.
- **Around Me** (`agents/nearby/`): cafés, restaurants, ATMs, pharmacies, petrol, sights, parks, malls, or search anything by name
  (*"biryani"*). Closest first, with distance and a Directions button. Free text works too (*"coffee near me"*). Places come from
  OpenStreetMap (Overpass; Nominatim is the fallback when the public Overpass servers are busy, which they often are). **Before launch
  switch `services/geo_service.py` to Google Places**: better India coverage, ratings, open-now and no shared-server limits.
- **Events** (`agents/events/`): listings from the `events` table (dummy data for now), closest first when we know where the user is
  (the pin maps to the nearest city we cover), otherwise soonest first in the city of the flight just booked. Entry is paid at the venue,
  ticket booking is not built.
- **Trip companion** (Buddy): each message Buddy gets the user's next flight, hotel and location as *facts computed by code*
  (`buddy/trip.py`: when to be at the airport, drive time, when to leave), so it never guesses times or status. It can also ask the app
  to act: send a Google Maps button to the airport or hotel, request the user's location, or hand over to Around Me / Events. Typed
  questions about a booked trip ("traffic mein fas gaya, flight miss na ho jaye") stay with Buddy; only clear booking requests
  ("book a flight to Goa") open the booking flow.
- **Not built yet:** proactive nudges ("time to leave", "did you reach?"), which need a scheduler and WhatsApp message templates outside
  the 24-hour window; gate and terminal info; real-time flight status (Duffel gives schedules and fares, not live delays).

## Flights (Duffel)
Flights are live, there is no timetable in our database. `app/services/duffel.py` searches any airport pair for a day, and every
search is mirrored into the `flights` table so bookings have a row to point at (old unbooked offers are pruned by `prune_flights()`).
- **Keys:** `DUFFEL_API_TOKEN` in `.env`. `duffel_test_…` uses Duffel's sandbox (fake airlines, no money); `duffel_live_…` issues real tickets
  and charges your Duffel balance. `DUFFEL_MARKUP_PCT` adds your margin. Fares come in the airline's currency and are shown in rupees per traveller.
- **Cities:** any airport in the `airports` table can be searched; add a row (code, city, name, country, lat, lon) to open a new city.
- **Travellers:** the airline needs each traveller's name, date of birth and gender. They are asked once in one message
  (*"Rahul Verma, 14/03/1992, M"*) and remembered in `users.preferences.travellers`. Offers that need passport details are not shown.
- **Booking:** the offer is re-priced at checkout (a dearer fare counts as sold out, a few rupees more are absorbed), the ticket is issued
  after payment, and cancelling asks the airline for its refund first, then refunds the traveller their share through Razorpay.
- **Not covered yet:** Duffel returns few Indian low-cost carriers (IndiGo, SpiceJet…); an India-domestic supplier (e.g. Tripjack, TBO) would sit
  next to Duffel behind the same `live` interface.

## Visa assistant
`agents/visa/` walks a user through a visa on WhatsApp: destination, purpose and dates, then whether a visa is needed
(e-visa, sticker, visa-free, on-arrival), the document list, document upload (photo or PDF), automatic checks, the form
data (read from the passport), the fee via Razorpay, and status tracking.
- **Document checks:** with `OPENAI_API_KEY` set, images are checked by a vision model (right document? readable? passport
  expiry at least 6 months after the trip, name and number read out for the user to confirm). PDFs and no-key setups get basic checks
  and a note that our team will review them by hand.
- **Rules data:** `visa_rules` is indicative (Indian passport holders). Refresh it from official sources before launch.
- **Submission and status:** the bot does not log in to embassy portals. After payment the application is `submitted` to your
  visa team, who move it along with the admin API; the user is notified on WhatsApp each time.
  ```
  POST /admin/visa/{ref}/status      header: X-Admin-Token: <ADMIN_TOKEN>
  {"status": "in_review" | "approved" | "rejected", "note": "...", "file_url": "https://.../visa.pdf"}
  ```
  Set `ADMIN_TOKEN` in `.env` (without it the endpoint answers 503).
- **Demo mode:** `VISA_DEMO_AUTOPROGRESS=true` moves submitted applications to *in review* after 90 s and *approved* after 4 min.
- Files are stored in the private Supabase bucket `visa-docs`.
