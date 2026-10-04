#!/usr/bin/env bash
# disable-support.sh — same as: faheem-support disable (kept as a file name for runbooks and muscle memory)
exec "$(dirname "$(readlink -f "$0")")/faheem-support" disable "$@"
