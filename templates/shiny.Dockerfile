# shiny.Dockerfile: templated image for R Shiny tenants on the EduCloud layer.
#
# Base: rocker/r2u (Ubuntu 24.04 "noble"). In r2u, install.packages()/install.r
# pulls prebuilt CRAN binaries through bspm/apt, which cuts R build times far
# below compile-from-source. Verified against rocker-project.org and
# github.com/eddelbuettel/r2u at build time; re-check before relying on it.
#
# One template covers the whole R portfolio. Per tenant, set R_PKGS to the app's
# declared package list and put the app source under ./app in the build context
# (app.R, or ui.R + server.R). This image runs the app directly with
# shiny::runApp; it does NOT use shiny-server (one app per container, Traefik is
# the proxy).

FROM rocker/r2u:24.04

# curl is only for the container healthcheck below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# App package list. Override per tenant, for example:
#   docker build --build-arg R_PKGS="shiny ggplot2 dplyr" -t demo-public-shiny .
# r2u resolves these to apt binaries.
ARG R_PKGS="shiny"
RUN install.r ${R_PKGS}

# App source. Self-contained, relative paths only (onboarding contract).
ARG APP_DIR=app
WORKDIR /srv/shiny
COPY ${APP_DIR}/ /srv/shiny/

# Shiny listens on 3838 inside the container. Bind 0.0.0.0 so Traefik can reach
# it on the Docker network; the port is never published to the host directly.
EXPOSE 3838
ENV SHINY_PORT=3838 SHINY_HOST=0.0.0.0

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:3838/ || exit 1

CMD ["R", "-q", "-e", "shiny::runApp('/srv/shiny', host=Sys.getenv('SHINY_HOST','0.0.0.0'), port=as.integer(Sys.getenv('SHINY_PORT','3838')))"]
