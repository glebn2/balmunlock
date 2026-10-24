#!/usr/bin/env bash
# Compatibility entry point with English selected explicitly.
set -euo pipefail
if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is required. Follow the Linux setup section in README.md." >&2
    exit 1
fi
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -X utf8 "$ROOT/tools/unlock.py" --lang en "$@"
