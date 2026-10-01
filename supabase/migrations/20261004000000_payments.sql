-- Payments: one Razorpay payment link per pending booking.
-- A booking stays 'pending' (seats held) until the link is paid or expires.
create table if not exists public.payments (
    id         uuid primary key default gen_random_uuid(),
    booking_id uuid not null references public.bookings (id) on delete cascade,
    user_id    uuid not null references public.users (id),
    amount_inr int  not null check (amount_inr > 0),
    provider   text not null default 'razorpay',
    link_id    text not null unique,                 -- Razorpay payment link id (plink_...)
    short_url  text not null,
    status     text not null default 'created' check (status in ('created', 'paid', 'expired', 'cancelled')),
    expires_at timestamptz not null,
    paid_at    timestamptz,
    created_at timestamptz not null default now()
);
create index if not exists idx_payments_booking on public.payments (booking_id);
create index if not exists idx_payments_status_expiry on public.payments (status, expires_at);
alter table public.payments enable row level security;
