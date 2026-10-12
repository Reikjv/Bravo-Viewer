FROM python:3.12-slim AS wheels
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
COPY --from=wheels /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels
COPY rov_history ./rov_history
COPY web ./web
COPY docs/catalogos ./docs/catalogos
EXPOSE 8090
LABEL version="0.8.0-beta.1"
LABEL org.opencontainers.image.title="Bravo Viewer"
LABEL org.opencontainers.image.source="https://github.com/Reikjv/Bravo-Viewer"
LABEL org.reikjv.history.managed="true"
LABEL type="tool"
LABEL tags='["logs","history","monitoring","export"]'
LABEL permissions='{"HostConfig":{"NetworkMode":"host","Memory":268435456,"NanoCpus":500000000,"Binds":["/usr/blueos/extensions/reikjv-rov-history:/data","/root/.config/blueos/ardupilot-manager/logs:/blueos/ardupilot_logs/logs:ro","/var/logs/blueos/services:/blueos/system_logs:ro"]}}'
LABEL authors='[{"name":"Reikjv","email":"141414061+Reikjv@users.noreply.github.com"}]'
LABEL company='{"name":"Bravo Viewer","about":"Histórico e diagnóstico do ROV · Reikjv"}'
LABEL readme="https://raw.githubusercontent.com/Reikjv/Bravo-Viewer/main/README.md"
LABEL links='{"github":"https://github.com/Reikjv/Bravo-Viewer","documentation":"https://github.com/Reikjv/Bravo-Viewer/blob/main/README.md"}'
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/health',timeout=3).read()"
CMD ["python", "-m", "rov_history.app", "--data-dir", "/data", "--port", "8090", "--collect"]
