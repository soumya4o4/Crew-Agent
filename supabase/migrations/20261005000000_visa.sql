-- Visa assistant: rules (indicative, for Indian passport holders), applications, documents, status timeline.
-- Payments become shared between flights and visas.

create table if not exists public.visa_rules (
    country_code      text not null,                -- ISO-2, or SCHENGEN
    country_name      text not null,
    flag              text not null default '',
    purpose           text not null check (purpose in ('tourist', 'business')),
    visa_type         text not null check (visa_type in ('e-visa', 'sticker', 'visa-free', 'on-arrival')),
    fee_inr           int  not null default 0,      -- government + our service fee
    processing_days   int  not null default 0,
    max_stay_days     int  not null default 0,
    passport_min_months int not null default 6,     -- passport must be valid this long after travel
    docs              text[] not null default '{}', -- required document codes
    notes             text not null default '',
    primary key (country_code, purpose)
);

create table if not exists public.visa_applications (
    id              uuid primary key default gen_random_uuid(),
    ref             text not null unique check (ref ~ '^VS[A-Z0-9]{5}$'),
    user_id         uuid not null references public.users (id),
    country_code    text not null,
    purpose         text not null check (purpose in ('tourist', 'business')),
    visa_type       text not null,
    travel_date     date not null,
    stay_days       int  not null,
    applicant_name  text,
    passport_no     text,
    passport_expiry date,
    dob             date,
    fee_inr         int  not null,
    status          text not null default 'draft' check (status in
                    ('draft', 'payment_pending', 'submitted', 'in_review', 'approved', 'rejected', 'cancelled')),
    visa_file_path  text,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
create index if not exists idx_visa_apps_user on public.visa_applications (user_id, created_at desc);
drop trigger if exists trg_visa_apps_updated_at on public.visa_applications;
create trigger trg_visa_apps_updated_at before update on public.visa_applications
    for each row execute function public.set_updated_at();

create table if not exists public.visa_documents (
    id             uuid primary key default gen_random_uuid(),
    application_id uuid not null references public.visa_applications (id) on delete cascade,
    doc_type       text not null,
    storage_path   text,                            -- null for documents we attach ourselves (e.g. flight itinerary)
    mime           text,
    status         text not null default 'uploaded' check (status in ('uploaded', 'verified', 'rejected', 'auto')),
    issue          text,
    extracted      jsonb not null default '{}'::jsonb,
    created_at     timestamptz not null default now()
);
create index if not exists idx_visa_docs_app on public.visa_documents (application_id);

create table if not exists public.visa_events (
    id             uuid primary key default gen_random_uuid(),
    application_id uuid not null references public.visa_applications (id) on delete cascade,
    status         text not null,
    note           text,
    created_at     timestamptz not null default now()
);
create index if not exists idx_visa_events_app on public.visa_events (application_id, created_at);

-- Payments now serve flights and visas.
alter table public.payments add column if not exists kind text not null default 'flight' check (kind in ('flight', 'visa'));
alter table public.payments add column if not exists visa_application_id uuid references public.visa_applications (id) on delete cascade;
alter table public.payments alter column booking_id drop not null;
alter table public.payments drop constraint if exists payments_target_check;
alter table public.payments add constraint payments_target_check check (
    (kind = 'flight' and booking_id is not null) or (kind = 'visa' and visa_application_id is not null));

alter table public.visa_rules        enable row level security;
alter table public.visa_applications enable row level security;
alter table public.visa_documents    enable row level security;
alter table public.visa_events       enable row level security;
