"""Local dev server, split out from `__main__` so `--reload` works on Windows.

Two Windows constraints stack up here.

1. psycopg's async driver cannot run on the default ProactorEventLoop — the pool
   fails every connection and startup dies on a 30s PoolTimeout. It needs a
   selector loop. The plain `uvicorn` CLI can't be fixed from inside the app,
   because uvicorn builds its loop before importing the app module, so the loop
   is constructed here via the supported `loop_factory`.

2. `--reload` spawns a subprocess, and Windows spawn *pickles* the target. A
   bound method pickles by qualified name, so `SelectorServer` has to live in a
   real importable module — defined in `__main__` it unpickles in the child as
   `AttributeError: module '__main__' has no attribute ...`.

Both are no-ops off Windows: `LOOP_FACTORY` is None, and Docker/Cloud Run use
the stock uvicorn CLI (see the Dockerfile). This module is dev-only.
"""

import asyncio
import sys

import uvicorn

APP = "compendium.main:app"

LOOP_FACTORY = asyncio.SelectorEventLoop if sys.platform == "win32" else None


class SelectorServer(uvicorn.Server):
    """uvicorn.Server that builds its own event loop.

    `run` is the method uvicorn's reloader targets in the child process, so
    overriding it is what carries the loop choice across the process boundary.
    """

    def run(self, sockets=None):
        asyncio.run(self.serve(sockets=sockets), loop_factory=LOOP_FACTORY)


def serve(host: str = "127.0.0.1", port: int = 8000) -> int:
    """Run the server once, in the foreground. Reload is handled by `watch()`."""
    server = SelectorServer(uvicorn.Config(APP, host=host, port=port))
    server.run()
    return 0


def watch(host: str = "127.0.0.1", port: int = 8000) -> int:
    """Restart `serve()` whenever src/ changes.

    Uvicorn's own `--reload` supervisor is deliberately not used. It spawns the
    child via multiprocessing and pickles the target, which on Windows either
    fails outright or — as seen here — logs "Reloading..." and then silently
    goes on serving the old code, which is worse than no reload at all.

    watchfiles' own `run_process` command mode isn't used either: it puts the
    command through `shlex.split`, which eats the backslashes in a Windows
    interpreter path and dies with WinError 2.

    So: watch, and re-exec an argv list. Nothing is pickled, nothing is parsed.
    The cost is a brief unbind/rebind of the port per restart, fine for dev.
    """
    import subprocess

    from watchfiles import watch as watch_files

    argv = [sys.executable, "-m", "compendium", "--host", host, "--port", str(port)]

    def spawn() -> subprocess.Popen:
        return subprocess.Popen(argv)

    process = spawn()
    try:
        for changes in watch_files("src"):
            names = ", ".join(sorted(p for _, p in changes)[:3])
            print(f"\n--- reloading ({names}) ---", flush=True)
            process.terminate()
            process.wait()
            process = spawn()
    except KeyboardInterrupt:
        pass
    finally:
        process.terminate()
        process.wait()
    return 0
