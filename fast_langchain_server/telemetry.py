"""
OpenTelemetry setup for langchain-agent-server.

Provides:
- Process-global SDK initialization (traces + metrics + logs via OTLP/gRPC)
- W3C Trace Context + Baggage propagation
- Lazy delegation metrics (counters + histograms)
- Helper to extract/inject trace context from/into HTTP headers

All configuration is driven by standard OTEL_* env vars:
    OTEL_SERVICE_NAME              service name (required to activate)
    OTEL_EXPORTER_OTLP_ENDPOINT   collector gRPC endpoint (required)
    OTEL_SDK_DISABLED              set to "true" to fully opt-out
    OTEL_INCLUDE_HTTP_SERVER       set to "true" to instrument FastAPI
    OTEL_INCLUDE_HTTP_CLIENT       set to "true" to instrument httpx
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

from opentelemetry import _logs as otel_logs
from opentelemetry import metrics, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import extract, inject, set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME as OTEL_SERVICE_KEY
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level service name — computed once, used for tracers/meters
# ---------------------------------------------------------------------------

SERVICE_NAME: str = (
    f"fls.{os.getenv('OTEL_SERVICE_NAME', os.getenv('AGENT_NAME', 'fls-agent'))}"
)

# ---------------------------------------------------------------------------
# Process-global state
# ---------------------------------------------------------------------------

_initialized: bool = False
_delegation_counter: Optional[metrics.Counter] = None
_delegation_duration: Optional[metrics.Histogram] = None


# ---------------------------------------------------------------------------
# Custom log handler
# ---------------------------------------------------------------------------


class FLSLoggingHandler(LoggingHandler):
    """Extends the standard OTLP handler to attach logger.name as an attribute,
    making individual loggers distinguishable in collectors like SigNoz/Jaeger."""

    def emit(self, record: logging.LogRecord) -> None:
        if not hasattr(record, "logger_name"):
            record.logger_name = record.name
        super().emit(record)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def is_otel_enabled() -> bool:
    """True only after ``init_otel()`` has been successfully called."""
    return _initialized


def should_enable_otel() -> bool:
    """Pre-init check — True if the required env vars are present and SDK not disabled."""
    if os.getenv("OTEL_SDK_DISABLED", "false").lower() in ("true", "1", "yes"):
        return False
    return bool(
        os.getenv("OTEL_SERVICE_NAME") and os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    )


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def init_otel(service_name: Optional[str] = None) -> bool:
    """Initialise the OpenTelemetry SDK.  Idempotent — safe to call multiple times.

    Returns ``True`` if initialisation succeeded, ``False`` if OTel is disabled
    or already initialised.

    The SDK reads endpoint / TLS / header config from the standard
    ``OTEL_EXPORTER_OTLP_*`` env vars, so no explicit configuration is needed
    beyond the two required variables.
    """
    global _initialized

    if _initialized:
        return False

    if os.getenv("OTEL_SDK_DISABLED", "false").lower() in ("true", "1", "yes"):
        logger.debug("OpenTelemetry disabled via OTEL_SDK_DISABLED")
        return False

    # If caller provided a name but env var is not set, use it as a fallback
    if service_name and not os.getenv("OTEL_SERVICE_NAME"):
        os.environ["OTEL_SERVICE_NAME"] = service_name

    svc = os.getenv("OTEL_SERVICE_NAME", "")
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "")

    if not svc or not endpoint:
        logger.debug(
            "OpenTelemetry not configured: "
            "OTEL_SERVICE_NAME and OTEL_EXPORTER_OTLP_ENDPOINT are required"
        )
        return False

    resource = Resource.create({OTEL_SERVICE_KEY: svc})

    # W3C TraceContext + Baggage propagation (interoperable with all major vendors)
    set_global_textmap(
        CompositePropagator(
            [TraceContextTextMapPropagator(), W3CBaggagePropagator()]
        )
    )

    # ── Traces ────────────────────────────────────────────────────────────────
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter())  # reads OTEL_EXPORTER_OTLP_* vars
    )
    trace.set_tracer_provider(tracer_provider)

    # ── Metrics ───────────────────────────────────────────────────────────────
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
    )
    metrics.set_meter_provider(meter_provider)

    # ── Logs ──────────────────────────────────────────────────────────────────
    log_level_str = os.getenv("AGENT_LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter())
    )
    otel_logs.set_logger_provider(logger_provider)
    otel_handler = FLSLoggingHandler(level=log_level, logger_provider=logger_provider)
    logging.getLogger().addHandler(otel_handler)

    # ── Optional: auto-instrument FastAPI / httpx ─────────────────────────────
    if os.getenv("OTEL_INCLUDE_HTTP_SERVER", "false").lower() in ("true", "1", "yes"):
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            FastAPIInstrumentor().instrument()
            logger.debug("FastAPI OTel instrumentation enabled")
        except ImportError:
            logger.debug("opentelemetry-instrumentation-fastapi not installed, skipping")

    if os.getenv("OTEL_INCLUDE_HTTP_CLIENT", "false").lower() in ("true", "1", "yes"):
        try:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
            HTTPXClientInstrumentor().instrument()
            logger.debug("httpx OTel instrumentation enabled")
        except ImportError:
            logger.debug("opentelemetry-instrumentation-httpx not installed, skipping")

    logger.info("OpenTelemetry initialised (service=%s endpoint=%s)", svc, endpoint)
    _initialized = True
    return True


# ---------------------------------------------------------------------------
# Delegation metrics (lazily initialised)
# ---------------------------------------------------------------------------


def get_delegation_metrics() -> (
    Tuple[Optional[metrics.Counter], Optional[metrics.Histogram]]
):
    """Return (delegation_counter, delegation_duration_histogram).

    Returns ``(None, None)`` when OTel is disabled.
    """
    global _delegation_counter, _delegation_duration

    if not _initialized:
        return None, None

    if _delegation_counter is None:
        meter = metrics.get_meter(SERVICE_NAME)
        _delegation_counter = meter.create_counter(
            "fls.delegations",
            description="Number of agent delegations",
            unit="1",
        )
        _delegation_duration = meter.create_histogram(
            "fls.delegation.duration",
            description="Duration of agent delegations",
            unit="ms",
        )

    return _delegation_counter, _delegation_duration


# ---------------------------------------------------------------------------
# Trace context helpers
# ---------------------------------------------------------------------------


def extract_context(headers: Dict[str, str]):
    """Extract W3C trace context from an HTTP headers dict.

    Use as the ``context`` argument when starting a new span so that
    incoming distributed traces are continued rather than broken.

    Example::

        parent_ctx = extract_context(dict(request.headers))
        with tracer.start_as_current_span("my-span", context=parent_ctx):
            ...
    """
    return extract(headers)


def inject_context(headers: Dict[str, str]) -> None:
    """Inject the current W3C trace context into *headers* (mutates in place).

    Call this before forwarding HTTP requests to downstream agents so that
    the distributed trace is propagated end-to-end.
    """
    inject(headers)


def get_current_trace_context() -> Optional[Dict[str, str]]:
    """Return ``{"trace_id": ..., "span_id": ...}`` for the active span, or ``None``."""
    if not _initialized:
        return None
    span = trace.get_current_span()
    if span is None:
        return None
    ctx = span.get_span_context()
    if not ctx.is_valid:
        return None
    return {
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
    }
