# demo-internal-shiny: instantiated from templates/shiny.Dockerfile.
# Same base as the public app; the auth gate is applied by oauth2-proxy, not here.
FROM rocker/r2u:24.04

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN install.r shiny

WORKDIR /srv/shiny
COPY app/ /srv/shiny/

EXPOSE 3838
ENV SHINY_PORT=3838 SHINY_HOST=0.0.0.0

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:3838/ || exit 1

CMD ["R", "-q", "-e", "shiny::runApp('/srv/shiny', host=Sys.getenv('SHINY_HOST','0.0.0.0'), port=as.integer(Sys.getenv('SHINY_PORT','3838')))"]
