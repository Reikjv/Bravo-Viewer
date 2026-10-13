# Testes de bancada

Alvo: BlueOS 1.4.6, ArduSub 4.7.1, Navigator, Bravo Viewer 0.8 no ROV e Bravo Viewer Desktop 0.8 em Windows 11. Registre as versões e o nome do equipamento nas observações da operação.

## ROV

1. Instale pelo catálogo do Bravo Viewer no gerenciador de Extensões (ou pelo ZIP offline da arquitetura do servidor Docker). Escolha só um método.
2. Abra `http://IP_DO_BLUEOS:8090/`, confira nome e endereço em Configuração e dê nome, local e cliente à operação aberta em **Operações**.
3. Confira em **Arquivos** que aparecem só os logs de voo (`.tlog`/`.bin`) e que o log da inicialização atual entra depois de reiniciar o ROV (a coleta ao vivo cobre o intervalo).
4. Ao voltar para a base, use **Encerrar operação** e abra o `relatorio.html` do ZIP.

## Desktop

1. Instale o `-Setup.exe` da release. Ao abrir, o programa deve encontrar o ROV sozinho (cabo e rede da base) e mostrá-lo na **Frota**, uma vez só, mesmo respondendo por dois endereços.
2. Aguarde a sincronização: os logs são baixados, conferidos e processados no PC. A seguinte, sem novidades, leva frações de segundo.
3. Em **Configuração → Nuvem**, entre uma vez com a conta do PC (operador nos PCs de operação/oficina, gestão nos de gestão). O envio e o recebimento seguem sozinhos a cada minuto.
4. Em um segundo PC conectado à mesma nuvem, confira que o ROV aparece na Frota como **Pela nuvem**, com operações, gráficos, mergulhos e relatório.

## Comportamentos esperados

| Ensaio | Resultado esperado |
| --- | --- |
| Ligar e desligar o ROV várias vezes | Uma sessão por inicialização, todas na mesma operação aberta |
| Encerrar operação | Logs mais recentes buscados no BlueOS, resumo congelado, ZIP gerado e próxima operação aberta |
| Relatório da operação | Indicadores, tempo armado médio, mergulhos, picos com horário, gráficos com pausas marcadas e erros/avisos agrupados; sem listas de mensagens |
| Operação além do limite | O ROV guarda as 20 últimas encerradas; ao encerrar a 21ª, a mais antiga é removida no ROV (o PC mantém) |
| Logs de serviço do BlueOS | Não aparecem em listas, relatórios nem sincronização; continuam no BlueOS |
| Logs no ROV | Lidos onde o BlueOS grava, sem cópia; o Bravo nunca apaga logs do BlueOS |
| Cópia interrompida para o PC | Retoma de onde parou e confere o SHA-256 |
| Desktop sem internet | Fila da nuvem pendente; continua quando a conexão voltar |
| Segundo PC na mesma conta | Recebe o que o primeiro enviou; nada volta para a nuvem em vão |
| Profundidade sem sensor externo | "Sem sensor", nunca um valor estimado |
| Atualização do Desktop | Aviso de versão nova; ao confirmar, instala em silêncio e reabre com dados e login preservados |
| Interromper o acesso às APIs do BlueOS | Lacuna registrada; não é contada como tempo armado contínuo |

## Operação representativa

As capturas de bancada analisadas até aqui mostram o ROV desarmado. A validação física deve usar uma operação conduzida pelo operador, com horários conhecidos de armamento e desarmamento e medições de referência de alimentação. Compare os mergulhos do relatório com esses horários. O tempo armado soma intervalos de até 5 segundos entre heartbeats armados consecutivos; lacunas interrompem a contagem. Profundidade máxima e média só aparecem com o sensor externo registrado (`SCALED_PRESSURE2`).

## Evidência já obtida

- Testes automáticos de backend, interface (DOM/API), esquema do Supabase (33 verificações) e builds ARM/Windows passam no CI a cada versão.
- Logs de bancada (1.054.950 mensagens): banco de 234 MB para 0,46 MB no formato compacto; ensaio de carga com 8 clientes sem erros e pico de 81 MB de memória ([ESTABILIDADE.md](ESTABILIDADE.md) traz os ensaios anteriores).
- Navigator real: 177 sessões e 3,7 milhões de mensagens baixadas e processadas no PC em 58 s.
- Supabase real: envio de 183 sessões e 179 logs (199 MB) pelo Desktop e limpeza pela conta de gestão.

Ainda pendentes: ensaio prolongado no Navigator, operação representativa armada, sensor de profundidade e recebimento entre dois PCs reais.
