#!/usr/bin/env bash
# Host safety workflow, modified 2026-10-17. Original authors: see README.md.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$ROOT/tools/unlock.py" --lang ru "$@"
