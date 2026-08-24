from fastapi.testclient import TestClient

from compendium import __version__, db, main
from compendium.config import settings


def test_liveness_does_not_touch_the_database(monkeypatch):
    """Liveness must stay green when Postgres is down, or a container that is
    actually fine gets restart-looped."""

    async def explode():
        raise AssertionError("liveness must not touch the database")

    monkeypatch.setattr(db, "healthy", explode)
    monkeypatch.setattr(main.db, "open_pool", _noop)
    monkeypatch.setattr(main.db, "close_pool", _noop)

    with TestClient(main.app) as client:
        for path in settings().health_path_list:
            response = client.get(path)

            assert response.status_code == 200, path
            assert response.json() == {"status": "ok", "version": __version__}


def test_liveness_is_served_on_more_than_one_path():
    """A single hardcoded path is the bug this guards: GCP's frontend answers
    `/healthz` before the request reaches the container."""
    assert len(settings().health_path_list) > 1


def test_readyz_reports_503_when_database_is_unreachable(monkeypatch):
    async def unhealthy():
        return False

    monkeypatch.setattr(main.db, "healthy", unhealthy)
    monkeypatch.setattr(main.db, "open_pool", _noop)
    monkeypatch.setattr(main.db, "close_pool", _noop)

    with TestClient(main.app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["database"] is False


async def _noop(*args, **kwargs):
    return None
