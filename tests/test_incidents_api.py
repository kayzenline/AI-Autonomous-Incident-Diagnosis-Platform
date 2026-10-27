import pytest
from app.main import app
from fastapi.testclient import TestClient

HEADERS = {"X-API-Key": "dev-key"}
INCIDENT = {
    "title": "Checkout latency high",
    "description": "Shoppers time out while paying.",
    "service": "checkout",
    "occurred_at": "2026-10-23T03:00:00Z",
    "detected_at": "2026-10-23T03:10:00Z",
    "user_severity": "high",
}


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


def test_incident_keeps_user_severity_off_the_diagnosis(client: TestClient) -> None:
    response = client.post("/v1/incidents", json=INCIDENT, headers=HEADERS)

    assert response.status_code == 201
    body = response.json()
    assert body["user_severity"] == "high"
    assert body["status"] == "OPEN"
    assert "severity" not in body
    assert body["archived_at"] is None


def test_incidents_require_the_api_key(client: TestClient) -> None:
    response = client.post("/v1/incidents", json=INCIDENT)

    assert response.status_code == 401
    assert response.json()["error_code"] == "unauthorized"


def test_list_hides_archived_incidents(client: TestClient) -> None:
    first = client.post("/v1/incidents", json=INCIDENT, headers=HEADERS).json()
    second = client.post("/v1/incidents", json=INCIDENT, headers=HEADERS).json()
    deleted = client.delete(f"/v1/incidents/{first['id']}", headers=HEADERS)

    assert deleted.status_code == 204
    listed = client.get("/v1/incidents", headers=HEADERS)
    assert listed.status_code == 200
    ids = {item["id"] for item in listed.json()["items"]}
    assert first["id"] not in ids
    assert second["id"] in ids
    hidden = client.get(f"/v1/incidents/{first['id']}", headers=HEADERS)
    assert hidden.status_code == 200
    assert hidden.json()["archived_at"] is not None


def test_only_a_person_can_resolve_an_incident(client: TestClient) -> None:
    created = client.post("/v1/incidents", json=INCIDENT, headers=HEADERS).json()

    rejected = client.patch(
        f"/v1/incidents/{created['id']}",
        json={"status": "DIAGNOSED"},
        headers=HEADERS,
    )
    resolved = client.patch(
        f"/v1/incidents/{created['id']}",
        json={"status": "RESOLVED"},
        headers=HEADERS,
    )

    assert rejected.status_code == 422
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "RESOLVED"
