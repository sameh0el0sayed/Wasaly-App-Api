# Stage 1: download the GCC extract and clip it to Bahrain (Geofabrik has no Bahrain-only file).
# Done here, on a maintained Debian, so the OSRM image (end-of-life Debian base) never needs apt-get.
FROM debian:bookworm-slim AS pbf
ARG OSM_REGION_URL=https://download.geofabrik.de/asia/gcc-states-latest.osm.pbf
ARG BBOX=50.3,25.5,50.9,26.4
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates osmium-tool \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL --retry 3 --retry-delay 5 -o /tmp/gcc.osm.pbf "$OSM_REGION_URL" \
    && osmium extract -b "$BBOX" /tmp/gcc.osm.pbf -o /tmp/bahrain.osm.pbf --overwrite \
    && rm -f /tmp/gcc.osm.pbf \
    && test -s /tmp/bahrain.osm.pbf

# Stage 2: OSRM; the graph is built once at first start from the clipped extract and kept in the osrmdata volume
FROM ghcr.io/project-osrm/osrm-backend:v5.27.1
WORKDIR /data
COPY --from=pbf /tmp/bahrain.osm.pbf /opt/seed/bahrain.osm.pbf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
