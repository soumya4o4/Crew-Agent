-- Cabs (places, fares, drivers, bookings) + multi-passenger flight bookings + chat memory.
-- Safe to re-run: everything is "if not exists".

-- ----------------------------------------------------------- flights: travellers per booking
alter table public.bookings add column if not exists passengers int not null default 1 check (passengers between 1 and 9);

-- ------------------------------------------------------------------- chat memory (idempotent)
create table if not exists public.messages (
    id         uuid primary key default gen_random_uuid(),
    user_id    uuid not null references public.users (id) on delete cascade,
    role       text not null check (role in ('user', 'assistant')),
    agent      text,
    content    text not null,
    created_at timestamptz not null default now()
);
create index if not exists idx_messages_user_created on public.messages (user_id, created_at desc);
alter table public.messages enable row level security;

-- ---------------------------------------------------------------------------------- cabs
-- Pickup/drop spots per city (airport, railway station, popular areas).
create table if not exists public.cab_places (
    id        uuid primary key default gen_random_uuid(),
    city_code text not null references public.airports (code),
    name      text not null,
    kind      text not null check (kind in ('airport', 'railway', 'area', 'mall', 'landmark')),
    lat       double precision not null,
    lon       double precision not null,
    unique (city_code, name)
);
create index if not exists idx_cab_places_city on public.cab_places (city_code);

-- Rate card per city and vehicle type.
create table if not exists public.cab_fares (
    city_code     text not null references public.airports (code),
    vehicle_type  text not null check (vehicle_type in ('Mini', 'Sedan', 'SUV', 'Luxury')),
    base_fare     int  not null,
    per_km        numeric(6, 2) not null,
    min_fare      int  not null,
    seats         int  not null,
    example_model text not null,
    primary key (city_code, vehicle_type)
);

create table if not exists public.cab_drivers (
    id           uuid primary key default gen_random_uuid(),
    name         text not null,
    phone        text not null unique,
    city_code    text not null references public.airports (code),
    vehicle_type text not null check (vehicle_type in ('Mini', 'Sedan', 'SUV', 'Luxury')),
    vehicle_model text not null,
    plate        text not null unique,
    rating       numeric(2, 1) not null,
    trips        int not null default 0
);
create index if not exists idx_cab_drivers_city_type on public.cab_drivers (city_code, vehicle_type);

create table if not exists public.cab_bookings (
    id             uuid primary key default gen_random_uuid(),
    ref            text not null unique check (ref ~ '^CB[A-Z0-9]{5}$'),
    user_id        uuid not null references public.users (id),
    city_code      text not null references public.airports (code),
    pickup_id      uuid not null references public.cab_places (id),
    drop_id        uuid not null references public.cab_places (id),
    pickup_time    timestamptz not null,
    vehicle_type   text not null check (vehicle_type in ('Mini', 'Sedan', 'SUV', 'Luxury')),
    distance_km    numeric(5, 1) not null,
    fare_inr       int not null,
    driver_id      uuid references public.cab_drivers (id),
    otp            text not null check (otp ~ '^[0-9]{4}$'),
    status         text not null default 'confirmed' check (status in ('confirmed', 'cancelled', 'completed')),
    cancel_fee_inr int not null default 0,
    created_at     timestamptz not null default now(),
    check (pickup_id <> drop_id)
);
create index if not exists idx_cab_bookings_user on public.cab_bookings (user_id, pickup_time desc);

alter table public.cab_places   enable row level security;
alter table public.cab_fares    enable row level security;
alter table public.cab_drivers  enable row level security;
alter table public.cab_bookings enable row level security;
