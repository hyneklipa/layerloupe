from fastapi.testclient import TestClient

from layerloupe import __version__
from layerloupe.main import app

client = TestClient(app)


def test_version_is_set() -> None:
    assert __version__
    assert isinstance(__version__, str)


def test_healthz_returns_ok() -> None:
    response = client.get("/api/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_openapi_schema_available() -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "LayerLoupe"
