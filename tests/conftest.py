import asyncio
import sys

# psycopg's async driver refuses to run on ProactorEventLoop, the Windows default
# — without a selector loop the integration tests spend 30s each failing to open a
# pool. `compendium.__main__` solves the same problem the same way for the dev
# server.
#
# The hook is defined only on Windows, and deliberately so: it's declared
# `firstresult`, so a registered implementation returning None is a UsageError
# rather than a fall-through to pytest-asyncio's defaults. Leaving it undefined
# elsewhere is what makes Linux (and CI) use the defaults.
if sys.platform == "win32":

    def pytest_asyncio_loop_factories(config, item):
        return {"selector": asyncio.SelectorEventLoop}
