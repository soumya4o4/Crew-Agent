-- Events: things happening in a city (concerts, comedy, food fests...), with coordinates so the bot can show what is near.
-- Safe to re-run: everything is "if not exists".

create table if not exists public.events (
    id          uuid primary key default gen_random_uuid(),
    city_code   text not null references public.airports (code),
    title       text not null,
    category    text not null check (category in ('music', 'comedy', 'food', 'sports', 'art', 'festival', 'workshop')),
    venue       text not null,
    area        text not null default '',
    lat         double precision not null,
    lon         double precision not null,
    starts_at   timestamptz not null,
    price_inr   int  not null default 0 check (price_inr >= 0),   -- 0 = free entry
    description text not null default ''
);
create index if not exists idx_events_city_start on public.events (city_code, starts_at);
alter table public.events enable row level security;
