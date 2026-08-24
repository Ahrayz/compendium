"""Dev server entry point: `python -m compendium [--reload]`.

Argument parsing only — the server itself lives in `compendium.devserver`,
which has to be an importable module for `--reload` to survive Windows'
spawn-and-pickle. See that module for why.
"""

import argparse

from compendium.devserver import serve, watch


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m compendium")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="restart on file change")
    args = parser.parse_args()

    run = watch if args.reload else serve
    return run(host=args.host, port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
