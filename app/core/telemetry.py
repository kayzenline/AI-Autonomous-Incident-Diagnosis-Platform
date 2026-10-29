"""Opt-in OTel spans. Never attach prompts, observations, reports, or credentials."""

import os
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import ParamSpec, TypeVar

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

P = ParamSpec("P")
T = TypeVar("T")
_provider: TracerProvider | None = None


def configure_telemetry() -> None:
    global _provider
    if _provider is not None or not (
        os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    ):
        return
    if os.getenv("OTEL_SDK_DISABLED", "").lower() == "true":
        return
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    _provider = TracerProvider(resource=Resource.create({"service.name": "agentops"}))
    _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(_provider)


def flush_telemetry() -> None:
    if _provider is not None:
        _provider.force_flush(timeout_millis=5000)


def traced(name: str) -> Callable[[Callable[P, Awaitable[T]]], Callable[P, Awaitable[T]]]:
    def decorate(function: Callable[P, Awaitable[T]]) -> Callable[P, Awaitable[T]]:
        @wraps(function)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
            tracer = trace.get_tracer("agentops")
            # Exception text can include provider responses; record only its type.
            with tracer.start_as_current_span(
                name, record_exception=False, set_status_on_exception=False
            ) as span:
                try:
                    result = await function(*args, **kwargs)
                except BaseException as exc:
                    span.set_attribute("error.type", type(exc).__name__)
                    span.set_status(trace.StatusCode.ERROR)
                    raise
                for attribute in (
                    "status",
                    "tokens_in",
                    "tokens_out",
                    "tool_calls",
                    "model_rounds",
                ):
                    value = getattr(result, attribute, None)
                    if isinstance(value, str | int | float | bool):
                        span.set_attribute(f"agentops.{attribute}", value)
                return result

        return wrapped

    return decorate
