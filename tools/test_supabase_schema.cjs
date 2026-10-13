// PostgreSQL/PGlite: autenticação e metadados Storage simulados, sem projeto remoto.
// npm install --prefix .tooling/supabase-validation --no-save --package-lock=false @electric-sql/pglite@0.3.14
// node tools/test_supabase_schema.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { PGlite } = require('../.tooling/supabase-validation/node_modules/@electric-sql/pglite');
const migration = fs.readFileSync(path.join(__dirname, '../supabase/migrations/20261012221134_bravo_viewer.sql'), 'utf8');
const memberSetup = fs.readFileSync(path.join(__dirname, '../supabase/setup_members.sql'), 'utf8');
const enrolment = fs.readFileSync(path.join(__dirname, '../supabase/migrations/20261012221325_bravo_auth_enrolment.sql'), 'utf8');
const accessPolicies = fs.readFileSync(path.join(__dirname, '../supabase/migrations/20261012221619_bravo_access_policies.sql'), 'utf8');
const users = {
  operator: '00000000-0000-0000-0000-000000000001',
  manager: '00000000-0000-0000-0000-000000000002',
  other: '00000000-0000-0000-0000-000000000003',
  outsider: '00000000-0000-0000-0000-000000000004',
};
const device1 = 'a'.repeat(32), device2 = 'b'.repeat(32), sha = 'c'.repeat(64);
let passed = 0;
const db = new PGlite();
async function check(label, fn) {
  await fn();
  passed++;
  console.log('OK:', label);
}
async function as(role, user, fn, extra = {}) {
  await db.exec(`set role ${role}`);
  await db.query("select set_config('request.jwt.claims', $1, false)", [JSON.stringify({sub: user, ...extra})]);
  try { return await fn(); }
  finally { await db.exec('reset role'); await db.query("select set_config('request.jwt.claims', '{}', false)"); }
}
const auth = (user, fn, extra) => as('authenticated', user, fn, extra);
const scalar = async (sql, params = []) => Object.values((await db.query(sql, params)).rows[0])[0];
const denied = async (fn, code = '42501') => assert.rejects(fn, error => error.code === code);

