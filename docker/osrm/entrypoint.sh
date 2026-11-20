#!/usr/bin/env bash
# Builds the OSRM graph and serves it.
# The dataset lives in a Docker volume, so restarts are instant.
set -euo pipefail

DATA_DIR="${OSRM_DATA_DIR:-/data}"
PBF="$DATA_DIR/bahrain.osm.pbf"
SEED="${OSRM_SEED_PBF:-/opt/seed/bahrain.osm.pbf}"
PORT="${OSRM_PORT:-5000}"

mkdir -p "$DATA_DIR"

# osrm-extract names its output after the pbf (bahrain.osm.pbf -> bahrain.osrm[.*]); newer versions write only bahrain.osrm.* files
OSRM="$DATA_DIR/bahrain.osrm"

if [ -f "$OSRM.hsgr" ]; then
    echo "[osrm] existing data found: $OSRM (no download needed)"
else
    if [ ! -f "$OSRM" ] && [ ! -f "$OSRM.ebg" ]; then
        if [ ! -f "$PBF" ]; then
            if [ ! -s "$SEED" ]; then
                echo "[osrm] ERROR: no bundled extract at $SEED. Rebuild the image." >&2
                exit 1
            fi
            cp "$SEED" "$PBF"
        fi
        echo "[osrm] extracting road network (car profile)..."
        osrm-extract -p /opt/car.lua "$PBF"
        if [ ! -f "$OSRM" ] && [ ! -f "$OSRM.ebg" ]; then
            echo "[osrm] ERROR: osrm-extract produced no output for $OSRM" >&2
            ls -la "$DATA_DIR" >&2
            exit 1
        fi
    fi
    echo "[osrm] contracting graph..."
    osrm-contract "$OSRM"
    echo "[osrm] data ready: $OSRM"
fi

echo "[osrm] serving on port $PORT (OpenStreetMap data © OpenStreetMap contributors)"
exec osrm-routed --algorithm ch --port "$PORT" --max-matching-table-size 10000 "$OSRM"
