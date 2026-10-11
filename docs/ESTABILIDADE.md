# Estabilidade · Bravo Viewer 0.2.0-beta.1

Ensaios realizados em 10/10/2026, no computador de desenvolvimento Windows. A carga HTTP e o servidor rodaram no mesmo processo Python; nenhuma consulta foi enviada ao ROV nesses ensaios. Cada teste usou uma cópia SQLite da captura com 1.054.950 registros.

| Medida | Primeiro ensaio | Séries alternadas entre fontes |
| --- | ---: | ---: |
| Duração medida | 91,74 s | 302,37 s |
| Clientes simultâneos | 8 | 8 |
| Respostas 200 | 20.560 | 23.364 |
| Respostas 503 de proteção | 0 | 29.177 |
| Falhas inesperadas / 500 / timeouts | 0 | 0 |
| Health, p95 | 2,46 ms | 2,57 ms |
| Health, máximo | 11,10 ms | 14,35 ms |
| Pico RSS do processo | 52,19 MiB | 64,50 MiB |
| RSS no fim da carga | 52,03 MiB | 60,67 MiB |
| Amostras simuladas gravadas durante a carga | 45 | 150 |
| Registros no ZIP exportado | 1.054.950 | 1.054.950 |
| Integridade SQLite e CRC do ZIP | OK | OK |

No segundo ensaio, 12 combinações de fonte/campo competiram por um cache de oito séries e dois lugares para consultas pesadas. O servidor retornou 503 quando ambos estavam ocupados, sem iniciar outra consulta pesada. Esses pedidos não produziram gráfico; a interface informa a ocupação e permite tentar novamente. As demais rotas permaneceram atendendo.

Cada JSONL do ZIP foi contado e comparado ao manifesto. As amostras adicionadas depois do início da leitura consistente não entraram no ZIP, mas permaneceram no banco. O exportador usa fluxo de escrita, mantém apenas 200 ocorrências na prévia HTML e grava as demais em CSV.

## Proteções implementadas

- Cache com oito séries, invalidado por alteração da fonte; a tela consulta tabelas/gráficos ao abrir a aba.
- Cancelamento de consulta de gráfico anterior quando o usuário troca o indicador.
- Duas consultas pesadas simultâneas e até 32 atendimentos HTTP; sockets com tempo limite.
- Uma exportação ativa, fila de até 16 tarefas ativas, deduplicação de buscas no mesmo BlueOS.
- Importações transacionais e exportação com instantâneo SQLite; arquivo parcial substituído somente após conclusão.
- Escrita do ZIP em fluxo, limpeza de parciais interrompidos e retenção de cinco ZIPs concluídos.
- Contêiner com limite de 256 MiB e 0,5 CPU. Histórico e originais persistem no volume.

## Reprodução

```powershell
python tools/test_stability.py --source data/preview --seconds 300 --clients 8
```

O comando cria um diretório novo em `data/stability/`, copia a base e produz `report.json`, incluindo amostras de memória. Não use esse resultado como medição de desempenho do Raspberry Pi: o hardware e os limites de CPU do contêiner alteram os tempos.

Os testes de backend e DOM/API verificam funcionalidades e controles; não substituem inspeção visual de um navegador. Builds ARM sob QEMU verificam construção/inicialização. Ainda é necessário ensaio no Navigator com dados representativos, coleta por horas, quedas de rede, reinício da extensão, espaço disponível e comparação de períodos armados com os horários anotados pelo operador.
