# Testes de bancada da beta

Alvo: BlueOS 1.4.6, ArduSub 4.7.1, Navigator. A primeira versão cobre horas, operações, energia, ocorrências e inventário. Registre a versão do pacote e o nome do equipamento nas observações da sessão.

## Primeiro acesso

1. Instale pelo catálogo público do Bravo Viewer no gerenciador de Extensões ou use o ZIP correspondente à arquitetura do servidor Docker, seguindo o README. Escolha somente um método. O volume próprio da extensão preserva os dados entre atualizações.
2. Abra `http://IP_DO_BLUEOS:8090/` e confira nome/endereço em Configuração. Dentro do contêiner com rede do host, `127.0.0.1` acessa os serviços do BlueOS.
3. Acompanhe a busca dos arquivos pelo indicador de progresso. Confira arquivos importados e o inventário remoto, incluindo .bin vazios.
4. Compare um arquivo com o Log Browser: tipos presentes, quantidade de registros, campo de tensão e alertas. A cópia de um arquivo ativo pode divergir porque o original continua crescendo.

## Comportamentos esperados

| Ensaio | Resultado esperado |
| --- | --- |
| Importar o mesmo arquivo duas vezes | Histórico não duplicado; aviso de arquivo conhecido |
| Atualizar cópia da mesma fonte no BlueOS | Mesma sessão atualizada atomicamente; sem soma com a versão anterior |
| Consultar tensão e corrente | V e A no gráfico; CSV preserva valores registrados e JSONL mantém origem/campos |
| Consultar Ah/Wh | Último acumulador registrado da fonte; sem soma entre logs ou estimativa criada para preencher lacunas |
| Exportar um tipo | Todas as linhas da origem selecionada, mesmo que a tabela/gráfico mostre uma redução |
| Exportar Viewer: todo o histórico | ZIP com relatório HTML offline, sessões, contadores, gráficos e séries completas, ocorrências, inventário, possibilidades e dados por tipo/origem |
| Exportar Viewer: visão atual | Filtros respeitados; todas as linhas correspondentes, incluindo páginas fechadas; observações salvas incluídas |
| Incluir originais no ZIP | Originais importados presentes; coleta de API não inventa um original binário |
| Navegar durante exportação grande | Status/consultas leves e coleta continuam; consultas pesadas podem informar ocupação |
| Trocar de indicador repetidamente | Consulta anterior cancelada no navegador; gráfico final corresponde à seleção atual |
| Conferir identificação | Bravo Viewer e BRS na identificação, Reikjv apenas no rodapé |
| Consultar um dado desconhecido | Sentinela não aparece como medição válida nos indicadores |
| Consultar Dados e possibilidades | Recursos iniciais separados de painéis futuros; tipos encontrados separados da validade do sensor |
| Salvar observação e reiniciar somente a extensão | Observação, configuração, originais e histórico permanecem no volume |
| Fechar a tela com coleta habilitada | Backend continua coletando; novos registros aparecem ao atualizar/reabrir |
| Interromper o acesso da extensão às APIs | Estado de espera/erro; não preencher a lacuna com operação contínua |
| Restaurar acesso às APIs | Coleta retoma, preservando o histórico anterior |
| Importar arquivo com relógio descontínuo | Descontinuidade sinalizada e linha do gráfico interrompida entre segmentos |

## Operação representativa

A captura analisada nesta implementação só apresenta o ROV desarmado. A validação física deve usar uma operação conduzida pelo operador, com horários conhecidos de armamento/desarmamento e medições de referência de alimentação.

Compare os períodos identificados com esses horários e com os registros originais. O indicador de tempo armado observado contabiliza intervalos de até 5 segundos com dois heartbeats armados consecutivos; não presume o instante exato de uma transição entre amostras. Uma interrupção divide a continuidade. Os acumuladores STAT_* são os últimos valores registrados, não uma leitura contínua garantida nem um total vitalício.

Nesta beta, um arquivo ou coleta diária UTC constitui uma sessão. Registros .BIN sem data absoluta válida continuam acessíveis no inventário; a análise de períodos armados desta versão usa os heartbeats MAVLink do autopiloto 1:1. Profundidade, horas submersas, trajetória e telemetria por motor não fazem parte dos indicadores iniciais.

## Evidência já obtida

Os testes locais de backend e DOM/API passaram. A base de teste preserva 1.054.950 registros das cópias dos seis .tlog, incluindo um fragmento BAD_DATA. Os seis hashes originais foram conferidos. A exportação CSV da última fonte contém 2.344 linhas SYS_STATUS e a série de tensão corresponde a 24,535–24,729 V na captura.

Essas evidências validam leitura e controles locais. Builds ARM sob emulação, execução no BlueOS e a operação representativa são verificações distintas; registre o resultado de cada uma.

O ensaio local de carga da versão 0.2.0-beta.1 está documentado em [ESTABILIDADE.md](ESTABILIDADE.md). No Navigator, mantenha a coleta por algumas horas, exporte o histórico durante o uso, acompanhe memória/CPU/reinícios e espaço livre no BlueOS, reinicie somente a extensão e confira preservação dos dados. Interrompa/restaure acesso às APIs em bancada e compare lacunas com as interrupções anotadas. O ensaio físico deve ser conduzido pelo operador.
