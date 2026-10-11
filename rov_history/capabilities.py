"""Escopo da primeira versão e possibilidades sem confundir protocolo com medição."""


def capabilities(store):
    overview = store.overview()
    streams = store.streams()
    types = {stream["type"].split(":")[0] for stream in streams}
    statistics = overview["statistics"]
    rows = []

    def add(name, scope, sources, condition, present=None):
        found = sorted(types.intersection(sources))
        if present is None:
            present = bool(found)
        rows.append({"name": name, "scope": scope,
                     "data": "Registros identificados" if present else "Ainda sem registros identificados",
                     "sources": found, "condition": condition})

    add("Horas do autopiloto e inicializações", "Disponível nesta versão", {"PARAM_VALUE", "PARM"},
        "Contadores STAT_RUNTIME e STAT_BOOTCNT; podem ser reiniciados. Não equivalem a horas vitalícias garantidas.",
        "STAT_RUNTIME" in statistics or "STAT_BOOTCNT" in statistics)
    add("Tempo armado e operações", "Disponível nesta versão", {"HEARTBEAT"},
        "Sessões por arquivo/coleta, períodos armados e observações. Tempo armado inclui bancada; intervalos sem dados não são contabilizados.")
    add("Tensão, corrente, consumo e energia", "Disponível nesta versão", {"SYS_STATUS", "BATTERY_STATUS", "BAT"},
        "Exige monitor configurado e leituras válidas. Ah/Wh só aparecem quando registrados; acumuladores não são somados entre fontes.")
    add("Ocorrências e alertas", "Disponível nesta versão", {"STATUSTEXT", "ERR", "EV", "MSG", "LOG_TEXT"},
        "Mensagens presentes nos logs. Ausência de mensagem não comprova ausência de falha.")
    add("Gráficos e exportação", "Disponível nesta versão", {"SYS_STATUS", "BATTERY_STATUS", "BAT", "HEARTBEAT"},
        "Gráficos de alimentação e estado armado; CSV/JSONL por tipo, resumo JSON e arquivos originais.")
    add("Inventário completo dos registros", "Disponível nesta versão", types,
        "Arquivos das pastas de logs consultadas e todos os tipos/campos decodificados. Inclui acesso bruto a dados sem painel dedicado.",
        bool(streams))
    add("Profundidade máxima e média por operação", "Disponível nesta versão · depende de sensor", {"SCALED_PRESSURE2", "BARO"},
        "Usa a profundidade informada pelo ArduSub (GLOBAL_POSITION_INT) somente quando o sensor externo é registrado. A média considera o tempo abaixo de 0,5 m. Pressão interna ou altitude estimada não comprovam profundidade.")
    add("Operações com encerramento e limite de histórico", "Disponível nesta versão", set(),
        "Cada operação agrupa as sessões do período. Ao encerrar, o resumo é congelado, o ZIP é gerado e a próxima operação começa. As operações encerradas mais antigas são removidas ao passar do limite configurado.",
        True)
    add("Atitude, IMU, bússola e vibração", "Painel futuro", {"ATTITUDE", "AHRS", "AHRS2", "RAW_IMU", "SCALED_IMU", "SCALED_IMU2", "ATT", "IMU", "MAG", "VIBE"},
        "Tipos existentes podem ser consultados/exportados no inventário. Análise técnica e gráficos dedicados ficam para uma etapa posterior.")
    add("Posição, velocidade e trajetória", "Painel futuro · depende de sensor", {"GPS_RAW_INT", "GLOBAL_POSITION_INT", "LOCAL_POSITION_NED", "GPS", "POS"},
        "Verificar fix, qualidade e referencial de GPS/DVL/INS/acústica. Existência do tipo ou coordenadas zero não validam uma posição.")
    add("Navegação externa DVL/INS/acústica", "Integração futura · depende de sensor", {"ODOMETRY", "VISION_POSITION_ESTIMATE", "VISION_SPEED_ESTIMATE", "VISP", "VISO"},
        "Depende da integração e dos arquivos/API do sensor; telemetria enviada ao autopiloto não garante acesso aos dados brutos.")
    add("Comandos do piloto e saídas dos motores", "Painel futuro", {"MANUAL_CONTROL", "RC_CHANNELS", "SERVO_OUTPUT_RAW", "RCIN", "RCOU"},
        "Entradas e PWM podem ser inventariados. Comando de motor não mede RPM ou corrente individual.")
    esc = {name for name in types if name.startswith("ESC_TELEMETRY") or name == "ESC"}
    add("RPM, corrente e temperatura por ESC", "Painel futuro · depende de hardware", esc,
        "Exige ESCs que transmitam telemetria e integração habilitada. Não é calculado a partir do PWM.")
    add("Temperatura e carga restante da bateria", "Painel futuro · depende de configuração", {"BATTERY_STATUS", "BAT"},
        "Campos podem existir com sentinelas de desconhecido. Exige sensor/configuração válidos; a presença do tipo não garante a leitura.")
    add("Qualidade do estimador EKF", "Painel futuro", {"EKF_STATUS_REPORT", "XKF1", "XKF2", "XKF3", "XKF4", "NKF1", "NKF4"},
        "Diagnóstico dedicado depende dos tipos e da versão do estimador. Campos encontrados permanecem no inventário.")
    add("Sonar e distância ao fundo", "Integração futura · depende de sensor", {"RANGEFINDER", "DISTANCE_SENSOR", "RFND"},
        "Exige sensor e publicação habilitados. Imagens completas do sonar podem estar em arquivos próprios.")
    system = {name for name in types if name.startswith("SYSTEM_")}
    add("CPU, memória, disco, rede e temperatura BlueOS", "Painel futuro", system,
        "Logs de serviços podem ser inventariados/exportados. Gráficos dedicados do computador ficam para outra etapa.")
    add("Vídeos, gravações MCAP e dados de outras extensões", "Compatibilidade futura", set(),
        "Dependem da versão do BlueOS e de cada aplicação. Esta versão importa .tlog, .bin, .log e .gz; não indexa vídeos ou MCAP.")
    return {"version_scope": "Horas, operações, energia e ocorrências", "rows": rows,
            "note": "A disponibilidade abaixo considera o histórico importado/coletado. Tipo identificado não significa sensor válido. Painel futuro não possui análise dedicada nesta versão."}