(async () => {
  try {
    await db.exec(`
      create role anon; create role authenticated; create role service_role bypassrls;
      create schema auth; create schema storage;
      create table storage.buckets(id text primary key, name text not null, public boolean not null default false);
      create table auth.users(id uuid primary key, email text unique, email_confirmed_at timestamptz);
      create function auth.uid() returns uuid language sql stable as $$
        select nullif(current_setting('request.jwt.claims', true)::jsonb ->> 'sub', '')::uuid
      $$;
      grant usage on schema public, auth, storage to anon, authenticated, service_role;
      create table storage.objects(id uuid primary key default gen_random_uuid(), bucket_id text not null, name text not null, unique(bucket_id, name));
      alter table storage.objects enable row level security;
      grant select, insert, update, delete on storage.objects to authenticated;
    `);
    await check('migração executa em PostgreSQL', () => db.exec(migration));
    await db.exec(accessPolicies);
    for (const [name, id] of Object.entries(users)) await db.query('insert into auth.users values ($1,$2)', [id, name+'@example.invalid']);
    // Exercitar também o modo restrito opcional; produção usará toda a frota.
    await db.query(`insert into public.bravo_members(user_id, role, fleet_access) values ($1, 'operador', false), ($2, 'gestao', true), ($3, 'operador', false)`, [users.operator, users.manager, users.other]);
    await check('anônimo não tem acesso às tabelas', () => as('anon', null, () => denied(() => db.query('select * from public.bravo_devices'))));
    await check('usuário sem cadastro não pode criar equipamento', () => auth(users.outsider, () => denied(() => db.query('insert into public.bravo_devices(id,equipment) values ($1,$2)', [device1, 'Bravo 1']))));
    await check('operador registra equipamento com retorno e recebe atribuição', async () => {
      const result = await auth(users.operator, () => db.query('select (public.bravo_register_device($1,$2)).id', [device1, 'Bravo 1']));
      assert.equal(result.rows[0].id, device1);
      assert.equal(await auth(users.operator, () => scalar('select count(*) from public.bravo_devices')), 1);
      assert.equal(await scalar('select count(*) from public.bravo_device_access'), 1);
    });
    await check('repetir cadastro do equipamento não duplica', () => auth(users.operator, async () => {
      const result = await db.query('select (public.bravo_register_device($1,$2)).id', [device1, 'Bravo 1']);
      assert.equal(result.rows.length, 1);
    }));
    await auth(users.manager, () => db.query('insert into public.bravo_devices(id,equipment) values ($1,$2)', [device2, 'Bravo 2']));
    await db.query('insert into public.bravo_device_access(device_id,user_id) values ($1,$2)', [device2, users.other]);
    await check('operadores só veem seus equipamentos; gestão vê todos', async () => {
      assert.equal(await auth(users.operator, () => scalar('select count(*) from public.bravo_devices')), 1);
      assert.equal(await auth(users.other, () => scalar('select count(*) from public.bravo_devices')), 1);
      assert.equal(await auth(users.manager, () => scalar('select count(*) from public.bravo_devices')), 2);
    });
    await check('usuário não altera seu papel ou atribui outro equipamento', () => auth(users.operator, async () => {
      await denied(() => db.query("update public.bravo_members set role='gestao'"));
      await denied(() => db.query('insert into public.bravo_device_access(device_id,user_id) values ($1,$2)', [device2, users.operator]));
      await denied(() => db.query("insert into public.bravo_operations(device_id,op_id,name,status) values ($1,1,'Alheia','open')", [device2]));
      await denied(() => db.query('select public.bravo_register_device($1,$2)', [device2, 'Renomeado sem acesso']));
    }));
    await check('user_metadata ou app_metadata forjado não concede gestão', async () => {
      assert.equal(await auth(users.operator, () => scalar('select public.bravo_current_role()'), {user_metadata: {role:'gestao'}, app_metadata:{role:'gestao'}}), 'operador');
    });
    await check('mesmo número de operação em ROVs distintos não colide', async () => {
      for (const device of [device1, device2]) await auth(users.manager, () => db.query("insert into public.bravo_operations(device_id,op_id,name,status,started_at) values ($1,1,'Inspeção','open',now())", [device]));
      assert.equal(await scalar('select count(*) from public.bravo_operations'), 2);
    });
    const sessionSql = `insert into public.bravo_sessions(device_id,source_key,operation_id,name,kind,sha256,size,records,source_revision,summary)
      values ($1,'source:1',1,'log.tlog','tlog',$2,10,25,$3,$4::jsonb)
      on conflict(device_id,source_key) do update set source_revision=excluded.source_revision, summary=excluded.summary returning source_key`;
    await check('reenviar sessão não duplica registros', () => auth(users.operator, async () => {
      for (let i=0;i<2;i++) await db.query(sessionSql,[device1,sha,2,'{"armed_seconds":30}']);
      assert.equal(await scalar('select count(*) from public.bravo_sessions'), 1);
    }));
    await check('segundo PC atrasado não sobrescreve revisão nova', () => auth(users.operator, async () => {
      const result = await db.query(sessionSql,[device1,sha,1,'{"armed_seconds":1}']);
      assert.equal(result.rows.length, 0);
      assert.deepEqual((await db.query('select summary from public.bravo_sessions')).rows[0].summary, {armed_seconds:30});
    }));
    await check('sessão precisa referenciar operação existente do mesmo ROV', () => auth(users.operator, () => denied(() => db.query("insert into public.bravo_sessions(device_id,source_key,operation_id,name,kind) values ($1,'bad',2,'bad','live')", [device1]), '23503')));
    await check('gestão recebe atribuição e operador passa a ver novo ROV', async () => {
      await auth(users.manager, () => db.query('insert into public.bravo_device_access(device_id,user_id) values ($1,$2)', [device2,users.operator]));
      assert.equal(await auth(users.operator, () => scalar('select count(*) from public.bravo_devices')), 2);
    });
    await check('operador não exclui histórico; gestão pode excluir', async () => {
      assert.equal((await auth(users.operator, () => db.query('delete from public.bravo_operations returning op_id'))).rows.length, 0);
      assert.equal((await auth(users.manager, () => db.query('delete from public.bravo_operations where device_id=$1 returning op_id', [device2]))).rows.length, 1);
    });
    await check('operação encerrada não é reaberta por cópia atrasada', () => auth(users.operator, async () => {
      await db.query("update public.bravo_operations set status='closed',ended_at=now() where device_id=$1", [device1]);
      await db.query("update public.bravo_operations set status='open',ended_at=null where device_id=$1", [device1]);
      assert.equal(await scalar('select status from public.bravo_operations'), 'closed');
    }));
    await check('auditoria usa o usuário real e preserva o criador', () => auth(users.operator, async () => {
      await db.query('update public.bravo_sessions set created_by=$1,updated_by=$1', [users.manager]);
      const row = (await db.query('select created_by,updated_by from public.bravo_sessions')).rows[0];
      assert.deepEqual(row,{created_by:users.operator,updated_by:users.operator});
      await denied(() => db.query("update public.bravo_sessions set source_key='changed'"),'23514');
      await denied(() => db.query('update public.bravo_operations set op_id=2'),'23514');
      await denied(() => db.query('update public.bravo_devices set id=$1 where id=$2',['e'.repeat(32),device1]),'23514');
    }));
    const objectName=device1+'/'+sha+'.tlog';
    await check('operador envia log no caminho do seu ROV', () => auth(users.operator, () => db.query("insert into storage.objects(bucket_id,name) values ('bravo-logs',$1)",[objectName])));
    await check('outro operador não lê nem envia logs desse ROV', () => auth(users.other, async () => {
      assert.equal(await scalar('select count(*) from storage.objects'),0);
      await denied(() => db.query("insert into storage.objects(bucket_id,name) values ('bravo-logs',$1)",[device1+'/'+ 'd'.repeat(64)+'.bin']));
    }));
    await check('caminhos malformados e outros buckets são negados', () => auth(users.operator, async () => {
      for(const name of ['../evil',device1+'/wrong.tlog',objectName+'/extra']) await denied(() => db.query("insert into storage.objects(bucket_id,name) values ('bravo-logs',$1)",[name]));
      await denied(() => db.query("insert into storage.objects(bucket_id,name) values ('other',$1)",[objectName]));
    }));
    await check('log existente não pode ser sobrescrito ou excluído pelo operador', () => auth(users.operator, async () => {
      assert.equal((await db.query('update storage.objects set name=$1 returning id',[device1+'/'+ 'd'.repeat(64)+'.tlog'])).rows.length,0);
      assert.equal((await db.query('delete from storage.objects returning id')).rows.length,0);
    }));
    await check('caminho de sessão exige ROV e SHA correspondentes', () => auth(users.operator, async () => {
      await denied(() => db.query('update public.bravo_sessions set storage_path=$1',[device2+'/'+sha+'.tlog']), '23514');
      await db.query('update public.bravo_sessions set storage_path=$1',[objectName]);
    }));
    await check('padrão escolhido concede ao operador acesso a toda frota', async () => {
      await db.query('update public.bravo_members set fleet_access=default where user_id=$1',[users.other]);
      assert.equal(await auth(users.other,()=>scalar('select count(*) from public.bravo_devices')),2);
      await db.query('update public.bravo_members set fleet_access=false where user_id=$1',[users.other]);
    });
    await check('revogação no banco bloqueia imediatamente dados e logs', async () => {
      await db.query('update public.bravo_members set enabled=false where user_id=$1',[users.operator]);
      await auth(users.operator,async()=>{
        assert.equal(await scalar('select count(*) from public.bravo_devices'),0);
        assert.equal(await scalar('select count(*) from public.bravo_sessions'),0);
        assert.equal(await scalar('select count(*) from storage.objects'),0);
      });
    });
    await check('reaplicar migração preserva dados e usuários', async () => {
      await db.exec(migration);
      await db.exec(accessPolicies);
      assert.equal(await scalar('select count(*) from public.bravo_members'),3);
      assert.equal(await scalar('select count(*) from public.bravo_sessions'),1);
    });
    await check('modelo de atribuição exige substituir o e-mail', async () => {
      await denied(() => db.exec(memberSetup), 'P0001');
      await db.exec('rollback');
    });
    await check('conta de operador pode ser configurada sem gestão definida', async () => {
      const configured = memberSetup.replace('TROQUE_OPERADOR@exemplo.invalid', 'operator@example.invalid');
      await db.exec(configured);
      await db.exec(configured);
      const row = (await db.query('select role,fleet_access,enabled from public.bravo_members where user_id=$1',[users.operator])).rows[0];
      assert.deepEqual(row,{role:'operador',fleet_access:true,enabled:true});
      assert.equal(await scalar('select count(*) from public.bravo_members'),3);
    });
    await check('gestão pode ser acrescentada depois com conta distinta', async () => {
      const configured = memberSetup.replace('TROQUE_OPERADOR@exemplo.invalid','operator@example.invalid')
        .replace('manager_email text := null;', "manager_email text := 'manager@example.invalid';");
      await db.exec(configured);
      assert.equal(await scalar('select role from public.bravo_members where user_id=$1',[users.manager]),'gestao');
    });
    await check('migração de cadastro automático executa e é reaplicável', async () => {
      await db.exec(enrolment); await db.exec(enrolment);
      await db.exec("insert into public.bravo_pending_members(email,role) values ('new-operator@example.invalid','operador'),('new-manager@example.invalid','gestao')");
    });
    await check('lista de autorizações não é exposta a usuários ou visitantes', async () => {
      await auth(users.operator,()=>denied(()=>db.query('select * from public.bravo_pending_members')));
      await as('anon',null,()=>denied(()=>db.query('select * from public.bravo_pending_members')));
    });
    const newOperator='00000000-0000-0000-0000-000000000005';
    const newManager='00000000-0000-0000-0000-000000000006';
    await check('e-mail autorizado sem confirmação ainda não recebe acesso', async () => {
      await db.query('insert into auth.users(id,email) values($1,$2)',[newOperator,'new-operator@example.invalid']);
      assert.equal(await scalar('select count(*) from public.bravo_members where user_id=$1',[newOperator]),0);
    });
    await check('confirmar e-mail autorizado cadastra operador com frota inteira', async () => {
      await db.query('update auth.users set email_confirmed_at=now() where id=$1',[newOperator]);
      assert.equal(await auth(newOperator,()=>scalar('select public.bravo_current_role()')),'operador');
      assert.equal(await auth(newOperator,()=>scalar('select count(*) from public.bravo_devices')),2);
    });
    await check('gestão confirmada recebe seu papel e e-mail alheio não recebe acesso', async () => {
      await db.query('insert into auth.users(id,email,email_confirmed_at) values($1,$2,now())',[newManager,'new-manager@example.invalid']);
      assert.equal(await auth(newManager,()=>scalar('select public.bravo_current_role()')),'gestao');
      await db.query('update auth.users set email_confirmed_at=now() where id=$1',[users.outsider]);
      assert.equal(await scalar('select count(*) from public.bravo_members where user_id=$1',[users.outsider]),0);
    });
    await check('atualização do cadastro Auth não desfaz uma revogação', async () => {
      await db.query('update public.bravo_members set enabled=false where user_id=$1',[newOperator]);
      await db.query('update auth.users set email_confirmed_at=now() where id=$1',[newOperator]);
      assert.equal(await auth(newOperator,()=>scalar('select count(*) from public.bravo_devices')),0);
    });
    console.log(`\n${passed} verificações passaram. Supabase Auth/Storage HTTP ainda precisam de validação no projeto conectado.`);
  } finally { await db.close(); }
})().catch(error=>{console.error(error.message, error.code || ''); process.exitCode=1;});
