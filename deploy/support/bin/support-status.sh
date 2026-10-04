#!/usr/bin/env bash
# support-status.sh — same as: faheem-support status (kept as a file name for runbooks and muscle memory)
exec "$(dirname "$(readlink -f "$0")")/faheem-support" status "$@"
