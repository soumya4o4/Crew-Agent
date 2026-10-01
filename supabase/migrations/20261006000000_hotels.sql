-- Hotels: hotels, room types, bookings. Payments now also serve hotel bookings.
-- Safe to re-run: everything is "if not exists".

create table if not exists public.hotels (
    id          uuid primary key default gen_random_uuid(),
    city_code   text not null references public.airports (code),
    name        text not null,
    area        text not null,
    stars       int  not null check (stars between 1 and 5),
    rating      numeric(2, 1) not null,
    amenities   text[] not null default '{}',
    description text not null default '',
    unique (city_code, name)
);
create index if not exists idx_hotels_city on public.hotels (city_code);

-- One row per room type of a hotel. `rooms_total` rooms of that type exist; availability is
-- rooms_total minus the bookings that overlap the requested nights.
create table if not exists public.hotel_rooms (
    id          uuid primary key default gen_random_uuid(),
    hotel_id    uuid not null references public.hotels (id) on delete cascade,
    room_type   text not null check (room_type in ('Standard', 'Deluxe', 'Suite')),
    bed         text not null,
    max_guests  int  not null check (max_guests between 1 and 6),
    price_inr   int  not null check (price_inr > 0),     -- per night
    rooms_total int  not null check (rooms_total > 0),
    unique (hotel_id, room_type)
);
create index if not exists idx_hotel_rooms_hotel on public.hotel_rooms (hotel_id);

-- A booking stays 'pending' (room held) until its Razorpay link is paid or expires.
create table if not exists public.hotel_bookings (
    id              uuid primary key default gen_random_uuid(),
    ref             text not null unique check (ref ~ '^HB[A-Z0-9]{5}$'),
    user_id         uuid not null references public.users (id),
    hotel_id        uuid not null references public.hotels (id),
    room_id         uuid not null references public.hotel_rooms (id),
    guest_name      text not null,
    guests          int  not null check (guests between 1 and 6),
    check_in        date not null,
    check_out       date not null,
    total_price_inr int  not null check (total_price_inr > 0),
    status          text not null default 'confirmed' check (status in ('pending', 'confirmed', 'cancelled')),
    created_at      timestamptz not null default now(),
    check (check_out > check_in)
);
create index if not exists idx_hotel_bookings_user on public.hotel_bookings (user_id, created_at desc);
create index if not exists idx_hotel_bookings_room on public.hotel_bookings (room_id, check_in, check_out)
    where status <> 'cancelled';

-- Payments now serve flights, visas and hotels.
alter table public.payments add column if not exists hotel_booking_id uuid references public.hotel_bookings (id) on delete cascade;
alter table public.payments drop constraint if exists payments_kind_check;
alter table public.payments add constraint payments_kind_check check (kind in ('flight', 'visa', 'hotel'));
alter table public.payments drop constraint if exists payments_target_check;
alter table public.payments add constraint payments_target_check check (
    (kind = 'flight' and booking_id is not null) or
    (kind = 'visa'   and visa_application_id is not null) or
    (kind = 'hotel'  and hotel_booking_id is not null));

alter table public.hotels         enable row level security;
alter table public.hotel_rooms    enable row level security;
alter table public.hotel_bookings enable row level security;
