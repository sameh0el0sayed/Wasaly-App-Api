FROM ghcr.io/project-osrm/osrm-backend:v5.27.1

WORKDIR /data

COPY entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
