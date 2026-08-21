import json
import os
import sys
import unittest
from dataclasses import dataclass

import numpy as np

ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mark_adjudicator import adjudicate, reconcile, MIN_PHYSICAL_COVERAGE
from mark_localizer import Mark as LocalizedMark, _candidate_rows_for_mark, DEFAULTS
from run_v3 import _populate_code_outputs


@dataclass
class Mark:
    id: int
    bbox: tuple
    stroke_px: int
    max_clearance: float
    candidates: list
    hint: list


def c(code, desc=None, kind="procedure", bbox=None):
    return {"code": code, "description": desc or code,
            "section": "Office Services", "kind": kind,
            "bbox": bbox or [0, 0, 10, 10]}


def member(code, coverage, desc=None, kind="procedure", idx=0):
    return {**c(code, desc, kind), "candidate_index": idx,
            "physical_coverage": coverage}


class PhysicalCoveragePolicyTests(unittest.TestCase):
    def test_slight_adjacent_touch_is_not_confirmed(self):
        mark = Mark(1, (0, 0, 10, 10), 100, 4.0,
                    [c("99205"), c("99214"), c("99215")], [])
        d = {"status": "ok", "circle_marks": [{
            "members": [member("99205", 0.10, idx=0),
                        member("99214", 0.94, idx=1),
                        member("99215", 0.22, idx=2)],
            "completeness": "complete", "dominant_index": None,
            "confidence": 0.95, "why": "circle centered on middle row"
        }], "other_marks": [], "invalid_indexes_rejected": []}
        r = reconcile(mark, d, 0.75, 0.12, 0.50)
        self.assertEqual([x["code"] for x in r["confirmed"]], ["99214"])

    def test_two_rows_over_half_are_both_confirmed(self):
        mark = Mark(2, (0, 0, 10, 10), 100, 4.0,
                    [c("99215"), c("99395")], [])
        d = {"status": "ok", "circle_marks": [{
            "members": [member("99215", 0.64, idx=0),
                        member("99395", 0.78, idx=1)],
            "completeness": "complete", "dominant_index": None,
            "confidence": 0.91, "why": "one large loop materially encloses both"
        }], "other_marks": [], "invalid_indexes_rejected": []}
        r = reconcile(mark, d, 0.75, 0.12, 0.50)
        self.assertEqual({x["code"] for x in r["confirmed"]}, {"99215", "99395"})

    def test_exact_half_counts_as_material_overlap(self):
        mark = Mark(3, (0, 0, 10, 10), 100, 4.0, [c("A"), c("B")], [])
        d = {"status": "ok", "circle_marks": [{
            "members": [member("A", 0.90, idx=0), member("B", 0.50, idx=1)],
            "completeness": "complete", "dominant_index": None,
            "confidence": 0.9, "why": "material overlap"
        }], "other_marks": [], "invalid_indexes_rejected": []}
        r = reconcile(mark, d, 0.75, 0.12, 0.50)
        self.assertEqual({x["code"] for x in r["confirmed"]}, {"A", "B"})

    def test_incomplete_dominant_row_survives_below_half(self):
        mark = Mark(4, (0, 0, 10, 10), 100, 4.0,
                    [c("99214"), c("99215")], [])
        d = {"status": "ok", "circle_marks": [{
            "members": [member("99214", 0.45, idx=0), member("99215", 0.18, idx=1)],
            "completeness": "incomplete", "dominant_index": 0,
            "confidence": 0.83, "why": "real broken arc clearly centered on first row"
        }], "other_marks": [], "invalid_indexes_rejected": []}
        r = reconcile(mark, d, 0.75, 0.12, 0.50)
        self.assertEqual([x["code"] for x in r["confirmed"]], ["99214"])

    def test_incomplete_adjacent_over_half_is_also_kept(self):
        mark = Mark(5, (0, 0, 10, 10), 100, 4.0,
                    [c("99214"), c("99215")], [])
        d = {"status": "ok", "circle_marks": [{
            "members": [member("99214", 0.82, idx=0), member("99215", 0.61, idx=1)],
            "completeness": "incomplete", "dominant_index": 0,
            "confidence": 0.84, "why": "real arc materially spans both"
        }], "other_marks": [], "invalid_indexes_rejected": []}
        r = reconcile(mark, d, 0.75, 0.12, 0.50)
        self.assertEqual({x["code"] for x in r["confirmed"]}, {"99214", "99215"})

    def test_geometry_cannot_override_visual_selection(self):
        mark = Mark(6, (0, 0, 10, 10), 100, 4.0,
                    [c("99205"), c("99214")],
                    [{"code": "99205", "score": 0.99},
                     {"code": "99214", "score": 0.20}])
        d = {"status": "ok", "circle_marks": [{
            "members": [member("99214", 0.90, idx=1)],
            "completeness": "complete", "dominant_index": None,
            "confidence": 0.95, "why": "clear loop"
        }], "other_marks": [], "invalid_indexes_rejected": []}
        r = reconcile(mark, d, 0.75, 0.12, 0.50)
        self.assertEqual([x["code"] for x in r["confirmed"]], ["99214"])
        self.assertFalse(r["confirmed"][0]["geometry_hint_agrees"])

    def test_no_physical_circle_is_rejected_not_invented(self):
        mark = Mark(7, (0, 0, 10, 10), 100, 4.0,
                    [c("95004X80")], [{"code": "95004X80", "score": 0.99}])
        d = {"status": "ok", "circle_marks": [],
             "other_marks": [], "invalid_indexes_rejected": []}
        r = reconcile(mark, d, 0.75, 0.12, 0.50)
        self.assertTrue(r["no_selection"])
        self.assertEqual(r["confirmed"], [])


