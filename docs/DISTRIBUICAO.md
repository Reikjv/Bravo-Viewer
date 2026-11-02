# Distribuição · Bravo Viewer

Repositório principal: [Reikjv/Bravo-Viewer](https://github.com/Reikjv/Bravo-Viewer), com código, testes, catálogo BlueOS e documentação. O repositório anterior, `Reikjv/Reikjv-ROV-History`, está arquivado (somente leitura) com o histórico inicial. Capturas e bancos locais ficam em `data/`, fora do Git.

## Publicação de uma versão

1. Atualizar a versão em `rov_history/__init__.py`, `Dockerfile`, `install.sh`, `README.md` e `.github/workflows/verify-distribution.yml`.
2. Criar e enviar a tag `v<versão>`. O workflow testa (backend, interface, esquema do Supabase), gera e testa as imagens ARM64/ARMv7 e o instalador do Windows, publica a imagem no GHCR e cria a release.
3. No mesmo workflow, a versão entra no catálogo único [Reikjv/blueos-extensions](https://github.com/Reikjv/blueos-extensions) (segredo `CATALOG_TOKEN`), que mantém as 3 versões mais recentes; e as imagens de versões mais antigas são apagadas do GHCR.
4. O BlueOS relê o catálogo periodicamente; para ver a versão na hora, acrescentar `?v=<qualquer coisa>` à URL da fonte.

Builds completos (ARM e Windows) rodam apenas em tags; commits comuns rodam só os testes, e commits apenas de documentação não rodam nada. Repositório público não consome minutos do GitHub Actions.

O Desktop instalado encontra a versão nova sozinho (aviso **Atualizar agora**, com conferência do SHA-256).

## Versões publicadas

**0.8.0-beta.1 (atual).** Nuvem nos dois sentidos: cada PC envia o que sincronizou e recebe, a cada minuto, os ROVs, operações, resumos e gráficos enviados por outros PCs, sem reenvios. [Release](https://github.com/Reikjv/Bravo-Viewer/releases/tag/v0.8.0-beta.1).

**0.7.0-beta.1 (anterior).** Relatório visual da operação (indicadores, mergulhos, picos com horário, gráficos e erros/avisos agrupados); histórico só com logs de voo; Sessões por operação; Arquivos simplificado; Frota sem piscar. [Release](https://github.com/Reikjv/Bravo-Viewer/releases/tag/v0.7.0-beta.1).

### Histórico

| Versão | Principal mudança |
| --- | --- |
| 0.6 | Logs lidos direto da pasta do BlueOS, sem cópia; Desktop processa no PC, encontra ROVs na rede, tela Frota, envio ao Supabase e atualização automática |
| 0.5 | Armazenamento compacto (resumos e séries de 10 s em vez de cada mensagem): banco de 234 MB para 0,46 MB com os logs de bancada |
| 0.4 | Bravo Viewer Desktop para Windows, janela própria, instalador e logo BV |
| 0.3 | Operações com encerramento, ZIP e limite de operações guardadas; fila separada para buscas |
| 0.2 | Primeira distribuição pública, identidade BRS e visual renovado |

As releases anteriores à 0.7 foram removidas do GitHub e do catálogo para simplificar a escolha no BlueOS; o código de cada uma continua nas tags do Git.

## Pacote GHCR

Repositório público e pacote GHCR são configurações separadas. O [pacote bravo-viewer](https://github.com/users/Reikjv/packages/container/package/bravo-viewer) está público, e o repositório Bravo-Viewer tem acesso de escrita a ele em **Manage Actions access**. O download anônimo de cada versão publicada é conferido após a release.

Referências: [visibilidade e permissões de pacotes](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility) e [extensões BlueOS](https://blueos.cloud/docs/stable/development/extensions/).

## Equipamento de bancada

Navigator em 192.168.2.2, BlueOS 1.4.6, imagem ARMv7. Em 13/10/2026 os logs de voo antigos do BlueOS, o histórico do Bravo no ROV, o histórico local do Desktop e os dados do Supabase foram zerados a pedido do usuário, mantendo nome, configuração e código interno do equipamento. A partir daí o histórico reflete apenas o que é capturado. Veja o [guia de bancada](GUIA_TESTES.md).
