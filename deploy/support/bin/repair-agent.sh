#!/usr/bin/env bash
# repair-agent.sh — same as: faheem-support repair (kept as a file name for runbooks and muscle memory)
exec "$(dirname "$(readlink -f "$0")")/faheem-support" repair "$@"
