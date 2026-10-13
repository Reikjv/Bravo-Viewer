-- Autorizações pré-cadastradas: a criação/definição de senha continua no Supabase Auth.
-- E-mails são dados administrativos, não ficam hardcoded na migração pública.
begin;
create table if not exists public.bravo_pending_members (
  email text primary key check (email = lower(btrim(email)) and position('@' in email) > 1),
  display_name text not null default '',
  role text not null check (role in ('operador', 'gestao')),
  fleet_access boolean not null default true,
  enabled boolean not null default true,
  created_at timestamptz not null default now()
);
alter table public.bravo_pending_members enable row level security;
revoke all on public.bravo_pending_members from public, anon, authenticated;
grant all on public.bravo_pending_members to service_role;

create or replace function public.bravo_enrol_auth_user() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  -- Um cadastro sem confirmação do e-mail ainda não recebe acesso à frota.
  if new.email_confirmed_at is not null then
    insert into public.bravo_members(user_id, display_name, role, fleet_access, enabled)
    select new.id, p.display_name, p.role, p.fleet_access, true
    from public.bravo_pending_members p
    where p.email = lower(new.email) and p.enabled
    on conflict (user_id) do nothing;
    -- Não reabilitar nem elevar um membro existente ao atualizar o usuário Auth.
  end if;
  return new;
end
$$;
revoke all on function public.bravo_enrol_auth_user()
  from public, anon, authenticated, service_role;
drop trigger if exists bravo_enrol_user on auth.users;
create trigger bravo_enrol_user after insert or update of email, email_confirmed_at
  on auth.users for each row execute function public.bravo_enrol_auth_user();
notify pgrst, 'reload schema';
commit;
