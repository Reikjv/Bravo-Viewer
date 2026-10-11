# Distribuição · Bravo Viewer

Repositório principal: [Reikjv/Bravo-Viewer](https://github.com/Reikjv/Bravo-Viewer). Código, testes, catálogo e documentação de uso estão nesse repositório. A pasta local usa esse endereço como `origin`; o histórico inicial ficou no remoto `legacy` e na branch `legacy-main`. Os documentos anteriores da inspeção também foram preservados em `data/legacy_documents/`, fora da branch publicada. Capturas e bancos locais permanecem em `data/`, fora do Git.

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
