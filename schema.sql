-- ViMa Supabase Schema
-- Run this in the Supabase SQL editor (Dashboard → SQL → New query).
-- All tables live in the public schema.
-- RLS is enabled on every table; service-role key bypasses it from the backend.

-- ─────────────────────────────────────────────────────────────────────────────
-- Extensions
-- ─────────────────────────────────────────────────────────────────────────────
create extension if not exists "uuid-ossp";
create extension if not exists "pg_trgm";   -- for future fuzzy search


-- ─────────────────────────────────────────────────────────────────────────────
-- users
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists users (
  phone       text        primary key,          -- E.164, e.g. 919876543210
  name        text,
  locale      text        not null default 'en',
  -- The channel the user reaches us on. New users default to web; users who
  -- arrive via the Gupshup webhook are upserted with channel='whatsapp'.
  channel     text        not null default 'web'
                          check (channel in ('web', 'whatsapp')),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- ── Migration for existing deployments ─────────────────────────────────────
-- Idempotent: safe to re-run.
do $$
begin
  if not exists (
    select 1 from information_schema.columns
    where table_name = 'users' and column_name = 'channel'
  ) then
    alter table users
      add column channel text not null default 'web'
      check (channel in ('web', 'whatsapp'));
  end if;

  if not exists (
    select 1 from information_schema.columns
    where table_name = 'users' and column_name = 'email'
  ) then
    alter table users add column email text unique;
  end if;

  if not exists (
    select 1 from information_schema.columns
    where table_name = 'users' and column_name = 'google_id'
  ) then
    alter table users add column google_id text unique;
  end if;

  if not exists (
    select 1 from information_schema.columns
    where table_name = 'users' and column_name = 'avatar_url'
  ) then
    alter table users add column avatar_url text;
  end if;
end$$;

alter table users enable row level security;

DROP POLICY IF EXISTS "service role full access" ON "users";
create policy "service role full access" on users
  using (true) with check (true);

-- Keep updated_at fresh automatically.
create or replace function touch_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;
DROP TRIGGER IF EXISTS users_updated_at ON public.users;
create trigger users_updated_at
  before update on users
  for each row execute function touch_updated_at();


-- ─────────────────────────────────────────────────────────────────────────────
-- conversations
-- Stores every inbound and outbound message turn for context retrieval.
-- user_id accepts phone (WhatsApp) or email (web) — no FK so both work.
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists conversations (
  id          uuid        primary key default uuid_generate_v4(),
  phone       text        not null,     -- phone (WhatsApp) or email (web); no FK
  role        text        not null check (role in ('user', 'assistant')),
  content     text        not null,
  created_at  timestamptz not null default now()
);

-- ── Migration for existing deployments ─────────────────────────────────────
-- Drop the users(phone) FK from conversations so email addresses can also
-- be stored in the phone column. Idempotent — safe to re-run.
do $$
declare
  _conname text;
begin
  select conname into _conname
  from pg_constraint
  where conrelid = 'conversations'::regclass
    and contype = 'f'
    and conname ilike '%users%'
  limit 1;
  if _conname is not null then
    execute format('alter table conversations drop constraint %I', _conname);
  end if;
end$$;

create index if not exists conversations_phone_created
  on conversations (phone, created_at desc);

alter table conversations enable row level security;

DROP POLICY IF EXISTS "service role full access" ON conversations;
create policy "service role full access" on conversations
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- user_state
-- One row per user; tracks which flow they are in + step-level data.
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists user_state (
  phone           text        primary key,  -- phone (WhatsApp) or email (web); no FK
  flow            text        not null default 'idle'
                              check (flow in ('idle', 'resume', 'interview')),
  resume_step     text        not null default 'welcome',
  interview_step  text        not null default 'await_target_role',
  -- Arbitrary JSON blob for flow-specific state (parsed resume, target role, transcript, etc.)
  data            jsonb       not null default '{}'::jsonb,
  updated_at      timestamptz not null default now()
);

-- ── Migration: drop users(phone) FK so email keys work ──────────────────────
do $$
declare
  _conname text;
begin
  select conname into _conname
  from pg_constraint
  where conrelid = 'user_state'::regclass
    and contype = 'f'
    and conname ilike '%users%'
  limit 1;
  if _conname is not null then
    execute format('alter table user_state drop constraint %I', _conname);
  end if;
end$$;

DROP TRIGGER IF EXISTS user_state_updated_at ON public.user_state;
create trigger user_state_updated_at
  before update on user_state
  for each row execute function touch_updated_at();

alter table user_state enable row level security;

DROP POLICY IF EXISTS "service role full access" ON user_state;
create policy "service role full access" on user_state
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- payments
-- Tracks every Razorpay payment link created and its lifecycle.
-- period_end is set when status transitions to 'paid'. Duration depends on
-- payment_type:
--   * access_pass     → 60 days (one-time pass, current default)
--   * monthly_renewal → 30 days (Razorpay Subscriptions, Phase 1.5)
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists payments (
  id              uuid        primary key default uuid_generate_v4(),
  phone           text        not null references users(phone) on delete cascade,
  amount_paise    integer     not null check (amount_paise > 0),
  link_id         text,                         -- Razorpay payment_link id
  razorpay_payment_id text,                     -- Razorpay payment id (set on success)
  payment_type    text        not null default 'access_pass'
                              check (payment_type in ('access_pass', 'monthly_renewal')),
  status          text        not null default 'created'
                              check (status in ('created', 'paid', 'expired', 'cancelled')),
  period_end      timestamptz,                  -- subscription active until this timestamp
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now()
);

create index if not exists payments_phone_status on payments (phone, status);
create index if not exists payments_link_id      on payments (link_id);
create index if not exists payments_phone_type   on payments (phone, payment_type, status);

-- ── Migration for existing deployments ─────────────────────────────────────
-- If you already created the payments table before payment_type existed,
-- run this once. Idempotent — safe to run multiple times.
do $$
begin
  if not exists (
    select 1 from information_schema.columns
    where table_name = 'payments' and column_name = 'payment_type'
  ) then
    alter table payments
      add column payment_type text not null default 'access_pass'
      check (payment_type in ('access_pass', 'monthly_renewal'));
  end if;
end$$;

DROP TRIGGER IF EXISTS payments_updated_at ON public.payments;
create trigger payments_updated_at
  before update on payments
  for each row execute function touch_updated_at();

alter table payments enable row level security;

DROP POLICY IF EXISTS "service role full access" ON payments;
create policy "service role full access" on payments
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- artifacts
-- Tracks generated PDFs (resumes, interview reports) stored in Supabase Storage.
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists artifacts (
  id              uuid        primary key default uuid_generate_v4(),
  phone           text        not null references users(phone) on delete cascade,
  kind            text        not null check (kind in ('resume', 'interview_report')),
  storage_path    text        not null,          -- path within the vima-artifacts bucket
  version         integer     not null default 1,
  created_at      timestamptz not null default now()
);

create index if not exists artifacts_phone_kind on artifacts (phone, kind, created_at desc);

alter table artifacts enable row level security;

DROP POLICY IF EXISTS "service role full access" ON artifacts;
create policy "service role full access" on artifacts
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- otp_codes
-- Short-lived OTP records used during web phone-number verification.
-- One row per send attempt; verified=true marks a code as consumed.
-- ─────────────────────────────────────────────────────────────────────────────
do $$
begin
  if not exists (
    select 1 from information_schema.tables
    where table_schema = 'public' and table_name = 'otp_codes'
  ) then
    create table otp_codes (
      id          uuid        primary key default gen_random_uuid(),
      phone       text        not null,
      code_hash   text        not null,          -- SHA-256 hex of the 6-digit code
      expires_at  timestamptz not null,
      attempts    integer     not null default 0,
      verified    boolean     not null default false,
      created_at  timestamptz not null default now()
    );
  end if;
end$$;

create index if not exists otp_codes_phone_verified_expires
  on otp_codes (phone, verified, expires_at);

alter table otp_codes enable row level security;

drop policy if exists "service role full access" on otp_codes;
create policy "service role full access" on otp_codes
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- sessions
-- Tracks active web sessions issued after successful OTP verification.
-- The raw JWT is never stored — only its SHA-256 hash.
-- ─────────────────────────────────────────────────────────────────────────────
do $$
begin
  if not exists (
    select 1 from information_schema.tables
    where table_schema = 'public' and table_name = 'sessions'
  ) then
    create table sessions (
      id               uuid        primary key default gen_random_uuid(),
      user_id          text        not null,     -- email for web users
      jwt_token_hash   text        not null,     -- SHA-256 hex of the raw JWT
      expires_at       timestamptz not null,
      created_at       timestamptz not null default now()
    );
  end if;
end$$;

create index if not exists sessions_user_expires on sessions (user_id, expires_at);
create index if not exists sessions_token_hash   on sessions (jwt_token_hash);

alter table sessions enable row level security;

drop policy if exists "service role full access" on sessions;
create policy "service role full access" on sessions
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- user_documents
-- Stores extracted text from files uploaded via the web chat (resume, JD).
-- Indexed by user_id (email) + type so the flow can fetch the latest upload.
-- ─────────────────────────────────────────────────────────────────────────────
do $$
begin
  if not exists (
    select 1 from information_schema.tables
    where table_schema = 'public' and table_name = 'user_documents'
  ) then
    create table user_documents (
      id          uuid        primary key default gen_random_uuid(),
      user_id     text        not null,   -- email (web) or phone (whatsapp)
      type        text        not null    -- 'resume' | 'jd' | 'other'
                              check (type in ('resume', 'jd', 'other')),
      filename    text,
      mime_type   text,
      raw_text    text        not null default '',
      created_at  timestamptz not null default now()
    );
  end if;
end$$;

create index if not exists user_documents_user_type
  on user_documents (user_id, type, created_at desc);

alter table user_documents enable row level security;

drop policy if exists "service role full access" on user_documents;
create policy "service role full access" on user_documents
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- Storage bucket
-- Create in Supabase Dashboard → Storage, or via the management API.
-- SQL cannot create buckets directly; this is a reminder comment.
--
-- Bucket name : vima-artifacts
-- Public      : false  (all access via signed URLs)
-- File size   : 10 MB limit recommended
-- ─────────────────────────────────────────────────────────────────────────────
