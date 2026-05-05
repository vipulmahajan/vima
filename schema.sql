-- ViMa Supabase Schema v2 — Fresh install
-- Primary key is now uuid id on users (not phone).
-- Phone is optional (WhatsApp Phase 2); email is optional (web users).
-- Run in Supabase SQL Editor. Safe to re-run — drops and recreates all tables.

-- ─────────────────────────────────────────────────────────────────────────────
-- Extensions
-- ─────────────────────────────────────────────────────────────────────────────
create extension if not exists "uuid-ossp";
create extension if not exists "pg_trgm";

-- ─────────────────────────────────────────────────────────────────────────────
-- Drop existing tables (clean slate)
-- ─────────────────────────────────────────────────────────────────────────────
drop table if exists user_documents  cascade;
drop table if exists otp_codes       cascade;
drop table if exists sessions        cascade;
drop table if exists artifacts       cascade;
drop table if exists payments        cascade;
drop table if exists user_state      cascade;
drop table if exists conversations   cascade;
drop table if exists users           cascade;

-- ─────────────────────────────────────────────────────────────────────────────
-- Shared trigger function
-- ─────────────────────────────────────────────────────────────────────────────
create or replace function touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- users
-- id is the primary key. Both phone and email are optional + unique.
-- Web users: email + google_id filled, phone null until payment step.
-- WhatsApp users: phone filled, email null.
-- ─────────────────────────────────────────────────────────────────────────────
create table users (
  id          uuid        primary key default uuid_generate_v4(),
  phone       text        unique,                   -- E.164, nullable for web users
  email       text        unique,                   -- nullable for WhatsApp users
  name        text,
  google_id   text        unique,
  avatar_url  text,
  locale      text        not null default 'en',
  channel     text        not null default 'web'
                          check (channel in ('web', 'whatsapp')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index users_phone   on users (phone)   where phone  is not null;
create index users_email   on users (email)   where email  is not null;
create index users_google  on users (google_id) where google_id is not null;

create trigger users_updated_at
  before update on users
  for each row execute function touch_updated_at();

alter table users enable row level security;
create policy "service role full access" on users
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- conversations
-- user_id stores email (web) or phone (WhatsApp) — no FK, flexible.
-- ─────────────────────────────────────────────────────────────────────────────
create table conversations (
  id          uuid        primary key default uuid_generate_v4(),
  user_id     text        not null,   -- email or phone, no FK
  role        text        not null check (role in ('user', 'assistant')),
  content     text        not null,
  created_at  timestamptz not null default now()
);

create index conversations_user_created
  on conversations (user_id, created_at desc);

alter table conversations enable row level security;
create policy "service role full access" on conversations
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- user_state
-- user_id stores email (web) or phone (WhatsApp) — no FK, flexible.
-- ─────────────────────────────────────────────────────────────────────────────
create table user_state (
  user_id         text        primary key,  -- email or phone
  flow            text        not null default 'idle'
                              check (flow in ('idle', 'resume', 'interview')),
  resume_step     text        not null default 'welcome',
  interview_step  text        not null default 'await_target_role',
  data            jsonb       not null default '{}'::jsonb,
  updated_at      timestamptz not null default now()
);

create trigger user_state_updated_at
  before update on user_state
  for each row execute function touch_updated_at();

alter table user_state enable row level security;
create policy "service role full access" on user_state
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- payments
-- user_id stores email (web) or phone (WhatsApp) — no FK, flexible.
-- payment_type:
--   access_pass     → 60 days (current)
--   monthly_renewal → 30 days (Phase 1.5)
-- ─────────────────────────────────────────────────────────────────────────────
create table payments (
  id                  uuid        primary key default uuid_generate_v4(),
  user_id             text        not null,   -- email or phone, no FK
  amount_paise        integer     not null check (amount_paise > 0),
  link_id             text,
  razorpay_payment_id text,
  payment_type        text        not null default 'access_pass'
                                  check (payment_type in ('access_pass', 'monthly_renewal')),
  status              text        not null default 'created'
                                  check (status in ('created', 'paid', 'expired', 'cancelled')),
  period_end          timestamptz,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

create index payments_user_status   on payments (user_id, status);
create index payments_link_id       on payments (link_id);
create index payments_user_type     on payments (user_id, payment_type, status);

create trigger payments_updated_at
  before update on payments
  for each row execute function touch_updated_at();

alter table payments enable row level security;
create policy "service role full access" on payments
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- artifacts
-- user_id stores email (web) or phone (WhatsApp) — no FK, flexible.
-- ─────────────────────────────────────────────────────────────────────────────
create table artifacts (
  id           uuid        primary key default uuid_generate_v4(),
  user_id      text        not null,   -- email or phone, no FK
  kind         text        not null check (kind in ('resume', 'interview_report')),
  storage_path text        not null,
  version      integer     not null default 1,
  created_at   timestamptz not null default now()
);

create index artifacts_user_kind
  on artifacts (user_id, kind, created_at desc);

alter table artifacts enable row level security;
create policy "service role full access" on artifacts
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- sessions
-- Web auth sessions. Stores JWT hash only, never raw token.
-- user_id = email for web users.
-- ─────────────────────────────────────────────────────────────────────────────
create table sessions (
  id              uuid        primary key default gen_random_uuid(),
  user_id         text        not null,
  jwt_token_hash  text        not null,
  expires_at      timestamptz not null,
  created_at      timestamptz not null default now()
);

create index sessions_user_expires on sessions (user_id, expires_at);
create index sessions_token_hash   on sessions (jwt_token_hash);

alter table sessions enable row level security;
create policy "service role full access" on sessions
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- user_documents
-- Uploaded files (resumes, JDs). user_id = email or phone.
-- ─────────────────────────────────────────────────────────────────────────────
create table user_documents (
  id          uuid        primary key default gen_random_uuid(),
  user_id     text        not null,
  type        text        not null
                          check (type in ('resume', 'jd', 'other')),
  filename    text,
  mime_type   text,
  raw_text    text        not null default '',
  created_at  timestamptz not null default now()
);

create index user_documents_user_type
  on user_documents (user_id, type, created_at desc);

alter table user_documents enable row level security;
create policy "service role full access" on user_documents
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- otp_codes (kept for WhatsApp Phase 2 OTP if needed later)
-- ─────────────────────────────────────────────────────────────────────────────
create table otp_codes (
  id          uuid        primary key default gen_random_uuid(),
  phone       text        not null,
  code_hash   text        not null,
  expires_at  timestamptz not null,
  attempts    integer     not null default 0,
  verified    boolean     not null default false,
  created_at  timestamptz not null default now()
);

create index otp_codes_phone_verified_expires
  on otp_codes (phone, verified, expires_at);

alter table otp_codes enable row level security;
create policy "service role full access" on otp_codes
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- Storage bucket reminder
-- Create manually: Supabase Dashboard → Storage → New bucket
-- Name: vima-artifacts | Private: true | Max file size: 10MB
-- ─────────────────────────────────────────────────────────────────────────────
