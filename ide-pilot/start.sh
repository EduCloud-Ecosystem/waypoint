#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
set -eu
pilot_root=$(cd "$1" && pwd)
docker run -d --name waypoint-mobile-ide-pilot --cpus 2 --memory 2g \
  -p 127.0.0.1:58771:8080 --env-file "$pilot_root/server.env" \
  --mount "type=bind,src=$pilot_root/project,dst=/home/coder/project" \
  --mount "type=bind,src=$pilot_root/state,dst=/home/coder/state" \
  waypoint-mobile-ide:pilot --bind-addr 0.0.0.0:8080 --auth password \
  --disable-telemetry --disable-update-check --user-data-dir /home/coder/state \
  --extensions-dir /home/coder/.local/share/code-server/extensions /home/coder/project
