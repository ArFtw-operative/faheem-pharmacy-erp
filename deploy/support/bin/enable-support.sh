#!/usr/bin/env bash
# enable-support.sh — same as: faheem-support enable (kept as a file name for runbooks and muscle memory)
exec "$(dirname "$(readlink -f "$0")")/faheem-support" enable "$@"
