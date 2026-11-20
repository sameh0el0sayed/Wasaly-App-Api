#!/usr/bin/env bash
# Builds the OSRM graph and serves it.
# The dataset lives in a Docker volume, so restarts are instant.
set -euo pipefail

DATA_DIR="${OSRM_DATA_DIR:-/data}"
PBF="$DATA_DIR/bahrain.osm.pbf"
SEED="${OSRM_SEED_PBF:-/opt/seed/bahrain.osm.pbf}"
PORT="${OSRM_PORT:-5000}"

mkdir -p "$DATA_DIR"

find_prefix() {
    find "$DATA_DIR" -maxdepth 1 -type f -name '*.osrm' | head -n 1 || true
}

OSRM="$(find_prefix)"
if [ -n "$OSRM" ] && [ -f "$OSRM.hsgr" ]; then
    echo "[osrm] existing data found: $OSRM (no download needed)"
else
    if [ -z "$OSRM" ]; then
        # nothing prepared in the volume: use the extract bundled into the image at build time (no network needed)
        if [ ! -f "$PBF" ]; then
            if [ ! -s "$SEED" ]; then
                echo "[osrm] ERROR: no prepared graph in $DATA_DIR and no bundled extract at $SEED. Rebuild the image or put a prepared graph into the osrmdata volume." >&2
                exit 1
            fi
            cp "$SEED" "$PBF"
        fi
        echo "[osrm] extracting road network (car profile)..."
        osrm-extract -p /opt/car.lua "$PBF"
        OSRM="$(find_prefix)"
        if [ -z "$OSRM" ]; then
            echo "[osrm] ERROR: osrm-extract did not produce a .osrm file" >&2
            exit 1
        fi
    fi
    echo "[osrm] contracting graph..."
    osrm-contract "$OSRM"
    echo "[osrm] data ready: $OSRM"
fi

echo "[osrm] serving on port $PORT (OpenStreetMap data © OpenStreetMap contributors)"
exec osrm-routed --algorithm ch --port "$PORT" --max-matching-table-size 10000 "$OSRM"
