"""
Reports this service's Claude usage to intellibill, for its Analytics >
AI Insights dashboard (process "chargesheet_extraction").

  - record_usage_of(client) wraps the Anthropic client once, where it is
    created, so every messages.create through it is noted - tokens, model,
    duration, failures - without touching the call sites.
  - DocumentUsage(document_id) collects the calls made while one document is
    processed and report() posts them to intellibill in one request:

        usage = ai_usage.DocumentUsage(document_id)
        try:
            ...extract...
        finally:
            usage.report()

    A call made outside any document is posted on its own.

Posts to POST {AI_USAGE_INGEST_URL} with the x-ai-usage-key header. Off (a
no-op) until AI_USAGE_INGEST_URL and AI_USAGE_INGEST_API_KEY are both set.
Never raises: reporting usage must not fail an extraction.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
from typing import Optional
from urllib import request

logger = logging.getLogger("chargesheet.ai_usage")

INGEST_URL = os.environ.get("AI_USAGE_INGEST_URL", "").strip()
INGEST_API_KEY = os.environ.get("AI_USAGE_INGEST_API_KEY", "").strip()

PROCESS = "chargesheet_extraction"
SOURCE = "chargesheet_ingest"
# intellibill accepts at most this many events per request.
MAX_EVENTS_PER_REQUEST = 500
POST_TIMEOUT_SECONDS = 10

# The calls noted for the document being processed in this context.
_current_events: contextvars.ContextVar[Optional[list]] = contextvars.ContextVar(
    "ai_usage_events", default=None)


def enabled() -> bool:
    return bool(INGEST_URL and INGEST_API_KEY)


def record_usage_of(client):
    """Wraps client.messages.create so every call is noted. Returns the client;
    wrapping the same client twice notes each call once."""
    messages = client.messages
    if getattr(messages, "_ai_usage_recorded", False):
        return client
    create = messages.create

    def create_and_record(*args, **kwargs):
        started = time.monotonic()
        try:
            message = create(*args, **kwargs)
        except Exception as exc:
            _note({
                "model": kwargs.get("model"),
                "durationMs": _elapsed_ms(started),
                "status": "failed",
                "errorMessage": str(exc)[:2000],
            })
            raise
        # A streamed response carries its usage in the stream events - not used here.
        if not kwargs.get("stream"):
            _note(_message_event(message, kwargs.get("model"), started))
        return message

    messages.create = create_and_record
    messages._ai_usage_recorded = True
    return client


class DocumentUsage:
    """Collects the Claude calls made while one document is processed."""

    def __init__(self, document_id, client_id: Optional[int] = None):
        self.reference_id = str(document_id)
        self.client_id = client_id
        self._events: list = []
        self._token = _current_events.set(self._events)

    def report(self):
        """Stops collecting and posts what was collected."""
        _current_events.reset(self._token)
        for event in self._events:
            event["referenceId"] = self.reference_id
            if self.client_id is not None:
                event["clientId"] = self.client_id
        _post(self._events)


def _message_event(message, requested_model, started) -> dict:
    usage = getattr(message, "usage", None)
    return {
        # The response names the model that ran; the request only what was asked for.
        "model": getattr(message, "model", None) or requested_model,
        "inputTokens": getattr(usage, "input_tokens", None) or 0,
        "outputTokens": getattr(usage, "output_tokens", None) or 0,
        "durationMs": _elapsed_ms(started),
        "status": "success",
        "metadata": {
            # Prompt caching isn't used today; kept so cached calls are visible if it is.
            "cacheCreationInputTokens": getattr(usage, "cache_creation_input_tokens", None) or 0,
            "cacheReadInputTokens": getattr(usage, "cache_read_input_tokens", None) or 0,
            "stopReason": getattr(message, "stop_reason", None),
        },
    }


def _note(event: dict):
    try:
        event.update({"process": PROCESS, "provider": "anthropic", "operation": "chat_completion"})
        events = _current_events.get()
        if events is not None:
            events.append(event)
        else:
            _post([event])
    except Exception as exc:
        logger.warning("Could not note AI usage (ignored): %s", exc)


def _post(events: list):
    if not events or not enabled():
        return
    for start in range(0, len(events), MAX_EVENTS_PER_REQUEST):
        batch = events[start:start + MAX_EVENTS_PER_REQUEST]
        try:
            req = request.Request(
                INGEST_URL,
                data=json.dumps({"source": SOURCE, "events": batch}).encode("utf-8"),
                headers={"content-type": "application/json", "x-ai-usage-key": INGEST_API_KEY},
                method="POST",
            )
            with request.urlopen(req, timeout=POST_TIMEOUT_SECONDS) as resp:
                resp.read()
        except Exception as exc:
            logger.warning("Could not report %d AI usage event(s) to intellibill (ignored): %s",
                           len(batch), exc)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
