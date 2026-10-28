"""
Smoke test for ai_usage.py: one small, real Claude call through the same
client api.py builds (run.make_client() wrapped by ai_usage.record_usage_of),
reported to intellibill as one document.

Writes nothing but the usage row in intellibill - no blob, attachment, page
or claim is read or changed.

intellibill's server/src/scripts/smokeTestAiUsage.ts --process=chargesheet_extraction
runs this against its own /api/ai-usage/events route and checks the row. To
run it on its own against a running intellibill:

    .venv/bin/python smoke_ai_usage.py smoke-test-<anything>

Needs AI_USAGE_INGEST_URL and AI_USAGE_INGEST_API_KEY (from .env or the
environment); without them nothing is reported and it exits 1.
"""

import json
import sys

import ai_usage
import run
from auth import HAIKU_MODEL  # importing auth also loads .env


def main(reference_id: str) -> int:
    if not ai_usage.enabled():
        print("AI usage reporting is off - set AI_USAGE_INGEST_URL and AI_USAGE_INGEST_API_KEY.", file=sys.stderr)
        return 1

    client = ai_usage.record_usage_of(run.make_client())
    usage = ai_usage.DocumentUsage(reference_id)
    try:
        message = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=16,
            messages=[{"role": "user", "content": "Smoke test. Reply with the single word OK."}],
        )
    finally:
        usage.report()

    print(json.dumps({
        "model": message.model,
        "reply": "".join(block.text for block in message.content if block.type == "text"),
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
    }))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: smoke_ai_usage.py <reference-id>", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
