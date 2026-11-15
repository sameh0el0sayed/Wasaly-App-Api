# Self-hosted OSRM backed by the Bahrain OpenStreetMap extract (Geofabrik).
# No paid API, no public demo server: all road-network processing stays on the VPS.
FROM ghcr.io/project-osrm/osrm-backend:v5.27.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5000
ENTRYPOINT ["/entrypoint.sh"]
