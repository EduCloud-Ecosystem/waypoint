# demo-static-report: static HTML served by nginx. No template needed (the Shiny
# and Python templates do not apply to static content); this is the canonical
# static-app pattern Phase 6 uses (a single small nginx container).
FROM nginx:1.27-alpine

COPY site/ /usr/share/nginx/html/

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD wget -qO- http://127.0.0.1/ >/dev/null 2>&1 || exit 1
