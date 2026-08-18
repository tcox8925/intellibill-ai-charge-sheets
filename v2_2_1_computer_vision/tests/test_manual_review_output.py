import unittest

from run import _manual_review_code_buckets
from locked_template import LockedTemplate
from template_registry import load_manifest_and_catalog


class ManualReviewOutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, catalog, reference = load_manifest_and_catalog()
        cls.template = LockedTemplate(catalog=catalog, reference_path=str(reference))

    def test_ambiguous_office_overlap_surfaces_procedures_when_confirmed_is_empty(self):
        mark = {
            "type": "ambiguous_circle",
            "reason": "adjacent_code_overlap",
            "kind": "procedure",
            "candidate_codes": [
                {"code": "99214", "score": 0.7516, "source": "residual_geometry"},
                {"code": "99205", "score": 0.5419, "source": "residual_geometry"},
            ],
            "ai_review": {
                "classification": "ambiguous",
                "confidence": 0.45,
                "candidate_codes_selected": ["99214", "99205"],
            },
        }
        procs, dxs = _manual_review_code_buckets([mark], self.template, [])
        self.assertEqual([x["code"] for x in procs], ["99214", "99205"])
        self.assertTrue(all(x["status"] == "manual_review" for x in procs))
        self.assertEqual(dxs, [])

    def test_mixed_misc_overlap_splits_proc_and_dx_manual_review(self):
        mark = {
            "type": "ambiguous_circle",
            "reason": "adjacent_code_overlap",
            "kind": "diagnosis",
            "candidate_codes": [
                {"code": "F11.20", "score": 0.5867, "source": "residual_geometry"},
                {"code": "36415", "score": 0.5335, "source": "residual_geometry"},
            ],
            "ai_review": {
                "classification": "circle_multiple",
                "confidence": 0.82,
                "candidate_codes_selected": ["F11.20", "36415"],
            },
        }
        procs, dxs = _manual_review_code_buckets([mark], self.template, [{"code": "99395"}])
        self.assertEqual([x["code"] for x in procs], ["36415"])
        self.assertEqual([x["code"] for x in dxs], ["F11.20"])

    def test_zero_proc_fallback_prefers_office_service_candidate_group(self):
        marks = [
            {
                "type": "possible_circle", "reason": "partial_circle_low_confidence", "kind": "procedure",
                "candidate_codes": [{"code": "96361", "score": 0.50, "source": "residual_geometry"}],
            },
            {
                "type": "ambiguous_circle", "reason": "adjacent_code_overlap", "kind": "procedure",
                "candidate_codes": [
                    {"code": "99214", "score": 0.40, "source": "residual_geometry"},
                    {"code": "99205", "score": 0.39, "source": "residual_geometry"},
                ],
                "ai_review": {"status": "not_reviewed", "reason": "geometry_below_ai_review_gate"},
            },
        ]
        procs, _ = _manual_review_code_buckets(marks, self.template, [])
        self.assertEqual([x["code"] for x in procs], ["99214", "99205"])
        self.assertTrue(all(x["reason"] == "procedure_required_manual_review" for x in procs))


if __name__ == "__main__":
    unittest.main()
