#!/bin/zsh
# SPDX-License-Identifier: Apache-2.0
set -eu
case "${1:---live}" in
  --live) mode=--first-use ;;
  --offline) mode=--first-use-offline ;;
  *) print -u2 'Choose --live or --offline.'; exit 2 ;;
esac
exec "${0:A:h}/launch-nhanes-instructor.command" "$mode"
