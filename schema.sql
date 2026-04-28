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
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

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
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists conversations (
  id          uuid        primary key default uuid_generate_v4(),
  phone       text        not null references users(phone) on delete cascade,
  role        text        not null check (role in ('user', 'assistant')),
  content     text        not null,
  created_at  timestamptz not null default now()
);

create index if not exists conversations_phone_created
  on conversations (phone, created_at desc);

alter table conversations enable row level security;

create policy "service role full access" on conversations
  using (true) with check (true);


-- ─────────────────────────────────────────────────────────────────────────────
-- user_state
-- One row per user; tracks which flow they are in + step-level data.
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists user_state (
  phone           text        primary key references users(phone) on delete cascade,
  flow            text        not null default 'idle'
                              check (flow in ('idle', 'resume', 'interview')),
  resume_step     text        not null default 'welcome',
  interview_step  text        not null default 'await_target_role',
  -- Arbitrary JSON blob for flow-specific state (parsed resume, target role, transcript, etc.)
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

create trigger payments_updated_at
  before update on payments
  for each row execute function touch_updated_at();

alter table payments enable row level security;

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

create policy "service role full access" on artifacts
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
