-- The bot now serves travellers from any country, so a WhatsApp number may be any international number (E.164).
alter table public.users drop constraint if exists users_phone_check;
alter table public.users add constraint users_phone_check check (phone ~ '^\+[0-9]{8,15}$');
