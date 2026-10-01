-- Travel concierge: flight booking schema
-- Run in the Supabase SQL editor, or via `supabase db push`.

create extension if not exists pgcrypto;  -- gen_random_uuid() on older PG versions

-- ---------------------------------------------------------------- airports
create table if not exists public.airports (
    code    text primary key check (code = upper(code) and char_length(code) = 3),
    city    text not null,
    name    text not null,
    country text not null
);

-- ----------------------------------------------------------------- flights
create table if not exists public.flights (
    id             uuid primary key default gen_random_uuid(),
    airline        text not null,
    flight_no      text not null,
    from_code      text not null references public.airports (code),
    to_code        text not null references public.airports (code),
    departure_time timestamptz not null,
    arrival_time   timestamptz not null,
    duration_min   int  not null check (duration_min > 0),
    price_inr      int  not null check (price_inr >= 0),
    class          text not null default 'Economy' check (class in ('Economy', 'Business')),
    seats_left     int  not null default 0 check (seats_left >= 0),
    baggage_kg     int  not null default 15 check (baggage_kg >= 0),
    stops          int  not null default 0 check (stops >= 0),
    refundable     boolean not null default false,
    status         text not null default 'scheduled'
                   check (status in ('scheduled', 'delayed', 'cancelled')),
    check (from_code <> to_code),
    check (arrival_time > departure_time)
);

-- Main search path: "flights from X to Y on date D"
create index if not exists idx_flights_route_departure
    on public.flights (from_code, to_code, departure_time);
create index if not exists idx_flights_from_code      on public.flights (from_code);
create index if not exists idx_flights_to_code        on public.flights (to_code);
create index if not exists idx_flights_departure_time on public.flights (departure_time);

-- ------------------------------------------------------------------- users
create table if not exists public.users (
    id          uuid primary key default gen_random_uuid(),
    name        text not null,
    phone       text not null unique check (phone ~ '^\+91[0-9]{10}$'),
    preferences jsonb not null default '{}'::jsonb,   -- e.g. {"seat": "window", "meal": "veg"}
    created_at  timestamptz not null default now()
);

-- --------------------------------------------------------------- bookings
create table if not exists public.bookings (
    id              uuid primary key default gen_random_uuid(),
    pnr             text not null unique check (pnr ~ '^[A-Z0-9]{6}$'),
    user_id         uuid not null references public.users (id),
    flight_id       uuid not null references public.flights (id),
    passenger_name  text not null,
    status          text not null default 'pending'
                    check (status in ('pending', 'confirmed', 'cancelled')),
    total_price_inr int  not null check (total_price_inr >= 0),
    created_at      timestamptz not null default now()
);

create index if not exists idx_bookings_user_id   on public.bookings (user_id);
create index if not exists idx_bookings_flight_id on public.bookings (flight_id);

-- ---------------------------------------------------------- conversations
-- WhatsApp bot state, one row per phone number.
create table if not exists public.conversations (
    id           uuid primary key default gen_random_uuid(),
    phone        text not null unique,
    current_step text not null default 'start',
    context      jsonb not null default '{}'::jsonb,
    updated_at   timestamptz not null default now()
);

create or replace function public.set_updated_at() returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_conversations_updated_at on public.conversations;
create trigger trg_conversations_updated_at
    before update on public.conversations
    for each row execute function public.set_updated_at();

-- -------------------------------------------------------------------- RLS
-- The bot uses the service-role key (bypasses RLS). Enabling RLS with no
-- policies keeps the anon/public API key from reading user or booking data.
alter table public.airports      enable row level security;
alter table public.flights       enable row level security;
alter table public.users         enable row level security;
alter table public.bookings      enable row level security;
alter table public.conversations enable row level security;
