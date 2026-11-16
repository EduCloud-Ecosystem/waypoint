# SPDX-License-Identifier: Apache-2.0
FROM ghcr.io/coder/code-server:4.145.2@sha256:09565f9e07f5624554e295e8bab0e2fec4a7c533eb3cff459ed6596a4d3d7c1c
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3 git && rm -rf /var/lib/apt/lists/*
COPY --chown=coder:coder extension /home/coder/.local/share/code-server/extensions/educloud.waypoint-mobile-0.1.0
RUN chown -R coder:coder /home/coder/.local
USER coder
RUN code-server --install-extension ms-python.python@2026.4.0 \
    --install-extension ms-python.vscode-python-envs@1.42.0 \
    --install-extension ms-python.debugpy@2026.6.0
RUN code-server --list-extensions --show-versions | grep -Fx "educloud.waypoint-mobile@0.1.0"
