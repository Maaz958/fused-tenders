-- FUSED Tender Intelligence: shared Bid / No Bid decisions
-- Run once in Supabase: Dashboard > SQL Editor > New query > paste all of this > Run.
-- FIRST change 'CHANGE-ME-team-passcode' below to the passcode you and Andy will use.

create extension if not exists pgcrypto with schema extensions;

create table if not exists public.decisions (
  id          text primary key,              -- the tender's OCID
  decision    text not null check (decision in ('bid', 'nobid')),
  title       text,
  updated_by  text,
  updated_at  timestamptz not null default now()
);

create table if not exists public.team_passcode (hash text not null);

-- Lock both tables: with RLS on and no policies, the public key cannot read or write them directly.
-- All access goes through the two functions below, which check the passcode first.
alter table public.decisions     enable row level security;
alter table public.team_passcode enable row level security;

delete from public.team_passcode;
insert into public.team_passcode (hash)
values (extensions.crypt('CHANGE-ME-team-passcode', extensions.gen_salt('bf')));

create or replace function public.fused_check_pass(p text)
returns boolean language sql security definer set search_path = public, extensions as $$
  select exists (select 1 from public.team_passcode where hash = extensions.crypt(coalesce(p, ''), hash));
$$;

create or replace function public.get_decisions(p_pass text)
returns setof public.decisions language plpgsql security definer set search_path = public, extensions as $$
begin
  if not public.fused_check_pass(p_pass) then
    raise exception 'invalid passcode';
  end if;
  return query select * from public.decisions;
end $$;

create or replace function public.set_decision(p_pass text, p_id text, p_decision text, p_by text, p_title text default null)
returns void language plpgsql security definer set search_path = public, extensions as $$
begin
  if not public.fused_check_pass(p_pass) then
    raise exception 'invalid passcode';
  end if;
  if coalesce(p_decision, '') = '' then
    delete from public.decisions where id = p_id;           -- toggled back to "To review"
  else
    insert into public.decisions (id, decision, title, updated_by, updated_at)
    values (p_id, p_decision, left(p_title, 500), left(p_by, 80), now())
    on conflict (id) do update
      set decision = excluded.decision, title = excluded.title,
          updated_by = excluded.updated_by, updated_at = now();
  end if;
end $$;

-- Only the two passcode-checked functions are callable from the web page.
revoke all on function public.fused_check_pass(text) from public, anon, authenticated;
revoke all on function public.get_decisions(text) from public;
revoke all on function public.set_decision(text, text, text, text, text) from public;
grant execute on function public.get_decisions(text) to anon, authenticated;
grant execute on function public.set_decision(text, text, text, text, text) to anon, authenticated;

-- To change the passcode later, run just this line with the new value:
-- update public.team_passcode set hash = extensions.crypt('NEW-PASSCODE', extensions.gen_salt('bf'));
