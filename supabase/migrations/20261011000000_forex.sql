-- Forex: currency and forex-card orders. Payments now also serve forex orders.
-- Safe to re-run: everything is "if not exists" / "drop ... if exists".

create table if not exists public.forex_orders (
    id             uuid primary key default gen_random_uuid(),
    ref            text not null unique check (ref ~ '^FX[A-Z0-9]{5}$'),
    user_id        uuid not null references public.users (id),
    currency       text not null check (currency ~ '^[A-Z]{3}$'),
    foreign_amount numeric(14, 2) not null check (foreign_amount > 0),
    product        text not null check (product in ('cash', 'card')),
    mid_rate       numeric(14, 6) not null,              -- market rate (INR per 1 unit) when the order was placed
    rate           numeric(14, 6) not null,              -- the rate we gave the customer (mid + margin), locked for the order
    fee_inr        int  not null default 0,
    total_inr      int  not null check (total_inr > 0),
    -- pending = waiting for payment, confirmed = paid (our forex desk takes over), fulfilled = delivered / card issued
    status         text not null default 'confirmed' check (status in ('pending', 'confirmed', 'fulfilled', 'cancelled')),
    created_at     timestamptz not null default now()
);
create index if not exists idx_forex_orders_user on public.forex_orders (user_id, created_at desc);

-- Payments now serve flights, visas, hotels and forex.
alter table public.payments add column if not exists forex_order_id uuid references public.forex_orders (id) on delete cascade;
alter table public.payments drop constraint if exists payments_kind_check;
alter table public.payments add constraint payments_kind_check check (kind in ('flight', 'visa', 'hotel', 'forex'));
alter table public.payments drop constraint if exists payments_target_check;
alter table public.payments add constraint payments_target_check check (
    (kind = 'flight' and booking_id is not null) or
    (kind = 'visa'   and visa_application_id is not null) or
    (kind = 'hotel'  and hotel_booking_id is not null) or
    (kind = 'forex'  and forex_order_id is not null));

alter table public.forex_orders enable row level security;
