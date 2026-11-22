# Stage 1: download a Bahrain OSM extract and clip it to the Bahrain bbox.
# Sources are tried in order: Bahrain-only extract (small, ~9 MB, daily) first, then the Geofabrik GCC extract
# (Geofabrik moved regions into subfolders, e.g. asia/gcc-states/, and has no Bahrain-only file).
# Done here, on a maintained Debian, so the OSRM image (end-of-life Debian base) never needs apt-get.
FROM debian:bookworm-slim AS pbf
ARG OSM_REGION_URLS="https://download.openstreetmap.fr/extracts/asia/bahrain-latest.osm.pbf https://download.geofabrik.de/asia/gcc-states/gcc-states-latest.osm.pbf"
ARG BBOX=50.3,25.5,50.9,26.4
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates osmium-tool \
    && rm -rf /var/lib/apt/lists/*
RUN set -e; ok=0; \
    for url in $OSM_REGION_URLS; do \
        echo "[pbf] downloading $url"; \
        rm -f /tmp/src.osm.pbf /tmp/bahrain.osm.pbf; \
        if curl -fsSL --retry 3 --retry-delay 5 --retry-all-errors --connect-timeout 20 \
                --speed-limit 20000 --speed-time 60 -o /tmp/src.osm.pbf "$url" \
            && osmium extract -b "$BBOX" /tmp/src.osm.pbf -o /tmp/bahrain.osm.pbf --overwrite \
            && test -s /tmp/bahrain.osm.pbf; then ok=1; break; fi; \
        echo "[pbf] failed: $url"; \
    done; \
    rm -f /tmp/src.osm.pbf; \
    test "$ok" = 1 || { echo "[pbf] ERROR: all OSM sources failed" >&2; exit 1; }

# Stage 2: OSRM; the graph is built once at first start from the clipped extract and kept in the osrmdata volume
FROM ghcr.io/project-osrm/osrm-backend:v5.27.1
WORKDIR /data
COPY --from=pbf /tmp/bahrain.osm.pbf /opt/seed/bahrain.osm.pbf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
