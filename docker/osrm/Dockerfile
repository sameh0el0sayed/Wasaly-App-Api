# Stage 1: download the OSM extract with a curl image (no apt-get in the OSRM image: its Debian base is end-of-life)
FROM curlimages/curl:8.10.1 AS pbf
ARG OSM_REGION_URL=https://download.geofabrik.de/asia/bahrain-latest.osm.pbf
RUN curl -fsSL --retry 3 --retry-delay 5 -o /tmp/bahrain.osm.pbf "$OSM_REGION_URL"

# Stage 2: OSRM; the graph is built once at first start from the bundled extract and kept in the osrmdata volume
FROM ghcr.io/project-osrm/osrm-backend:v5.27.1
WORKDIR /data
COPY --from=pbf /tmp/bahrain.osm.pbf /opt/seed/bahrain.osm.pbf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
