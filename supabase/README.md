# Supabase schema + dummy data

## What's here
- `migrations/20261001000000_travel_concierge_schema.sql`: tables (`airports`, `flights`, `users`, `bookings`, `conversations`), indexes, and RLS.
- `seed/seed_dummy_data.py`: generates and inserts dummy data.

## 1. Run the migration
Pick one:
- **SQL editor:** open Supabase Dashboard → SQL Editor, paste the migration file, and run it.
- **Supabase CLI:** `supabase link --project-ref <ref>`, then `supabase db push`.

The migration is idempotent (`if not exists`), so it can be re-run.

> If the Alembic models in `app/db/models` point at this same Supabase database, tables named `users`, `bookings` and `conversations` may already exist with different columns. In that case run the migration against a fresh project or schema, or reconcile the two first.

## 2. Run the seed script
Add these to the project-root `.env`. Use the **service-role** key from Project Settings → API. Never expose it client-side.

```
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
```

```
pip install -r supabase/seed/requirements.txt
python supabase/seed/seed_dummy_data.py
```

Re-running is safe. The script first deletes the dummy users (phones `+91500000000X`), their bookings and conversations, and **all** rows in `flights`. It then regenerates everything from today, so dates always cover the next 30 days.

## What gets generated
| Table | Rows | Notes |
|---|---|---|
| airports | 11 | 10 Indian airports + DXB |
| flights | about 4,400 | 15 routes in both directions (BOM↔DXB is the only international one), 4-6 flights per day per route, 30 days |
| users | 10 | Indian names, phones `+915000000001`–`+915000000010` |
| bookings | 15 | 8 confirmed, 3 pending, 4 cancelled; one confirmed booking is on a delayed flight |
| conversations | 3 | sample mid-flow bot states |

- **Fares:** domestic Economy is ₹3,000–9,000 and Dubai is ₹12,000–25,000. Fares vary by time of day (morning and evening cost more, night costs less) and by airline. They rise up to 30% for departures within 10 days and 5% on Fri/Sun. Air India Business fares are 2.2× Economy, so they sit above those ranges.
- **Edge cases:** about 3% sold out (`seats_left = 0`), 3% `delayed`, 2% `cancelled`, and about 7% of flights are 1-stop (longer duration, slightly cheaper).
- **Fake phones:** `+91 5…` is not an allocated Indian mobile range, so none of these numbers can reach a real person. If you want to test the bot over WhatsApp, edit `DUMMY_PHONES` in the script to use your own test number.

## Cabs, travellers and chat memory
Run `migrations/20261003000000_cabs_and_passengers.sql` (safe to re-run). It adds:
- `cab_places`, `cab_fares`, `cab_drivers`, `cab_bookings` for the Cab agent
- `bookings.passengers` for multi-traveller flight bookings
- the `messages` table (chat history)

Seed only the cab data, leaving flights and real bookings untouched:
```
python supabase/seed/seed_dummy_data.py --cabs-only
```
This creates 60 pickup/drop places (10 cities), 40 rate cards, 120 fake drivers and 6 sample rides. Driver names, phones (`+91 5…`) and plates are invented.

The full seed (no flag) also resets flights. It stops with an error if a real booking still references a flight, so real bookings are never deleted by accident.

## Payments
Run `migrations/20261004000000_payments.sql`: one `payments` row per Razorpay payment link, tied to a (pending) booking.

## Hotels
Run `migrations/20261006000000_hotels.sql` (safe to re-run): `hotels`, `hotel_rooms`, `hotel_bookings`, and `payments` now also
serves hotel stays (`kind = 'hotel'`).

Seed only the hotel data, leaving flights and real bookings untouched:
```
python supabase/seed/seed_dummy_data.py --hotels-only
```
This creates 44 invented hotels (4 per city, 11 cities), 124 rooms (Standard / Deluxe / Suite, no Suite in 2-star hotels) and 4 sample
stays. The full seed includes it too. It stops with an error if a real stay still references a hotel, so real bookings are never deleted.

## Events
Run `migrations/20261008000000_events.sql` (safe to re-run): the `events` table (city, venue with latitude/longitude, start time, price).
Seed only the events (regenerated from today, 110 invented listings at real public spots in the 11 cities):
```
python supabase/seed/seed_dummy_data.py --events-only
```
The full seed includes them too.

## Buddy memory
Run `migrations/20261007000000_buddy_memory.sql` (safe to re-run): the `user_memories` table. Buddy still chats without it, it just
won't remember anything.

## Visa
Run `migrations/20261005000000_visa.sql`: `visa_rules`, `visa_applications`, `visa_documents`, `visa_events`, and `payments`
now serves flights and visas (`kind`). Create a **private** Storage bucket named `visa-docs` (the bot uploads documents there).
Load or refresh the rules with `python supabase/seed/seed_dummy_data.py --visa-only` (safe to re-run; it never touches applications).
