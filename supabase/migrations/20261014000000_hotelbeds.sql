-- Live hotels from Hotelbeds are mirrored into our own tables, so bookings, payments and "My Stays" work unchanged.
-- Safe to re-run: everything is "if not exists".

alter table public.hotels      add column if not exists hb_code     integer;       -- Hotelbeds hotel code
alter table public.hotel_rooms add column if not exists hb_rate_key text;          -- rate key from the latest search (needed to book at Hotelbeds)
create unique index if not exists idx_hotels_hb_code on public.hotels (hb_code) where hb_code is not null;

-- Hotelbeds has no star-style guest rating in a plain search, so a hotel may have none.
alter table public.hotels alter column rating drop not null;
