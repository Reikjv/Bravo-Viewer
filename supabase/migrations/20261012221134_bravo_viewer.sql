-- Bravo Viewer: esquema independente do SQLite do ROV/Desktop.
-- Aplicar no projeto escolhido, como administrador, após conferir objetos existentes.
-- Criação do bucket por SQL é suportada pelo Supabase. Objetos usam a API Storage.
-- O rascunho docs/supabase/schema.sql da outra sessão não é esta migração.
begin;

create table if not exists public.bravo_members (
  user_id uuid primary key references auth.users(id) on delete cascade,
  display_name text not null default '',
  role text not null check (role in ('operador', 'gestao')),
  fleet_access boolean not null default true,
  enabled boolean not null default true,
  created_at timestamptz not null default now()
);

create table if not exists public.bravo_devices (
  id text primary key check (id ~ '^[0-9a-f]{32}$'),
  equipment text not null check (length(btrim(equipment)) between 1 and 100),
  created_at timestamptz not null default now(),
  created_by uuid references auth.users(id) on delete set null,
  updated_at timestamptz not null default now(),
  updated_by uuid references auth.users(id) on delete set null
);

create table if not exists public.bravo_device_access (
  device_id text not null references public.bravo_devices(id) on delete cascade,
  user_id uuid not null references public.bravo_members(user_id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (device_id, user_id)
);
create index if not exists bravo_device_access_user_idx
  on public.bravo_device_access(user_id, device_id);

create table if not exists public.bravo_operations (
  device_id text not null references public.bravo_devices(id) on delete cascade,
  op_id bigint not null check (op_id > 0),
  name text not null check (length(btrim(name)) between 1 and 200),
  location text not null default '',
  client text not null default '',
  notes text not null default '',
  started_at timestamptz,
  ended_at timestamptz,
  status text not null check (status in ('open', 'closed')),
  summary jsonb not null default '{}' check (jsonb_typeof(summary) = 'object'),
  created_at timestamptz not null default now(),
  created_by uuid references auth.users(id) on delete set null,
  updated_at timestamptz not null default now(),
  updated_by uuid references auth.users(id) on delete set null,
  primary key (device_id, op_id),
  check (ended_at is null or started_at is null or ended_at >= started_at),
  check (status <> 'closed' or ended_at is not null)
);
create index if not exists bravo_operations_history_idx
  on public.bravo_operations(device_id, started_at desc);

create table if not exists public.bravo_sessions (
  device_id text not null references public.bravo_devices(id) on delete cascade,
  source_key text not null check (length(source_key) between 1 and 2048),
  operation_id bigint,
  name text not null,
  kind text not null check (kind in ('tlog', 'bin', 'log', 'live')),
  sha256 text check (sha256 ~ '^[0-9a-f]{64}$'),
  size bigint check (size >= 0),
  records bigint check (records >= 0),
  source_revision double precision not null default 0
    check (source_revision >= 0 and source_revision < 'Infinity'::double precision),
  summary jsonb not null default '{}' check (jsonb_typeof(summary) = 'object'),
  analysis jsonb not null default '{}' check (jsonb_typeof(analysis) = 'object'),
  note text not null default '',
  storage_path text,
  created_at timestamptz not null default now(),
  created_by uuid references auth.users(id) on delete set null,
  updated_at timestamptz not null default now(),
  updated_by uuid references auth.users(id) on delete set null,
  primary key (device_id, source_key),
  foreign key (device_id, operation_id)
    references public.bravo_operations(device_id, op_id) on delete cascade,
  check (storage_path is null or
    (sha256 is not null and storage_path ~
      ('^' || device_id || '/' || sha256 || '\.(tlog|bin|log|gz)$'))),
  check (kind <> 'live' or (sha256 is null and storage_path is null))
);
create index if not exists bravo_sessions_operation_idx
  on public.bravo_sessions(device_id, operation_id);
create index if not exists bravo_sessions_storage_idx
  on public.bravo_sessions(storage_path) where storage_path is not null;

-- Índices para as FKs de auditoria usadas ao remover uma conta em Auth.
create index if not exists bravo_devices_created_by_idx on public.bravo_devices(created_by);
create index if not exists bravo_devices_updated_by_idx on public.bravo_devices(updated_by);
create index if not exists bravo_operations_created_by_idx on public.bravo_operations(created_by);
create index if not exists bravo_operations_updated_by_idx on public.bravo_operations(updated_by);
create index if not exists bravo_sessions_created_by_idx on public.bravo_sessions(created_by);
create index if not exists bravo_sessions_updated_by_idx on public.bravo_sessions(updated_by);

insert into storage.buckets(id, name, public)
values ('bravo-logs', 'bravo-logs', false) on conflict (id) do nothing;
do $$
begin
  if exists (select 1 from storage.buckets where id = 'bravo-logs' and public) then
    raise exception 'O bucket bravo-logs precisa ser privado antes de aplicar a migração';
  end if;
end
$$;

-- As funções consultam permissões atuais no banco, e não papéis antigos no JWT.
-- SECURITY DEFINER evita recursão nas políticas; todas têm search_path fixo.
create or replace function public.bravo_current_role() returns text
language sql stable security definer set search_path = '' as $$
  select role from public.bravo_members
  where user_id = (select auth.uid()) and enabled
$$;

create or replace function public.bravo_can_access_device(target_device text)
returns boolean language sql stable security definer set search_path = '' as $$
  select exists (
    select 1 from public.bravo_members m
    where m.user_id = (select auth.uid()) and m.enabled
      and (m.role = 'gestao' or m.fleet_access or exists (
        select 1 from public.bravo_device_access a
        where a.user_id = m.user_id and a.device_id = target_device
      ))
  )
$$;

create or replace function public.bravo_can_access_log(object_name text)
returns boolean language sql stable security definer set search_path = '' as $$
  select object_name ~ '^[0-9a-f]{32}/[0-9a-f]{64}\.(tlog|bin|log|gz)$'
    and exists (select 1 from public.bravo_devices d
      where d.id = split_part(object_name, '/', 1)
        and public.bravo_can_access_device(d.id))
$$;

create or replace function public.bravo_stamp_row() returns trigger
language plpgsql set search_path = '' as $$
begin
  if tg_op = 'INSERT' then
    new.created_at := now();
    new.created_by := coalesce(auth.uid(), new.created_by);
  else
    if tg_table_name = 'bravo_devices' then
      if new.id <> old.id then
        raise exception 'Identidade do equipamento é imutável' using errcode = '23514';
      end if;
    elsif tg_table_name = 'bravo_operations' then
      if new.device_id <> old.device_id or new.op_id <> old.op_id then
        raise exception 'Identidade da operação é imutável' using errcode = '23514';
      end if;
    elsif tg_table_name = 'bravo_sessions' then
      if new.device_id <> old.device_id or new.source_key <> old.source_key then
        raise exception 'Identidade da sessão é imutável' using errcode = '23514';
      end if;
    end if;
    new.created_at := old.created_at;
    new.created_by := old.created_by;
  end if;
  new.updated_at := now();
  new.updated_by := coalesce(auth.uid(), new.updated_by);
  return new;
end
$$;

create or replace function public.bravo_assign_creator() returns trigger
language plpgsql security definer set search_path = '' as $$
begin
  if public.bravo_current_role() = 'operador' then
    insert into public.bravo_device_access(device_id, user_id)
    values (new.id, auth.uid()) on conflict do nothing;
  end if;
  return new;
end
$$;

create or replace function public.bravo_guard_source_update() returns trigger
language plpgsql set search_path = '' as $$
begin
  -- Um segundo PC atrasado não deve substituir uma revisão mais nova.
  if tg_table_name = 'bravo_sessions' then
    if new.source_revision < old.source_revision then return null; end if;
  elsif old.status = 'closed' and new.status = 'open' then
    -- Uma cópia anterior ao encerramento não reabre a operação na nuvem.
    return null;
  end if;
  return new;
end
$$;

-- Cadastro com retorno após atribuir o operador. INSERT ... RETURNING direto
-- avaliaria SELECT antes da atribuição e falharia para um ROV ainda desconhecido.
create or replace function public.bravo_register_device(target_device text, equipment_name text)
returns public.bravo_devices language plpgsql security definer set search_path = '' as $$
declare result public.bravo_devices;
begin
  if coalesce(public.bravo_current_role(), '') not in ('operador', 'gestao') then
    raise exception 'Usuário sem acesso ao Bravo Viewer' using errcode = '42501';
  end if;
  if exists (select 1 from public.bravo_devices where id = target_device)
      and not public.bravo_can_access_device(target_device) then
    raise exception 'Equipamento não atribuído a este usuário' using errcode = '42501';
  end if;
  insert into public.bravo_devices(id, equipment) values (target_device, equipment_name)
    on conflict (id) do nothing;
  -- Revalidar após INSERT protege também dois PCs cadastrando o mesmo ROV.
  if not public.bravo_can_access_device(target_device) then
    raise exception 'Equipamento não atribuído a este usuário' using errcode = '42501';
  end if;
  update public.bravo_devices set equipment = equipment_name where id = target_device;
  select * into result from public.bravo_devices where id = target_device;
  return result;
end
$$;

alter table public.bravo_members enable row level security;
alter table public.bravo_devices enable row level security;
alter table public.bravo_device_access enable row level security;
alter table public.bravo_operations enable row level security;
alter table public.bravo_sessions enable row level security;

revoke all on public.bravo_members, public.bravo_device_access,
  public.bravo_devices, public.bravo_operations, public.bravo_sessions
  from public, anon, authenticated;
grant select on public.bravo_members to authenticated;
grant select, insert, update, delete on public.bravo_device_access,
  public.bravo_devices, public.bravo_operations, public.bravo_sessions
  to authenticated;
grant all on public.bravo_members, public.bravo_device_access,
  public.bravo_devices, public.bravo_operations, public.bravo_sessions
  to service_role;

revoke all on function public.bravo_current_role(),
  public.bravo_can_access_device(text), public.bravo_can_access_log(text),
  public.bravo_stamp_row(), public.bravo_assign_creator(),
  public.bravo_guard_source_update(), public.bravo_register_device(text, text)
  from public, anon, authenticated;
grant execute on function public.bravo_current_role(),
  public.bravo_can_access_device(text), public.bravo_can_access_log(text)
  to authenticated, service_role;
grant execute on function public.bravo_register_device(text, text) to authenticated;

drop policy if exists bravo_members_read on public.bravo_members;
create policy bravo_members_read on public.bravo_members for select to authenticated
  using (user_id = (select auth.uid()) or (select public.bravo_current_role()) = 'gestao');

drop policy if exists bravo_access_read on public.bravo_device_access;
create policy bravo_access_read on public.bravo_device_access for select to authenticated
  using (user_id = (select auth.uid()) or (select public.bravo_current_role()) = 'gestao');
drop policy if exists bravo_access_manage on public.bravo_device_access;
create policy bravo_access_manage on public.bravo_device_access for all to authenticated
  using ((select public.bravo_current_role()) = 'gestao')
  with check ((select public.bravo_current_role()) = 'gestao');

do $$
declare t text;
begin
  foreach t in array array['bravo_devices', 'bravo_operations', 'bravo_sessions'] loop
    execute format('drop trigger if exists bravo_stamp on public.%I', t);
    execute format('create trigger bravo_stamp before insert or update on public.%I for each row execute function public.bravo_stamp_row()', t);
    execute format('drop policy if exists bravo_read on public.%I', t);
    execute format('drop policy if exists bravo_insert on public.%I', t);
    execute format('drop policy if exists bravo_update on public.%I', t);
    execute format('drop policy if exists bravo_delete on public.%I', t);
    execute format('create policy bravo_read on public.%I for select to authenticated using (public.bravo_can_access_device(%I))', t, case when t = 'bravo_devices' then 'id' else 'device_id' end);
    if t = 'bravo_devices' then
      execute format('create policy bravo_insert on public.%I for insert to authenticated with check ((select public.bravo_current_role()) = ''gestao'')', t);
    else
      execute format('create policy bravo_insert on public.%I for insert to authenticated with check (public.bravo_can_access_device(device_id))', t);
    end if;
    execute format('create policy bravo_update on public.%I for update to authenticated using (public.bravo_can_access_device(%I)) with check (public.bravo_can_access_device(%I))', t, case when t = 'bravo_devices' then 'id' else 'device_id' end, case when t = 'bravo_devices' then 'id' else 'device_id' end);
    execute format('create policy bravo_delete on public.%I for delete to authenticated using ((select public.bravo_current_role()) = ''gestao'')', t);
  end loop;
end
$$;

drop trigger if exists bravo_assign_creator on public.bravo_devices;
create trigger bravo_assign_creator after insert on public.bravo_devices
  for each row execute function public.bravo_assign_creator();
drop trigger if exists bravo_guard_source on public.bravo_sessions;
create trigger bravo_guard_source before update on public.bravo_sessions
  for each row execute function public.bravo_guard_source_update();
drop trigger if exists bravo_guard_source on public.bravo_operations;
create trigger bravo_guard_source before update on public.bravo_operations
  for each row execute function public.bravo_guard_source_update();

-- Objetos originais são imutáveis: INSERT + SELECT, sem sobrescrita por UPDATE.
-- RLS de Storage não altera políticas de outros buckets.
drop policy if exists bravo_v1_logs_read on storage.objects;
drop policy if exists bravo_v1_logs_insert on storage.objects;
drop policy if exists bravo_v1_logs_delete on storage.objects;
create policy bravo_v1_logs_read on storage.objects for select to authenticated
  using (bucket_id = 'bravo-logs' and public.bravo_can_access_log(name));
create policy bravo_v1_logs_insert on storage.objects for insert to authenticated
  with check (bucket_id = 'bravo-logs' and public.bravo_can_access_log(name));
create policy bravo_v1_logs_delete on storage.objects for delete to authenticated
  using (bucket_id = 'bravo-logs' and (select public.bravo_current_role()) = 'gestao');

notify pgrst, 'reload schema';
commit;
