#!/usr/bin/env bash
# Self-hosted OSRM + Bahrain OpenStreetMap data, WITHOUT Docker (optional alternative).
# The recommended path is the `osrm` service in docker-compose.yml, which does the same thing.
#
#   Ubuntu/Debian:  sudo apt-get install -y osrm-backend
#   then:           sudo PORT=5000 ./scripts/osrm_setup.sh
#
# No paid API, no subscription, no per-request charge. Data © OpenStreetMap contributors.
set -euo pipefail

DATA_DIR="${OSRM_DATA_DIR:-/var/lib/osrm}"
PBF_URL="${OSM_REGION_URL:-https://download.geofabrik.de/asia/bahrain-latest.osm.pbf}"
PBF="$DATA_DIR/bahrain.osm.pbf"
PORT="${PORT:-5000}"

mkdir -p "$DATA_DIR"
cd "$DATA_DIR"

find_prefix() { find "$DATA_DIR" -maxdepth 1 -type f -name '*.osrm' | head -n 1 || true; }

OSRM="$(find_prefix)"
if [ -z "$OSRM" ] || [ ! -f "$OSRM.hsgr" ]; then
    echo "[osrm] downloading Bahrain OSM extract..."
    curl -fsSL --retry 3 -o "$PBF" "$PBF_URL"
    echo "[osrm] extracting..."
    osrm-extract -p /opt/car.lua "$PBF"
    OSRM="$(find_prefix)"
    echo "[osrm] contracting..."
    osrm-contract "$OSRM"
fi

echo "[osrm] serving $OSRM on port $PORT (data © OpenStreetMap contributors)"
exec osrm-routed --algorithm ch --port "$PORT" --max-matching-table-size 10000 "$OSRM"
