import pytest
from app.schemas.incident import IncidentCreate
from pydantic import ValidationError


def test_create_accepts_fr1_fields() -> None:
    incident = IncidentCreate(
        title="Payment API 502",
        description="Users started receiving 502 errors.",
        service="payment-api",
        occurred_at="2026-10-23T02:50:00Z",
        detected_at="2026-10-23T03:00:00Z",
        user_severity="high",
        metadata={"region": "au"},
    )

    assert incident.service == "payment-api"
    assert incident.user_severity == "high"


def test_create_rejects_an_empty_title() -> None:
    with pytest.raises(ValidationError):
        IncidentCreate(title="", description="The API is down.")


def test_request_model_does_not_carry_metrics() -> None:
    assert "cpu_usage" not in IncidentCreate.model_fields
    assert "memory_usage" not in IncidentCreate.model_fields
    assert "status_code" not in IncidentCreate.model_fields
    assert "severity" not in IncidentCreate.model_fields
