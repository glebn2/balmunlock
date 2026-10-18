"""Console entry point used by the portable Windows distribution."""
import runpy
import sys

from runtime_paths import HELPERS, VERSION, configure_runtime


def main():
    try:
        configure_runtime()
    except (OSError, RuntimeError) as error:
        if "en" not in sys.argv:
            print("Не удалось загрузить файлы программы. Распакуйте весь релизный ZIP в доступную папку, "
                  "сохранив _internal, platform-tools и tools рядом с EXE. Бэкапы и журналы не удаляйте.", file=sys.stderr)
        print(f"STOP: {error}", file=sys.stderr)
        return 2
    if sys.argv[1:2] == ["--version"]:
        print(f"unmuda {VERSION}")
        return 0
    if sys.argv[1:2] == ["--internal-tool"]:
        if len(sys.argv) < 3 or sys.argv[2] not in HELPERS:
            print("STOP: unknown or missing internal helper", file=sys.stderr)
            return 2
        name, args = sys.argv[2], sys.argv[3:]
        sys.argv = [name, *args]
        runpy.run_module(HELPERS[name], run_name="__main__")
        return 0
    from unlock import main as unlock_main
    return unlock_main()


if __name__ == "__main__":
    raise SystemExit(main())