class CandidateRecallTests(unittest.TestCase):
    def test_dense_candidate_set_does_not_drop_late_j_code(self):
        # Reproduces the v3.3 failure shape: interleaved procedure/diagnosis rows
        # with J3301 after the old 8-row truncation point.
        cells = []
        glyphs = {}
        codes = ["J1100", "I73.9", "J3420", "I10", "J0696", "I50.9",
                 "J1885", "I25.10", "J3301", "R06.00"]
        for i, code in enumerate(codes):
            row = i // 2
            x1 = 10 if i % 2 == 0 else 430
            box = [x1, 100 + row * 30, x1 + 100, 128 + row * 30]
            cells.append(c(code, bbox=box))
            glyphs[code] = box
        mask = np.zeros((400, 700), np.uint8)
        # Strong physical stroke around the late J3301 row.
        mask[205:235, 0:150] = 1
        m = LocalizedMark(id=1, bbox=[0, 95, 440, 245], crop_bbox=[0, 60, 650, 280],
                          stroke_px=int(mask.sum()), max_clearance=5.0,
                          band="table", mask=mask)
        p = dict(DEFAULTS)
        chosen = _candidate_rows_for_mark(m, cells, glyphs, p)
        self.assertIn("J3301", [x["code"] for x in chosen])
        self.assertGreaterEqual(int(p["max_candidates"]), 16)


class FakeBlock:
    type = "text"
    def __init__(self, text): self.text = text


class FakeMessages:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
    def create(self, **kwargs):
        text = self.replies[self.calls]
        self.calls += 1
        return type("Resp", (), {"content": [FakeBlock(text)]})()


class FakeClient:
    def __init__(self, replies): self.messages = FakeMessages(replies)


class ReaderRobustnessTests(unittest.TestCase):
    def test_unparseable_response_is_retried_once(self):
        mark = Mark(8, (0, 0, 10, 10), 100, 4.0, [c("J1885")], [])
        client = FakeClient([
            "not json",
            json.dumps({"circle_marks": [{
                "completeness": "complete",
                "rows": [{"index": 0, "physical_coverage": 0.95}],
                "dominant_index": None, "confidence": 0.95,
                "why": "clear physical loop"
            }], "other_marks": []})
        ])
        d = adjudicate(client, "fake-model", mark, b"png")
        self.assertEqual(d["status"], "ok_after_retry")
        self.assertEqual(d["reader_attempts"], 2)
        self.assertEqual(d["circle_marks"][0]["members"][0]["code"], "J1885")


class OutputContractTests(unittest.TestCase):
    def test_procedure_codes_contains_confirmed_only(self):
        page = {}
        confirmed = {
            "99395": {"code": "99395", "confidence": 0.96},
            "R10.9": {"code": "R10.9", "confidence": 0.91},
        }
        review = {
            "99215": {"code": "99215", "status": "manual_review"},
            "I10": {"code": "I10", "status": "manual_review"},
        }
        catalog = {"cells": [
            {"code": "99395", "kind": "procedure"},
            {"code": "99215", "kind": "procedure"},
            {"code": "R10.9", "kind": "diagnosis"},
            {"code": "I10", "kind": "diagnosis"},
        ]}
        _populate_code_outputs(page, confirmed, review, catalog)
        self.assertEqual([x["code"] for x in page["procedure_codes"]], ["99395"])
        self.assertEqual([x["code"] for x in page["manual_review_procedures"]], ["99215"])
        self.assertTrue(all(x["status"] == "confirmed" for x in page["procedure_codes"]))

    def test_model_client_uses_shared_eob_v9_auth_module(self):
        with open(os.path.join(ROOT, "model_client.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('from auth import get_anthropic_client, get_kv_client', src)
        with open(os.path.join(ROOT, "auth.py"), encoding="utf-8") as fh:
            auth = fh.read()
        self.assertIn('DefaultAzureCredential', auth)
        self.assertIn('https://keyvault-834analytics.vault.azure.net/', auth)
        self.assertIn('834-claude-key', auth)
        self.assertIn('https://sql-test-resource.services.ai.azure.com/anthropic/', auth)
        self.assertIn('AnthropicFoundry', auth)


if __name__ == "__main__":
    unittest.main()
