-- One Razorpay payment link can now pay several bookings at once (a flight and a hotel bundle).
-- Each booking keeps its own row in `payments`; rows of one bundle share the same link_id, so it can no longer be unique.
alter table public.payments drop constraint if exists payments_link_id_key;
create index if not exists idx_payments_link on public.payments (link_id);
