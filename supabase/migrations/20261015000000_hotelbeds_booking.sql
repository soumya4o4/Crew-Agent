-- The Hotelbeds booking reference of a stay (null for hotels from our own database). Needed to cancel it at Hotelbeds.
alter table public.hotel_bookings add column if not exists hb_reference text;
