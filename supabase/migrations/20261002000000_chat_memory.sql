-- Chat memory: every user/bot message, so the Concierge can use recent context when routing.
-- Run in the Supabase SQL editor (the bot keeps working without it, it just won't remember history).

create table if not exists public.messages (
    id         uuid primary key default gen_random_uuid(),
    user_id    uuid not null references public.users (id) on delete cascade,
    role       text not null check (role in ('user', 'assistant')),
    agent      text,                       -- which agent was active: flight, hotel, ...
    content    text not null,
    created_at timestamptz not null default now()
);

create index if not exists idx_messages_user_created on public.messages (user_id, created_at desc);

alter table public.messages enable row level security;
