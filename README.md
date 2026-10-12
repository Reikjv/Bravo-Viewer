# Bravo Viewer

<img src="catalog/card.png" alt="Bravo Viewer · BRS" width="320">

Extensão BlueOS para consultar o histórico do ROV: horas, operações, energia, ocorrências, gráficos e inventário de tipos/campos. **Beta 0.7.0-beta.1**, desenvolvida para testes com BlueOS 1.4.6, ArduSub 4.7.1 e Navigator.

## Instalação

No gerenciador de **Extensões do BlueOS**, abra as configurações de fontes/repositórios e adicione:

- Nome: **Bravo Viewer**
- URL:

```text
https://raw.githubusercontent.com/Reikjv/Bravo-Viewer/main/manifest.json
```

Ative a fonte, procure **Bravo Viewer** e selecione a versão **0.7.0-beta.1**. Habilite versões de desenvolvimento/beta se necessário. O BlueOS escolhe ARM64 ou ARMv7 e baixa `ghcr.io/reikjv/bravo-viewer:0.7.0-beta.1`. É preciso acesso à internet durante o download; o painel funciona localmente após a instalação.

O catálogo usa o suporte a fontes externas do BlueOS. A extensão não foi incluída no catálogo oficial. Consulte a [documentação do BlueOS](https://blueos.cloud/docs/stable/development/extensions/).

## Instaladores offline

Baixe nas [releases](https://github.com/Reikjv/Bravo-Viewer/releases/tag/v0.7.0-beta.1):

- [ARM64](https://github.com/Reikjv/Bravo-Viewer/releases/download/v0.7.0-beta.1/Bravo-Viewer-v0.7.0-beta.1-arm64.zip)
- [ARMv7](https://github.com/Reikjv/Bravo-Viewer/releases/download/v0.7.0-beta.1/Bravo-Viewer-v0.7.0-beta.1-armv7.zip)

Confira o checksum `.zip.sha256`. Extraia o pacote no BlueOS e execute `bash install.sh` na pasta extraída. O instalador confere arquitetura e hashes, carrega a imagem incluída e preserva o volume em atualizações. A arquitetura relevante é a do **servidor Docker**, consultável com `docker version --format '{{.Server.Arch}}'`.

Escolha somente um método de instalação. Gerenciador e script offline usam a mesma porta e o mesmo volume.

## Uso e exportação

Abra a extensão no BlueOS ou `http://IP_DO_BLUEOS:8090/`. Em Configuração, identifique o equipamento e confira a coleta. A primeira instalação inicia a coleta automática; o backend continua coletando quando a tela fecha. **Buscar no BlueOS** consulta os logs existentes, incluindo o inventário de arquivos vazios. A busca roda em fila própria e não atrasa exportações. Arquivos ainda em gravação não são reimportados a cada busca: o log de voo da inicialização atual entra depois que o ROV reinicia (a coleta ao vivo cobre esse intervalo) e logs de serviço entram após 10 minutos sem alteração. O ZIP do **Exportar Viewer** é leve por padrão; tabelas completas CSV/JSONL são opcionais. Também é possível importar `.tlog`, `.bin`, `.log` e `.gz`.

### Operações

A aba **Operações** agrupa as sessões de cada saída a campo. Sempre há uma operação aberta: os logs importados e a coleta ao vivo entram nela. Informe nome, local, cliente/serviço e observações. Ligar e desligar o ROV não muda a operação: cada inicialização vira uma sessão dentro dela. Ao voltar para a base, use **Encerrar operação**: os logs mais recentes do BlueOS são buscados para incluir o último mergulho, o resumo é congelado, o ZIP da operação é gerado com barra de progresso e a próxima operação começa automaticamente. O histórico existente na atualização forma a Operação 1.

O ZIP da operação é leve por padrão: relatório, resumos JSON e os logs originais `.tlog`/`.bin`, apenas copiados. Tabelas CSV/JSONL completas são opcionais porque exigem reler todos os registros no ROV. O ROV guarda as últimas **20** operações encerradas (limite fixo); ao encerrar a 21ª, a mais antiga é removida com registros e originais. A lista indica quais ZIPs ainda não foram baixados. Logs de uma operação encerrada não são alterados depois.

**Profundidade máxima e média** aparecem por operação quando o sensor externo de profundidade é registrado (`SCALED_PRESSURE2`). O valor é a profundidade informada pelo ArduSub; a média considera somente o tempo abaixo de 0,5 m. Sem sensor externo, a tela mostra "Sem sensor", pois o barômetro interno não mede profundidade.

Tempos e energia da operação usam a fonte com maior cobertura (coleta ao vivo ou .tlog), sem somar fontes que registram a mesma telemetria.

### Exportar Viewer

**Exportar Viewer** gera um ZIP organizado de **todo o histórico** ou da **visão atual e filtros**. Inclui todas as linhas correspondentes, inclusive páginas fechadas, e observações salvas. Extraia e abra `relatorio.html`: o relatório funciona sem internet, com gráficos e links para CSV/JSON/JSONL. As pastas separam resumo, energia, ocorrências, inventário, dados por sessão/tipo e originais opcionais. O manifesto identifica seleção, versão e quantidades. Datas exportadas usam UTC.

A grade **Dados e possibilidades** informa o que está disponível, os tipos identificados e o que depende de sensores ou de painéis futuros: profundidade, atitude/IMU, posição, DVL, telemetria por ESC e outros. Campos já decodificados continuam acessíveis no inventário mesmo sem painel dedicado.

## Armazenamento compacto

Um ROV gera cerca de 20 MB de `.tlog` e 470 mil mensagens por hora ligado. Guardar cada mensagem no banco ocupava cerca de 5 vezes o tamanho do log (≈31 GB em 30 dias de 10 h). A partir da 0.5, cada log é lido uma vez e o banco guarda apenas:

- resumo da sessão (horas, tempo armado, profundidade, energia), ocorrências e inventário de tipos/campos;
- séries de 10 s (mínimo, máximo e média) de tensão, corrente, consumo, energia, estado armado e profundidade.

O `.tlog` original continua sendo a fonte completa: o inventário de dados e as tabelas CSV/JSONL releem o log sob demanda, decodificando apenas o tipo pedido. Nos logs de bancada, o banco passou de 234 MB para 0,46 MB e a leitura ficou cerca de 7 vezes mais rápida. A leitura do log acontece fora da trava de escrita, sem pausar a coleta ao vivo, que também passa a guardar só séries, ocorrências e resumo. Na atualização, o histórico anterior é convertido automaticamente uma única vez.

Logs de operações **encerradas** que o Bravo Viewer Desktop já copiou e conferiu (SHA-256) são liberados no ROV; resumos e gráficos permanecem. A Configuração mostra o espaço livre, e um aviso aparece abaixo de 15%.

## Relatório da operação

O ZIP de cada operação traz um `relatorio.html` visual, sem listas de mensagens: duração, tempo armado total e médio por mergulho, maior mergulho, profundidade máxima e média, consumo, **picos com horário** (corrente máxima, tensão mínima e máxima), a tabela de mergulhos, gráficos de tensão, corrente e profundidade (com as pausas sem dados marcadas) e os erros e avisos agrupados (ex.: `MYGCS: heartbeat lost · 395×`). Acompanham `mergulhos.csv`, `ocorrencias.csv`, `graficos/*.csv` e os logs de voo originais em `logs/`.

O histórico considera só os logs de voo (`.tlog`/`.bin`). Os logs internos dos serviços do BlueOS (rede, câmera, ping…) ficam no BlueOS e não entram em listas, relatórios nem sincronização.

## Logs lidos direto do BlueOS

A partir da 0.6 o Bravo Viewer lê os `.tlog`/`.bin` e os logs de serviços **onde o BlueOS os grava**, montados somente leitura no contêiner (`/root/.config/blueos/ardupilot-manager/logs` e `/var/logs/blueos/services`). Nada é baixado nem copiado: cada log é indexado uma vez (resumo, ocorrências, séries de 10 s) e relido sob demanda. Na atualização, as cópias feitas pelas versões anteriores são trocadas pelo log original e removidas. O Bravo nunca apaga nem altera logs do BlueOS.

## Bravo Viewer Desktop (Windows 11)

Programa para o PC da base que copia os dados de um ou mais ROVs e faz o processamento pesado no computador. Abre em janela própria do Windows (WebView2, já incluído no Windows 11).

- **Instalador:** `Bravo-Viewer-Desktop-v<versão>-Setup.exe` na [release](https://github.com/Reikjv/Bravo-Viewer/releases). Cria atalhos no Menu Iniciar (e opcionalmente na Área de Trabalho), aparece em Aplicativos instalados e atualiza a instalação anterior ao instalar uma versão nova.
- **Instalação silenciosa (hub de programas):** `Bravo-Viewer-Desktop-v<versão>-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`. Pasta padrão `C:\Program Files\Bravo Viewer`; use `/DIR=` para outra pasta e `/CURRENTUSER` para instalar sem administrador.
- **Portátil:** `Bravo-Viewer-Desktop-v<versão>-portable.zip`, extrair e abrir `BravoViewerDesktop.exe`.

**Atualização automática:** o Desktop instalado consulta as releases do GitHub ao abrir e a cada 6 horas. Havendo versão nova, mostra um aviso com **Atualizar agora**; ao confirmar, baixa o instalador, confere o SHA-256, instala em silêncio e reabre. Dados, ROVs e login da nuvem são mantidos. A cópia portátil apenas avisa e aponta para a página da versão.

**Login da nuvem:** basta entrar uma vez em cada PC. A sessão é renovada sozinha e fica protegida pela conta do Windows; só é pedida de novo após **Sair** ou se a conta for desativada.

Sem assinatura digital, o Windows pode exibir "O Windows protegeu o computador": use **Mais informações → Executar assim mesmo**.

1. Ao abrir, o programa procura sozinho os ROVs com Bravo Viewer na rede (cabo do ROV e rede da base) e repete a busca a cada 2 minutos. A tela **Frota** mostra um cartão por ROV com status, operação atual e sincronização. **Adicionar por IP** continua disponível.
2. Com o ROV na rede, o Desktop sincroniza a cada minuto: baixa os `.tlog` completos (retomando cópias interrompidas e conferindo o SHA-256) e **processa tudo no PC**. Do ROV vem pronto apenas o que não tem log, como a coleta ao vivo. Na bancada, 177 sessões e 3,7 milhões de mensagens do Navigator foram baixadas e processadas em 58 s; as sincronizações seguintes levam frações de segundo.
3. Cada ROV é identificado por um código interno fixo; renomear o equipamento (Bravo 1, Bravo 10…) não mistura históricos. Vários ROVs ficam no seletor **Equipamento**.
4. O PC guarda todas as operações, inclusive as já removidas do ROV pelo limite de 20. Exportações e ZIPs são gerados no PC, com o nome do equipamento e da operação (`Bravo-10_Inspecao-Casco_2026-10-11.zip`).

**Nuvem (Supabase):** em Configuração → Nuvem, entre com a conta da pessoa (URL do projeto, chave publishable, e-mail e senha). A senha não é gravada e a sessão fica protegida pela conta do Windows. O envio é automático e em fila: operações, resumos, gráficos e logs originais (upload retomável). Sem internet, continua quando a conexão voltar.

Nome, local e encerramento da operação são feitos no Bravo Viewer do ROV; no Desktop esses campos são somente leitura. Os dados ficam em `%LOCALAPPDATA%\BravoViewerDesktop`. Uma segunda abertura do programa apenas mostra a janela existente. Envio para banco online e painel do gestor são as próximas etapas.

## Limites da beta

- Tempo armado inclui bancada. Lacunas de mais de cinco segundos não contam como operação contínua.
- Contadores podem ser reiniciados. Cada arquivo ou coleta diária UTC é uma sessão; arquivos sobrepostos não são somados.
- Ah/Wh são acumuladores registrados; não há estimativa para preencher dados ausentes. Sentinelas desconhecidas não são apresentadas como medições válidas.
- A API é amostrada e não captura todas as mensagens MAVLink transmitidas. O inventário de arquivos cobre as pastas consultadas de autopiloto/serviços, não todos os volumes privados de outras extensões.
- Histórico/originais persistem em `/usr/blueos/extensions/reikjv-rov-history`; acompanhe o espaço livre. Sessões fora do limite de operações são removidas automaticamente; sessões sem operação (logs anteriores à operação aberta) são preservadas. Apenas os cinco ZIPs exportados mais recentes são mantidos; o ZIP de uma operação guardada pode ser gerado de novo.
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
