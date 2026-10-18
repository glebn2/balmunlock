#!/usr/bin/env bash
# Host safety workflow, modified 2026-10-17. Original authors: see README.md.
set -euo pipefail
if ! command -v python3 >/dev/null 2>&1; then
    echo "Нужен Python 3. Настройте зависимости по разделу Linux в README.md." >&2
    exit 1
fi
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$ROOT/tools/unlock.py" --lang ru "$@"
