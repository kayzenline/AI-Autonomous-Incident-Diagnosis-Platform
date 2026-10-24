import pytest
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("API_KEY", "dev-key")
    return TestClient(app)


def test_scenarios_hide_ground_truth(client: TestClient) -> None:
    response = client.get("/v1/scenarios", headers={"X-API-Key": "dev-key"})

    assert response.status_code == 200
    body = response.json()
    assert len(body) >= 20
    assert {"id", "title", "service", "tags"} == set(body[0])
    assert "ground_truth" not in response.text
    assert "cpu-saturation-checkout" in {item["id"] for item in body}


def test_scenarios_reject_a_missing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_KEY", raising=False)
    response = TestClient(app).get("/v1/scenarios")

    assert response.status_code == 401
    assert response.json()["error_code"] == "unauthorized"


def test_scenarios_reject_a_wrong_key(client: TestClient) -> None:
    response = client.get("/v1/scenarios", headers={"X-API-Key": "nope"})

    assert response.status_code == 401
    assert response.json() == {
        "error_code": "unauthorized",
        "message": "Unauthorized",
        "details": [],
    }
