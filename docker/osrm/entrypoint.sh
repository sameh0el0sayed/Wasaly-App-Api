#!/usr/bin/env bash
# Downloads the Bahrain OSM extract once, builds the OSRM graph and serves it.
# The dataset lives in a Docker volume, so restarts are instant.
set -euo pipefail

DATA_DIR="${OSRM_DATA_DIR:-/data}"
PBF_URL="${OSM_REGION_URL:-https://download.geofabrik.de/asia/bahrain-latest.osm.pbf}"
PBF="$DATA_DIR/bahrain.osm.pbf"
PORT="${OSRM_PORT:-5000}"

mkdir -p "$DATA_DIR"

find_prefix() {
    find "$DATA_DIR" -maxdepth 1 -type f -name '*.osrm' | head -n 1 || true
}

OSRM="$(find_prefix)"
if [ -z "$OSRM" ] || [ ! -f "$OSRM.hsgr" ]; then
    echo "[osrm] downloading Bahrain OSM extract from $PBF_URL ..."
    curl -fsSL --retry 3 --retry-delay 5 -o "$PBF" "$PBF_URL"
    echo "[osrm] extracting road network (car profile)..."
    osrm-extract -p /opt/car.lua "$PBF"
    OSRM="$(find_prefix)"
    if [ -z "$OSRM" ]; then
        echo "[osrm] ERROR: osrm-extract did not produce a .osrm file" >&2
        exit 1
    fi
    echo "[osrm] contracting graph..."
    osrm-contract "$OSRM"
    echo "[osrm] data ready: $OSRM"
else
    echo "[osrm] existing data found: $OSRM"
fi

echo "[osrm] serving on port $PORT (OpenStreetMap data © OpenStreetMap contributors)"
exec osrm-routed --algorithm ch --port "$PORT" --max-matching-table-size 10000 "$OSRM"
