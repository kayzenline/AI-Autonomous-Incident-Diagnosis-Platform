from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_health_reports_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


def test_openapi_docs_are_available() -> None:
    response = client.get("/docs")

    assert response.status_code == 200


def test_unknown_path_uses_the_error_body() -> None:
    response = client.get("/missing")

    assert response.status_code == 404
    assert response.json() == {
        "error_code": "not_found",
        "message": "Not Found",
        "details": [],
    }
