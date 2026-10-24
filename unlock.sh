#!/usr/bin/env bash
# Common Linux entry point. The Python controller selects RU/EN automatically.
set -euo pipefail
if ! command -v python3 >/dev/null 2>&1; then
    case "${LC_ALL:-${LC_MESSAGES:-${LANG:-C}}}" in
        ru|ru_*|ru-*|ru.*|ru@*) echo "Нужен Python 3. Настройте зависимости по разделу Linux в README.md." >&2 ;;
        *) echo "Python 3 is required. Follow the Linux setup section in README.md." >&2 ;;
    esac
    exit 1
fi
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 -X utf8 "$ROOT/tools/unlock.py" "$@"
