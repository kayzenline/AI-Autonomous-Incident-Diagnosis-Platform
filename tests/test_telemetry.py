import asyncio

import pytest
from app.core import telemetry
from app.eval.runner import build_m4_report_async
from app.eval.scenario import load_frozen_scenarios
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode


def test_investigation_and_tool_spans_share_trace_without_contents(monkeypatch) -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry.trace, "get_tracer", provider.get_tracer)
    try:
        asyncio.run(build_m4_report_async(load_frozen_scenarios()[:1]))
        spans = exporter.get_finished_spans()
        root = next(span for span in spans if span.name == "agentops.investigate")
        tools = [span for span in spans if span.name.startswith("agentops.tool.")]
        assert tools
        assert all(span.context.trace_id == root.context.trace_id for span in tools)
        assert all(span.parent.span_id == root.context.span_id for span in tools)
        assert root.attributes["agentops.status"] == "COMPLETED"
        assert all(key.startswith("agentops.") for span in spans for key in span.attributes)
        assert not any(span.events for span in spans)
    finally:
        provider.shutdown()


def test_error_span_records_type_without_exception_message(monkeypatch) -> None:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry.trace, "get_tracer", provider.get_tracer)

    @telemetry.traced("failure")
    async def fail():
        raise ValueError("secret response")

    try:
        with pytest.raises(ValueError):
            asyncio.run(fail())
        span = exporter.get_finished_spans()[0]
        assert span.status.status_code == StatusCode.ERROR
        assert span.attributes == {"error.type": "ValueError"}
        assert not span.events
        assert span.status.description is None
    finally:
        provider.shutdown()


def test_telemetry_does_not_configure_export_without_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(telemetry, "_provider", None)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    telemetry.configure_telemetry()
    assert telemetry._provider is None
