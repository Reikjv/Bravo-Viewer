-- Evita avaliar duas políticas permissivas para SELECT de atribuições.
begin;
drop policy if exists bravo_access_manage on public.bravo_device_access;
drop policy if exists bravo_access_insert on public.bravo_device_access;
drop policy if exists bravo_access_update on public.bravo_device_access;
drop policy if exists bravo_access_delete on public.bravo_device_access;
create policy bravo_access_insert on public.bravo_device_access for insert to authenticated
  with check ((select public.bravo_current_role()) = 'gestao');
create policy bravo_access_update on public.bravo_device_access for update to authenticated
  using ((select public.bravo_current_role()) = 'gestao')
  with check ((select public.bravo_current_role()) = 'gestao');
create policy bravo_access_delete on public.bravo_device_access for delete to authenticated
  using ((select public.bravo_current_role()) = 'gestao');
commit;
