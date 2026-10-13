-- Leitura antes de aplicar a migração no projeto escolhido.
select version();
select table_schema, table_name
from information_schema.tables
where table_schema = 'public'
  and (table_name like 'bravo_%' or table_name in ('devices', 'operations', 'sessions'))
order by table_name;
select schemaname, tablename, policyname, roles, cmd, qual, with_check
from pg_policies
where (schemaname = 'public' and tablename like 'bravo_%')
   or (schemaname = 'storage' and tablename = 'objects')
order by schemaname, tablename, policyname;
select id, name, public, file_size_limit, allowed_mime_types
from storage.buckets where id = 'bravo-logs';

