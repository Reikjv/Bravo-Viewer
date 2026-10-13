-- Administração: criar as contas pelo Supabase Auth antes de executar.
-- Substituir o e-mail do operador. Gestão é opcional nesta primeira configuração.
-- Senhas nunca entram neste arquivo.
-- Este script não cria contas, não confirma e-mails e não envia mensagens.
begin;
do $$
declare
  operator_email text := 'TROQUE_OPERADOR@exemplo.invalid';
  manager_email text := null; -- Preencher quando a conta de gestão for definida.
  operator_id uuid;
  manager_id uuid;
begin
  if operator_email like 'TROQUE_%' or coalesce(manager_email, '') like 'TROQUE_%' then
    raise exception 'Preencha o e-mail da conta existente antes de executar';
  end if;
  if lower(operator_email) = lower(manager_email) then
    raise exception 'Operador e gestão precisam de contas distintas';
  end if;
  select id into operator_id from auth.users where lower(email) = lower(operator_email);
  if operator_id is null then
    raise exception 'A conta de operador ainda não existe em Supabase Auth';
  end if;
  insert into public.bravo_members(user_id, display_name, role, fleet_access, enabled)
  values (operator_id, 'Operador', 'operador', true, true)
  on conflict (user_id) do update set
    role = excluded.role, fleet_access = excluded.fleet_access, enabled = excluded.enabled;
  if nullif(btrim(manager_email), '') is not null then
    select id into manager_id from auth.users where lower(email) = lower(manager_email);
    if manager_id is null then
      raise exception 'A conta de gestão ainda não existe em Supabase Auth';
    end if;
    insert into public.bravo_members(user_id, display_name, role, fleet_access, enabled)
    values (manager_id, 'Gestão', 'gestao', true, true)
    on conflict (user_id) do update set
      role = excluded.role, fleet_access = excluded.fleet_access, enabled = excluded.enabled;
  end if;
end
$$;
commit;

