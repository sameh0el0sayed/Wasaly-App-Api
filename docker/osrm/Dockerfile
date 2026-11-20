FROM ghcr.io/project-osrm/osrm-backend:v5.27.1

# curl is needed by the entrypoint to download the OSM extract (the base image does not ship it)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /data

COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
