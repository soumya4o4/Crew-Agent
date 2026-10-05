-- Live flights from Duffel. The `flights` table stops being a hand-made timetable: every search mirrors the offers Duffel
-- returned into it (so bookings keep pointing at a real row), and the offer id is what we book against.
-- Safe to re-run: everything is "if not exists".

alter table public.flights add column if not exists duffel_offer_id    text;
alter table public.flights add column if not exists offer_expires_at   timestamptz;
alter table public.flights add column if not exists offer_passenger_ids jsonb not null default '[]'::jsonb;  -- Duffel's id per traveller, in order
alter table public.flights add column if not exists offer_pax          int  not null default 1;               -- travellers this offer was priced for
alter table public.flights add column if not exists offer_total        numeric(12, 2);                         -- what Duffel charges us for the whole offer...
alter table public.flights add column if not exists offer_currency     text;                                   -- ...in this currency
alter table public.flights add column if not exists itinerary_key      text;                                   -- "6E-123@2026-11-02T09:30:00|..." to find the same flights again
alter table public.flights add column if not exists checked_bags       int  not null default 0 check (checked_bags >= 0);
create unique index if not exists uq_flights_duffel_offer on public.flights (duffel_offer_id);

-- Duffel does not say how many seats are left or what bags weigh, and quotes more cabins than two.
alter table public.flights drop constraint if exists flights_class_check;
alter table public.flights alter column seats_left set default 9;

-- The airline ticket behind a booking, and what the airline needs to issue it.
alter table public.bookings add column if not exists duffel_order_id   text;
alter table public.bookings add column if not exists airline_pnr       text;                              -- the airline's booking reference (Duffel booking_reference)
alter table public.bookings add column if not exists passenger_details jsonb not null default '[]'::jsonb; -- [{name, dob, gender}]
alter table public.bookings add column if not exists contact           jsonb not null default '{}'::jsonb; -- {email, phone}

-- The old hand-made flights are gone. Ones that a booking still points at stay (a trip somebody paid for is history).
delete from public.flights f
 where f.duffel_offer_id is null
   and not exists (select 1 from public.bookings b where b.flight_id = f.id);

-- Offers older than a day that no booking uses; called now and then so the mirror does not grow forever.
create or replace function public.prune_flights() returns void as $$
    delete from public.flights f
     where f.offer_expires_at < now() - interval '1 day'
       and not exists (select 1 from public.bookings b where b.flight_id = f.id);
$$ language sql;
