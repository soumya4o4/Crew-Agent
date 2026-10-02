-- Optional own photo for a hotel (public https link, JPG or PNG, up to 5 MB).
-- Hotels without one still get a stock photo chosen by the app, so this column can stay empty.
alter table public.hotels add column if not exists image_url text;
