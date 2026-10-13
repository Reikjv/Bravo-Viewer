# Supabase · contrato de integração do Bravo Viewer

Estado em 13/10/2026: **em uso no projeto `vkilosmlgnvtlseuivoj`**. Seis tabelas com RLS, bucket privado `bravo-logs` e as contas de operação e gestão com papéis definidos. O Desktop 0.6+ enviou ao serviço real 183 sessões e 179 logs (199 MB), e a conta de gestão apagou esses dados pelas APIs de Storage e REST, conferindo a remoção. A partir da 0.8 o Desktop também recebe da nuvem o que outros PCs enviaram (validado com servidor simulado; recebimento entre dois PCs reais ainda pendente).

## Conexão administrativa

A tentativa de conectar a integração apresentou `Could not connect to local Supabase project. Make sure you've run 'supabase start'!`. Esse fluxo apontava para um projeto local. Foi cadastrada uma conexão MCP oficial na nuvem, em `~/.codex/config.toml`, com nome `bravo_supabase` e URL `https://mcp.supabase.com/mcp?project_ref=vkilosmlgnvtlseuivoj`. O login pelo `codex mcp add` retornou `Successfully logged in`; `codex mcp list` confirmou a conexão habilitada com OAuth. Os demais servidores configurados foram preservados.

Após recarregar o Codex, as ferramentas foram carregadas e a URL real do projeto, as tabelas, as migrações e as políticas puderam ser consultadas. O projeto estava vazio antes da aplicação. A vinculação ao GitHub é uma configuração separada e pode executar migrações conforme o fluxo de implantação configurado. Os nomes dos arquivos locais foram alinhados às versões já registradas pelo Supabase, para futuras implantações reconhecerem o que já foi aplicado.

