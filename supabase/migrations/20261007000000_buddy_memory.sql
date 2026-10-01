-- Buddy: what the bot remembers about a user between chats (short facts a friend would remember).
-- "forget me" deletes these rows (and the chat history in `messages`). Safe to re-run.

create table if not exists public.user_memories (
    id         uuid primary key default gen_random_uuid(),
    user_id    uuid not null references public.users (id) on delete cascade,
    category   text not null check (category in ('about', 'people', 'plans', 'worries', 'likes')),
    content    text not null check (char_length(content) between 3 and 200),
    created_at timestamptz not null default now()
);
create index if not exists idx_user_memories_user on public.user_memories (user_id, created_at desc);
alter table public.user_memories enable row level security;
