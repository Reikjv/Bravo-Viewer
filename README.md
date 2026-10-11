# Bravo Viewer

<img src="logo.png" alt="BRS" width="160">

Extensão BlueOS para consultar o histórico do ROV: horas, operações, energia, ocorrências, gráficos e inventário de tipos/campos. **Beta 0.2.1-beta.1**, desenvolvida para testes com BlueOS 1.4.6, ArduSub 4.7.1 e Navigator.

## Instalação

No gerenciador de **Extensões do BlueOS**, abra as configurações de fontes/repositórios e adicione:

- Nome: **Bravo Viewer**
- URL:

```text
https://raw.githubusercontent.com/Reikjv/Bravo-Viewer/main/manifest.json
```

Ative a fonte, procure **Bravo Viewer** e selecione a versão **0.2.1-beta.1**. Habilite versões de desenvolvimento/beta se necessário. O BlueOS escolhe ARM64 ou ARMv7 e baixa `ghcr.io/reikjv/bravo-viewer:0.2.1-beta.1`. É preciso acesso à internet durante o download; o painel funciona localmente após a instalação.

O catálogo usa o suporte a fontes externas do BlueOS. A extensão não foi incluída no catálogo oficial. Consulte a [documentação do BlueOS](https://blueos.cloud/docs/stable/development/extensions/).

## Instaladores offline

Baixe nas [releases](https://github.com/Reikjv/Bravo-Viewer/releases/tag/v0.2.1-beta.1):

- [ARM64](https://github.com/Reikjv/Bravo-Viewer/releases/download/v0.2.1-beta.1/Bravo-Viewer-v0.2.1-beta.1-arm64.zip)
- [ARMv7](https://github.com/Reikjv/Bravo-Viewer/releases/download/v0.2.1-beta.1/Bravo-Viewer-v0.2.1-beta.1-armv7.zip)

Confira o checksum `.zip.sha256`. Extraia o pacote no BlueOS e execute `bash install.sh` na pasta extraída. O instalador confere arquitetura e hashes, carrega a imagem incluída e preserva o volume em atualizações. A arquitetura relevante é a do **servidor Docker**, consultável com `docker version --format '{{.Server.Arch}}'`.

Escolha somente um método de instalação. Gerenciador e script offline usam a mesma porta e o mesmo volume.

## Uso e exportação

Abra a extensão no BlueOS ou `http://IP_DO_BLUEOS:8090/`. Em Configuração, identifique o equipamento e confira a coleta. A primeira instalação inicia a coleta automática; o backend continua coletando quando a tela fecha. **Buscar no BlueOS** consulta os logs existentes, incluindo o inventário de arquivos vazios. Também é possível importar `.tlog`, `.bin`, `.log` e `.gz`.

**Exportar Viewer** gera um ZIP organizado de **todo o histórico** ou da **visão atual e filtros**. Inclui todas as linhas correspondentes, inclusive páginas fechadas, e observações salvas. Extraia e abra `relatorio.html`: o relatório funciona sem internet, com gráficos e links para CSV/JSON/JSONL. As pastas separam resumo, energia, ocorrências, inventário, dados por sessão/tipo e originais opcionais. O manifesto identifica seleção, versão e quantidades. Datas exportadas usam UTC.

A grade **Dados e possibilidades** informa o que está disponível, os tipos identificados e o que depende de sensores ou de painéis futuros: profundidade, atitude/IMU, posição, DVL, telemetria por ESC e outros. Campos já decodificados continuam acessíveis no inventário mesmo sem painel dedicado.

## Limites da beta

- Tempo armado inclui bancada. Lacunas de mais de cinco segundos não contam como operação contínua.
- Contadores podem ser reiniciados. Cada arquivo ou coleta diária UTC é uma sessão; arquivos sobrepostos não são somados.
- Ah/Wh são acumuladores registrados; não há estimativa para preencher dados ausentes. Sentinelas desconhecidas não são apresentadas como medições válidas.
- A API é amostrada e não captura todas as mensagens MAVLink transmitidas. O inventário de arquivos cobre as pastas consultadas de autopiloto/serviços, não todos os volumes privados de outras extensões.
- Histórico/originais persistem em `/usr/blueos/extensions/reikjv-rov-history`; acompanhe o espaço livre. Apenas os cinco ZIPs exportados mais recentes são mantidos.
- Recursos do contêiner: 256 MiB e 0,5 CPU. Consultas pesadas podem informar ocupação; tente novamente após alguns segundos.

Os testes locais incluíram 1.054.950 registros, oito clientes, exportação simultânea e coleta simulada por cinco minutos: banco e ZIP íntegros, sem falhas inesperadas e pico de memória de 64,5 MiB. Esse ensaio no computador não garante desempenho nem estabilidade prolongada no Navigator. A validação física deve conferir operação representativa, horas de coleta, reinício, rede e espaço disponível.

Este repositório contém o código da extensão, catálogo, testes e documentação de uso; as releases incluem os instaladores. Os logs de teste não estão incluídos. As dependências de terceiros conservam suas licenças na imagem.

## Desenvolvimento

Com Python 3.12 ou posterior:

```powershell
python -m pip install -r requirements.txt
python -m rov_history.app --host 127.0.0.1 --port 8090 --data-dir data/app --blueos-host 192.168.2.2
python -m unittest discover -v
node --check web/app.js
```

O workflow testa a interface com dados sintéticos. Em tags de versão ou execução manual, constrói as imagens ARM sob QEMU, publica no GHCR e gera os instaladores e o manifesto com digests reais. No GHCR, o pacote precisa de visibilidade Public e acesso de escrita para as Actions deste repositório. A primeira publicação é privada por padrão do GitHub; a visibilidade do repositório não modifica automaticamente a do pacote.

Veja [ESTABILIDADE.md](docs/ESTABILIDADE.md) e [GUIA_TESTES.md](docs/GUIA_TESTES.md) para evidências e validação no equipamento.

---

Bravo Viewer · BRS · desenvolvido por Reikjv.
