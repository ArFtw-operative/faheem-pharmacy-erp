#!/usr/bin/env bash
# install-agent.sh — same as: faheem-support install (kept as a file name for runbooks and muscle memory)
exec "$(dirname "$(readlink -f "$0")")/faheem-support" install "$@"
