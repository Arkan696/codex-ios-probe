#!/bin/sh
# run.sh SIMULATOR_STATICLIB [swiftc link argument ...]
set -eu
exec python3 -B "$(dirname "$0")/smoke.py" "$@"
