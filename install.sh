#!/usr/bin/env bash
# Instala somente o contêiner de histórico; preserva o volume e a versão anterior.
set -Eeuo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
VERSION="0.2.0-beta.1"
IMAGE="bravo-viewer:$VERSION"
NAME="bravo-viewer"
DATA="/usr/blueos/extensions/reikjv-rov-history"
command -v docker >/dev/null || { echo "Docker não encontrado; execute no BlueOS." >&2; exit 1; }
ARCH="$(docker version --format '{{.Server.Arch}}')"
case "$ARCH" in
  arm64|aarch64) PACKAGE_ARCH=arm64 ;;
  arm|armv7l|armhf) PACKAGE_ARCH=armv7 ;;
  *) echo "Arquitetura Docker não suportada: $ARCH" >&2; exit 1 ;;
esac
if [[ -f image.tar.gz ]]; then
  sha256sum --quiet -c SHA256SUMS
  [[ "$(cat architecture.txt)" == "$PACKAGE_ARCH" ]] || { echo "Baixe o pacote $PACKAGE_ARCH." >&2; exit 1; }
  docker load -i image.tar.gz
elif [[ "${1:-}" == "--build" ]]; then
  docker build -t "$IMAGE" .
else
  echo "Use o ZIP offline da arquitetura $PACKAGE_ARCH ou bash install.sh --build com internet." >&2
  exit 1
fi
[[ "$(docker image inspect --format '{{.Architecture}}' "$IMAGE")" == "${PACKAGE_ARCH/armv7/arm}" ]] || { echo "Imagem incompatível." >&2; exit 1; }
mkdir -p -- "$DATA"
BACKUP=""
PREVIOUS_NAME="$NAME"
if ! docker container inspect "$NAME" >/dev/null 2>&1 && docker container inspect "reikjv-rov-history" >/dev/null 2>&1; then PREVIOUS_NAME="reikjv-rov-history"; fi
if docker container inspect "$PREVIOUS_NAME" >/dev/null 2>&1; then
  [[ "$(docker inspect --format '{{index .Config.Labels "org.reikjv.history.managed"}}' "$PREVIOUS_NAME")" == true ]] || { echo "Contêiner de mesmo nome não identificado; revise manualmente." >&2; exit 1; }
  BACKUP="$NAME-backup-$(date +%Y%m%d%H%M%S)"
  docker stop "$PREVIOUS_NAME"
  docker rename "$PREVIOUS_NAME" "$BACKUP"
fi
INSTALL_ID="$(date +%Y%m%d%H%M%S)-$$"
rollback() {
  echo "Falha ao iniciar; revertendo o contêiner."
  if [[ "$(docker inspect --format '{{index .Config.Labels "org.reikjv.history.install"}}' "$NAME" 2>/dev/null || true)" == "$INSTALL_ID" ]]; then docker rm -f "$NAME" >/dev/null; fi
  if [[ -n "$BACKUP" ]]; then docker rename "$BACKUP" "$PREVIOUS_NAME"; docker start "$PREVIOUS_NAME"; fi
}
trap rollback ERR
docker run -d --name "$NAME" --label "org.reikjv.history.install=$INSTALL_ID" \
  --restart unless-stopped --network host --memory 256m --cpus .5 \
  -v "$DATA:/data" "$IMAGE"
HEALTHY=false
for attempt in {1..30}; do
  if docker exec "$NAME" python -c 'import json,urllib.request; from rov_history import __version__; assert json.load(urllib.request.urlopen("http://127.0.0.1:8090/health",timeout=2))["version"] == __version__' >/dev/null 2>&1; then HEALTHY=true; break; fi
  sleep 1
done
[[ "$HEALTHY" == true ]]
trap - ERR
echo "Bravo Viewer iniciado: http://IP_DO_BLUEOS:8090/"
[[ -z "$BACKUP" ]] || echo "Contêiner anterior preservado e parado: $BACKUP"
