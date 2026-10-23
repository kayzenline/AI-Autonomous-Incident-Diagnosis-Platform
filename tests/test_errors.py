from app.api.errors import register_exception_handlers
from app.schemas.incident import IncidentCreate
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/echo")
    def echo(body: IncidentCreate) -> dict[str, str]:
        return {"title": body.title}

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("database password")

    return TestClient(app, raise_server_exceptions=False)


def test_invalid_json_uses_the_validation_error_body() -> None:
    response = _client().post("/echo", json={"title": "", "description": "down"})

    assert response.status_code == 422
    body = response.json()
    assert body["error_code"] == "validation_error"
    assert body["message"] == "Request validation failed."
    assert body["details"]


def test_unexpected_error_hides_the_exception_text() -> None:
    response = _client().get("/boom")

    assert response.status_code == 500
    assert response.json() == {
        "error_code": "internal_error",
        "message": "Internal server error.",
        "details": [],
    }
    assert "database password" not in response.text