Referências: [MCP no Codex](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) e [servidor oficial Supabase](https://supabase.com/docs/guides/ai-tools/mcp).

Projeto: https://supabase.com/dashboard/project/vkilosmlgnvtlseuivoj. URL da API confirmada: `https://vkilosmlgnvtlseuivoj.supabase.co`. A URL e a chave publishable ativa estão em `data/supabase/client.json`, fora do Git, para a outra sessão configurar o Desktop. Esse arquivo não contém chave administrativa, senha ou sessão de usuário.

Esta preparação é separada do código que a outra sessão está alterando. O rascunho `schema.sql` foi preservado. **Não execute os dois esquemas**: o rascunho anterior usa outras tabelas e políticas mais amplas para o mesmo bucket.

## Arquivos e aplicação

- `supabase/preflight.sql`: consulta tabelas existentes, políticas e bucket antes de qualquer mudança.
- `supabase/migrations/20261012221134_bravo_viewer.sql`: tabelas `bravo_*`, funções, índices, permissões e bucket privado. A criação de bucket por SQL é suportada pelo Supabase; upload/exclusão de objetos continua sendo feito pela API de Storage.
- `supabase/migrations/20261012221325_bravo_auth_enrolment.sql`: lista administrativa de e-mails autorizados e atribuição automática após o cadastro confirmado em Auth.
- `supabase/migrations/20261012221619_bravo_access_policies.sql`: separa INSERT/UPDATE/DELETE nas atribuições, evitando duas políticas permissivas para SELECT.
- `supabase/storage_bucket.json`: configuração equivalente para criar o bucket via API/painel em outro ambiente. O bucket deste projeto já foi criado com visibilidade privada e sem limite próprio; o limite efetivo é o do projeto.
- `supabase/setup_members.sql`: atribui os papéis às contas já existentes em Auth; exige substituir o e-mail do operador. Gestão é opcional na primeira aplicação e pode ser adicionada depois com conta distinta. Não contém senha. Conforme a escolha do usuário, o operador recebe `fleet_access=true`.

As três migrações já estão registradas no projeto. Não executar novamente arquivos antigos pelo SQL Editor; futuras alterações devem ser novas migrações. E-mails/perfis são dados administrativos e foram gravados separadamente no banco, sem embuti-los em arquivos públicos de migração.

Antes de aplicar, conferir o projeto e os resultados do preflight. Se já existirem o rascunho anterior ou políticas amplas em Storage, revisar e migrar essas políticas primeiro: políticas permissivas se combinam por OR. Não remover políticas de outros aplicativos/buckets sem examinar suas condições. Tabelas `bravo_*` evitam colisão com tabelas genéricas de um hub da empresa.

Criar os usuários com o fluxo de Supabase Auth escolhido pela empresa. O Desktop deve usar a URL do projeto e a chave **publishable**, fazendo login como usuário. Chaves secret/service_role e tokens de administração ficam fora do executável. Uma senha de operador embutida em um build público poderia ser extraída; o login deve ocorrer no PC, com a sessão persistida de forma protegida pelo Windows. Não é necessário usar GitHub Secrets para empacotar senhas.

## Modelo

Um projeto representa a frota desta empresa. Não há suporte a empresas independentes compartilhando as mesmas permissões nesta versão.

| Objeto | Chave e função |
| --- | --- |
| `bravo_members` | `user_id` de Auth; papel `operador`/`gestao`, habilitação e acesso global |
| `bravo_device_access` | `(device_id, user_id)`; atribuições gerenciadas pela gestão |
| `bravo_devices` | `id`: os 32 caracteres hexadecimais de `device_id` do ROV; `equipment`: nome exibido |
| `bravo_operations` | `(device_id, op_id)`; metadados e resumo da operação |
| `bravo_sessions` | `(device_id, source_key)`; resumo, revisão, análise compacta e referência ao original |
| `bravo_pending_members` | e-mail; autorização administrativa para atribuir o papel após confirmação em Auth, sem acesso pela API de usuários |
| Storage `bravo-logs` | arquivo privado `<device_id>/<sha256>.<ext>` |

As permissões são consultadas em `bravo_members`, evitando depender de um papel antigo em um JWT ainda válido. Um usuário de Auth sem cadastro ativo não recebe acesso. O usuário não pode alterar seu próprio papel. Gestão consulta e administra toda a frota; operador também consulta e envia dados de toda a frota, mas não pode excluir o histórico nem gerenciar permissões. Não é necessário cadastrar o mesmo ROV novamente para cada PC.

O operador tem acesso de leitura aos originais dos ROVs permitidos, além de upload. Leitura de um arquivo permite baixá-lo; não existe uma promessa de "ver o arquivo, mas impedir que seja baixado". Só gestão pode excluir. Os originais são imutáveis: não há política de UPDATE em Storage. Usar `upsert=false` para os arquivos; um conteúdo alterado terá outro hash e outro caminho.

O padrão de `bravo_members.fleet_access` é `true`. O modo restrito a ROVs atribuídos continua disponível para uma mudança futura: configurar `fleet_access=false` e cadastrar atribuições em `bravo_device_access`. Para revogar acesso, definir `enabled=false`. Atribuições não substituem esse bloqueio.

No painel **Authentication → Users**, criar as duas contas com os e-mails escolhidos e definir as senhas diretamente ali. O papel só é atribuído após o e-mail ser confirmado (ou o administrador confirmar o usuário no cadastro). A lista de autorizações está em `bravo_pending_members`; seu conteúdo não é acessível por usuários/visitantes. A função de cadastro não altera o papel nem desfaz a revogação de um membro já existente.

## Cadastro e sincronização pelo Desktop

Todas as requisições usam `apikey: <publishable>` e `Authorization: Bearer <access_token do usuário>`, por HTTPS. O prefixo da Data API é `/rest/v1/`.

1. Fazer login e consultar `bravo_members` para obter o papel atual. Manter a fila local quando não há internet/login; nenhum dado sai do PC até o usuário conectar a conta e habilitar o envio.
2. Cadastrar/atualizar o equipamento por `POST rpc/bravo_register_device`, com `{ "target_device": "<device_id>", "equipment_name": "Bravo 10" }`. Essa RPC também atribui o novo ROV ao operador e recusa cadastro de um ROV existente sem acesso. Para operador, não usar INSERT/UPSERT direto em `bravo_devices`.
3. Enviar as operações por UPSERT em `bravo_operations?on_conflict=device_id,op_id`, com `Prefer: resolution=merge-duplicates`. Converter `started_at`/`ended_at` de segundos Unix para data ISO 8601 UTC; mapear `operations.id` local para `op_id`. O banco impede que uma cópia antiga reabra uma operação encerrada.
4. Ler `bravo_sessions` e comparar `source_revision` e `sha256` antes de enviar. `source_revision` é `remote_revision` da sessão espelhada no PC, ou `imported_at` para uma sessão originada localmente. Não usar a hora atual do upload como revisão da fonte. Uma revisão inferior à existente é ignorada pelo banco.
5. Enviar os logs originais verificados pelo PC ao bucket privado. O caminho contém o `device_id` e o SHA-256 hexadecimal minúsculo calculado sobre os bytes do arquivo. As extensões aceitas são `.tlog`, `.bin`, `.log` e `.gz`. Para arquivos grandes, usar upload retomável/TUS conforme o limite do projeto. Um conflito de objeto existente exige conferir tamanho/hash antes de considerar o upload concluído; não sobrescrever o objeto.
6. Fazer UPSERT em `bravo_sessions?on_conflict=device_id,source_key`. Enviar `operation_id` da operação correspondente, `summary` do arquivo e `analysis` com `{streams, events, series}` retornado por `store.file_meta(file_id)`. `records` é o contador de `summary.records`, não o número de linhas do SQLite compacto. A operação precisa existir no mesmo ROV. `storage_path` só é preenchido após o upload e sua conferência. O banco valida o formato/ROV/hash do caminho, mas isso **não prova o conteúdo** do objeto: a conferência dos bytes cabe ao cliente/backend.
7. Marcar como enviado na fila do PC somente após concluir metadados e original, se houver. A sessão `live` envia resumo, ocorrências e séries compactas, com `sha256` e `storage_path` nulos. Não inventar um `.tlog` completo para leituras amostradas.

Na nuvem fica uma linha por sessão e por operação, não milhões de linhas MAVLink. Os arquivos originais permitem uma análise posterior completa; as séries compactas permitem mostrar gráficos sem reprocessar cada log. Campos `created_at/by` e `updated_at/by` são preenchidos pelo banco; não enviar os campos de auditoria do PC.

Exemplo de sessão (valores ilustrativos):

```json
{
  "device_id": "0123456789abcdef0123456789abcdef",
  "source_key": "blueos:192.168.2.2:/logs/exemplo.tlog",
  "operation_id": 1,
  "name": "exemplo.tlog",
  "kind": "tlog",
  "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "size": 1000,
  "records": 100,
  "source_revision": 1791838800,
  "summary": {"records": 100},
  "analysis": {"streams": [], "events": [], "series": []},
  "note": "",
  "storage_path": "0123456789abcdef0123456789abcdef/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.tlog"
}
```

Erros 401/403 mantêm a fila pendente e exigem corrigir sessão/acesso; não repetir indefinidamente como falha de internet. Em 429/5xx ou queda de rede, usar espera crescente e retomar. Nunca remover a cópia do PC só porque houve um upload. Uma cópia confirmada pelo PC no ROV e uma cópia confirmada na nuvem são estados diferentes.

Para excluir originais, usar a API de Storage. Exclusão por SQL de metadados de Storage não remove os bytes. Excluir uma operação/equipamento apaga as linhas relacionadas por FK, mas **não apaga automaticamente os objetos de Storage**: implementar a limpeza pela API, verificando arquivos compartilhados entre sessões e permitindo retomada de falhas.

## Verificação

Com Node disponível:

```powershell
npm install --prefix .tooling/supabase-validation --no-save --package-lock=false @electric-sql/pglite@0.3.14
node tools/test_supabase_schema.cjs
```

As 33 verificações locais executam as migrações em PostgreSQL/PGlite e simulam `auth.uid()` e metadados de Storage. Cobrem acesso anônimo, usuário não cadastrado, papéis, atribuições, dois ROVs com a mesma operação, reenvio, revisão atrasada, integridade, auditoria, proteção dos arquivos, revogação, reaplicação sem perda, configuração das contas em etapas e atribuição automática de papéis após confirmação. No projeto real, foram conferidos as seis tabelas com RLS, o bucket privado e as versões das migrações. Chamadas HTTP sem login às tabelas foram bloqueadas com 401; o registro de dispositivo também não ficou acessível a visitantes.

Auth com senha, upload TUS, download assinado, retomada HTTP e a fila do Desktop ainda dependem das contas e da integração do aplicativo. Nenhum log de teste foi enviado ao Supabase.

A revisão de performance apontou somente índices ainda não usados, compatível com o banco vazio; a duplicidade de políticas SELECT foi corrigida. A revisão de segurança informa que `bravo_pending_members` tem RLS sem políticas, intencional porque a tabela é exclusivamente administrativa, e alerta sobre quatro funções SECURITY DEFINER executáveis por usuários autenticados. Essas funções implementam consulta do próprio papel/escopo e cadastro autorizado do ROV, com `search_path` fixo, identidade de `auth.uid()` e EXECUTE revogado para visitantes. A RPC de cadastro valida papel ativo e acesso ao equipamento antes e depois do INSERT; os testes cobrem tentativa de registrar ROV alheio, elevação de papel e bloqueio após revogação. Não declarar "sem alertas"; preservar essa revisão ao alterar as funções. Referências dos avisos: [funções SECURITY DEFINER](https://supabase.com/docs/guides/database/database-linter?lint=0029_authenticated_security_definer_function_executable), [RLS administrativo](https://supabase.com/docs/guides/database/database-linter?lint=0008_rls_enabled_no_policy) e [índices ainda não usados](https://supabase.com/docs/guides/database/database-linter?lint=0005_unused_index).

Referências: [Data API](https://supabase.com/docs/guides/api), [chaves para aplicativos](https://supabase.com/docs/guides/getting-started/api-keys), [RLS](https://supabase.com/docs/guides/database/postgres/row-level-security), [acesso a Storage](https://supabase.com/docs/guides/storage/security/access-control), [upload retomável](https://supabase.com/docs/guides/storage/uploads/resumable-uploads) e [metadados de Storage](https://supabase.com/docs/guides/storage/schema/design).
