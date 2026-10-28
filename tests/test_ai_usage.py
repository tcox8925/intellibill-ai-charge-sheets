"""ai_usage.py against a real Anthropic client whose HTTP calls are faked, and a
local server standing in for intellibill's POST /api/ai-usage/events.

Run with: .venv/bin/python -m unittest tests.test_ai_usage
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import anthropic
import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ai_usage  # noqa: E402

CLAUDE_REPLY = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-4-6",
    "content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 1200, "output_tokens": 80,
              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
}


def claude_client(status=200):
    def reply(_request):
        if status != 200:
            return httpx.Response(status, json={"type": "error", "error": {"type": "api_error", "message": "overloaded"}})
        return httpx.Response(200, json=CLAUDE_REPLY)
    return anthropic.AnthropicFoundry(
        api_key="test", base_url="https://foundry.invalid/anthropic/", max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(reply)))


def ask(client):
    return client.messages.create(model="claude-opus-4-6", max_tokens=50,
                                  messages=[{"role": "user", "content": "hi"}])


class IntellibillStub(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["content-length"]))
        IntellibillStub.received.append({"key": self.headers.get("x-ai-usage-key"), "body": json.loads(body)})
        self.send_response(201)
        self.end_headers()
        self.wfile.write(b'{"recorded": 1}')

    def log_message(self, *args):
        pass


class AiUsageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), IntellibillStub)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/api/ai-usage/events"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        IntellibillStub.received.clear()
        self.addCleanup(setattr, ai_usage, "INGEST_URL", ai_usage.INGEST_URL)
        self.addCleanup(setattr, ai_usage, "INGEST_API_KEY", ai_usage.INGEST_API_KEY)
        ai_usage.INGEST_URL = self.url
        ai_usage.INGEST_API_KEY = "secret"

    def test_a_document_posts_its_calls_once_when_it_finishes(self):
        client = ai_usage.record_usage_of(claude_client())
        usage = ai_usage.DocumentUsage(123)
        usage.client_id = 7
        self.assertEqual(ask(client).content[0].text, "{}")
        ask(client)
        self.assertEqual(IntellibillStub.received, [])
        usage.report()

        self.assertEqual(len(IntellibillStub.received), 1)
        post = IntellibillStub.received[0]
        self.assertEqual(post["key"], "secret")
        self.assertEqual(post["body"]["source"], "chargesheet_ingest")
        events = post["body"]["events"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["process"], "chargesheet_extraction")
        self.assertEqual(events[0]["provider"], "anthropic")
        self.assertEqual(events[0]["model"], "claude-opus-4-6")
        self.assertEqual((events[0]["inputTokens"], events[0]["outputTokens"]), (1200, 80))
        self.assertEqual((events[0]["referenceId"], events[0]["clientId"]), ("123", 7))

    def test_a_failed_call_is_reported_and_still_raises(self):
        client = ai_usage.record_usage_of(claude_client(status=529))
        usage = ai_usage.DocumentUsage(5)
        with self.assertRaises(anthropic.APIStatusError):
            ask(client)
        usage.report()
        event = IntellibillStub.received[0]["body"]["events"][0]
        self.assertEqual(event["status"], "failed")
        self.assertEqual(event["model"], "claude-opus-4-6")
        self.assertNotIn("clientId", event)

    def test_a_call_outside_a_document_is_posted_on_its_own(self):
        ask(ai_usage.record_usage_of(claude_client()))
        self.assertEqual(len(IntellibillStub.received), 1)
        self.assertNotIn("referenceId", IntellibillStub.received[0]["body"]["events"][0])

    def test_wrapping_twice_reports_each_call_once(self):
        client = ai_usage.record_usage_of(ai_usage.record_usage_of(claude_client()))
        ask(client)
        self.assertEqual(len(IntellibillStub.received[0]["body"]["events"]), 1)

    def test_nothing_is_sent_until_both_settings_are_set(self):
        ai_usage.INGEST_API_KEY = ""
        ask(ai_usage.record_usage_of(claude_client()))
        self.assertEqual(IntellibillStub.received, [])

    def test_intellibill_being_down_never_fails_the_extraction(self):
        ai_usage.INGEST_URL = "http://127.0.0.1:9/api/ai-usage/events"
        client = ai_usage.record_usage_of(claude_client())
        usage = ai_usage.DocumentUsage(9)
        ask(client)
        usage.report()


if __name__ == "__main__":
    unittest.main()
