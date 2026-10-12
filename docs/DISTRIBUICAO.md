# Distribuição · Bravo Viewer

Repositório principal: [Reikjv/Bravo-Viewer](https://github.com/Reikjv/Bravo-Viewer). Código, testes, catálogo e documentação de uso estão nesse repositório. A pasta local usa esse endereço como `origin`; o histórico inicial ficou no remoto `legacy` e na branch `legacy-main`. Os documentos anteriores da inspeção também foram preservados em `data/legacy_documents/`, fora da branch publicada. Capturas e bancos locais permanecem em `data/`, fora do Git.

## Beta 0.4.1-beta.1

Desktop em janela nativa (WebView2) e instalador Inno Setup (`-Setup.exe`, com instalação silenciosa) além do pacote portátil. O CI gera o instalador, instala em modo silencioso e inicia o executável instalado no Windows. Corrige a tela que abria rolada até a âncora da aba. [Release](https://github.com/Reikjv/Bravo-Viewer/releases/tag/v0.4.1-beta.1).

## Beta 0.4.0-beta.1

Primeira versão do Bravo Viewer Desktop para Windows 11 (`Bravo-Viewer-Desktop-v0.4.0-beta.1-windows.zip` na [release](https://github.com/Reikjv/Bravo-Viewer/releases/tag/v0.4.0-beta.1)), gerado e iniciado em runner Windows no CI. No ROV: código interno do equipamento, API de sincronização incremental e ZIPs nomeados por equipamento/operação. Em ensaio local com 1.054.950 registros, a primeira sincronização levou 70 s no PC e as seguintes, sem novidades, 0,02 s; o executável empacotado sincronizou e decodificou os mesmos registros.

## Beta 0.3.2-beta.1

Buscas automáticas em fila própria; logs em gravação não são reimportados a cada busca; limite fixo de 20 operações; exportação leve por padrão. Motivo: no Navigator, logs de serviço que crescem continuamente eram reimportados a cada 5 minutos e uma exportação completa de 907 tipos/origens bloqueava a fila. [Release](https://github.com/Reikjv/Bravo-Viewer/releases/tag/v0.3.2-beta.1); download anônimo confirmado.

## Beta 0.3.1-beta.1

Encerrar operação busca os logs do BlueOS antes de congelar o resumo. O catálogo passa a usar `catalog/card.png` (imagem no formato do card) e `catalog/reikjv.png` (logo do desenvolvedor), com o nome do projeto na linha inferior. [Release](https://github.com/Reikjv/Bravo-Viewer/releases/tag/v0.3.1-beta.1) gerada pelo [workflow](https://github.com/Reikjv/Bravo-Viewer/actions/runs/38171406179); download anônimo confirmado.

## Beta 0.3.0-beta.1

Operações com encerramento, ZIP leve com progresso, limite de operações guardadas (padrão 10) e profundidade máxima/média condicionada ao sensor externo. Gerada pelo [workflow](https://github.com/Reikjv/Bravo-Viewer/actions/runs/38170213192) da tag `v0.3.0-beta.1`, com testes, controles DOM/API, imagens ARM64/ARMv7 e [release](https://github.com/Reikjv/Bravo-Viewer/releases/tag/v0.3.0-beta.1) concluídos; download anônimo da imagem confirmado. A atualização migra o histórico existente para a Operação 1. Em cópia local de 1.054.950 registros, encerrar a operação com ZIP leve levou 2,5 s no computador de desenvolvimento; no Navigator, espere tempos maiores. A fonte do catálogo no BlueOS usa `?v=<versão>` para forçar nova leitura.

## Beta 0.2.1-beta.1

Renovação visual da interface e do relatório exportado, sem mudança de dados ou API. Gerada pelo [workflow do Bravo-Viewer](https://github.com/Reikjv/Bravo-Viewer/actions/runs/38103513422) a partir da tag `v0.2.1-beta.1`: testes, controles DOM/API, inicialização das imagens ARM64/ARMv7 sob QEMU e [release](https://github.com/Reikjv/Bravo-Viewer/releases/tag/v0.2.1-beta.1) concluídos. O catálogo lista 0.2.1-beta.1 e 0.2.0-beta.1. Em 10/10/2026, após o pacote GHCR ficar público e receber acesso de escrita deste repositório, o download anônimo dos índices das duas versões foi confirmado.

## Beta 0.2.0-beta.1

Os [pacotes ARM64/ARMv7](https://github.com/Reikjv/Bravo-Viewer/releases/tag/v0.2.0-beta.1) estão públicos e tiveram ZIP, arquitetura, versão, commit e hashes internos/externos conferidos. Downloads públicos foram verificados sem autenticação.

As imagens dessa primeira publicação foram geradas pelo [workflow inicial](https://github.com/Reikjv/Reikjv-ROV-History/actions/runs/38101805215), do commit `44de40f`: 22 testes, controles DOM/API e inicialização das duas imagens sob QEMU concluíram com sucesso. O código da aplicação foi transferido para o repositório principal, onde os [mesmos testes passaram](https://github.com/Reikjv/Bravo-Viewer/actions/runs/38102981634). A tag da primeira release pública registra a distribuição inicial; o código transferido está na branch main. Tags futuras serão produzidas a partir desse repositório.

Catálogo externo para o BlueOS:

```text
https://raw.githubusercontent.com/Reikjv/Bravo-Viewer/main/manifest.json
```

Imagem: `ghcr.io/reikjv/bravo-viewer:0.2.0-beta.1`. O catálogo contém os digests e tamanhos reais das duas imagens, não valores estimados.

## Visibilidade do pacote GHCR

Repositório público e pacote GHCR são configurações separadas. A página canônica do pacote, retornada pela API, é:

[bravo-viewer no GitHub Packages](https://github.com/users/Reikjv/packages/container/package/bravo-viewer)

O pacote foi liberado para acesso público. A configuração de visibilidade fica em **Package settings → Change visibility**. Para próximas publicações pelas Actions do Bravo-Viewer, o repositório também precisa de acesso de escrita ao pacote em **Manage Actions access**.

Em 10/10/2026, o acesso anônimo foi validado sem credenciais: token, índice da versão `0.2.0-beta.1`, manifestos ARM64/ARMv7 e acesso às camadas retornaram HTTP 200. Os digests das duas imagens correspondem aos publicados no catálogo. Os instaladores offline públicos continuam disponíveis como alternativa.

Referências: [visibilidade e permissões de pacotes](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility) e [extensões BlueOS](https://blueos.cloud/docs/stable/development/extensions/).

## Validação no equipamento

Em 10/10/2026, o BlueOS em 192.168.2.2 voltou a responder. A API confirmou que a fonte **Reikjv Bravo-Viewer** já está cadastrada e habilitada com o endereço acima, e que a versão `0.2.0-beta.1` aparece no catálogo. A imagem ARMv7 foi reconhecida como compatível com esse equipamento; a ARM64 não é compatível com a instalação atual.

A consulta às extensões instaladas ainda não encontrou o Bravo Viewer. A imagem está acessível sem autenticação para instalação pelo catálogo de Extensões. Após a instalação, siga o [guia de bancada](GUIA_TESTES.md) para validar a execução no Navigator. Resultados de carga locais estão em [ESTABILIDADE.md](ESTABILIDADE.md); não substituem um ensaio prolongado no ROV.
