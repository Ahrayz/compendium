"""Apply pending SQL migrations in filename order.

    python migrations/run.py

Deliberately ~40 lines instead of Alembic: the migrations are hand-written SQL
(that's the point — you want to read the DDL you're reasoning about), and there
are no models to autogenerate from.
"""

import pathlib
import sys

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from compendium.config import settings  # noqa: E402

MIGRATIONS = pathlib.Path(__file__).resolve().parent


def main() -> int:
    files = sorted(p for p in MIGRATIONS.glob("*.sql"))
    if not files:
        print("no migrations found")
        return 0

    with psycopg.connect(settings().database_url, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            conn.commit()
            cur.execute("SELECT version FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}

        for path in files:
            version = path.stem
            if version in applied:
                print(f"skip  {version}")
                continue
            print(f"apply {version}")
            with conn.cursor() as cur:
                cur.execute(path.read_text(encoding="utf-8"))
                cur.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (version,))
            conn.commit()

    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
