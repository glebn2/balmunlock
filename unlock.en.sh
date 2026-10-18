#!/usr/bin/env bash
# Host safety workflow, modified 2026-10-17. Original authors: see README.md.
set -euo pipefail
if ! command -v python3 >/dev/null 2>&1; then
    echo "Python 3 is required. Follow the Linux setup section in README.md." >&2
    exit 1
fi
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$ROOT/tools/unlock.py" --lang en "$@"
