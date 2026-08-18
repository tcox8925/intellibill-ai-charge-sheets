import unittest

from mark_resolver import apply_mark_review, _prepare_regions
from locked_template import LockedTemplate
from template_registry import load_manifest_and_catalog


class ConstrainedMarkResolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, catalog, reference = load_manifest_and_catalog()
        cls.template = LockedTemplate(catalog=catalog, reference_path=str(reference))

    def _mark(self, codes, *, kind="procedure", reason="adjacent_code_overlap"):
        return {
            "type": "ambiguous_circle",
            "reason": reason,
            "kind": kind,
            "candidate_codes": [
                {"code": code, "score": 0.60 - i * 0.03, "source": "residual_geometry"}
                for i, code in enumerate(codes)
            ],
            "review_region": {"x1": 10, "y1": 10, "x2": 100, "y2": 100},
        }

    def test_high_confidence_single_circle_promotes_only_existing_candidate(self):
        mark = self._mark(["99395", "99215"])
        review = {"status": "ok", "regions": [{
            "id": "R1", "mark_index": 0, "classification": "circle_single",
            "selected_labels": ["A"], "confidence": 0.96, "reason": "one clear loop",
            "labels": {
                "A": {"code": "99395", "score": 0.6691, "source": "residual_geometry", "kind": "procedure"},
                "B": {"code": "99215", "score": 0.6274, "source": "residual_geometry", "kind": "procedure"},
            },
        }]}
        p, d, possible, suppressed, summary = apply_mark_review([], [], [mark], review, self.template)
        self.assertEqual([x["code"] for x in p], ["99395"])
        self.assertEqual(d, [])
        self.assertEqual(possible, [])
        self.assertEqual(suppressed, [])
        self.assertEqual(summary["promoted_codes"], ["99395"])

    def test_ambiguous_keeps_both_candidates_visible(self):
        mark = self._mark(["99395", "99215"])
        review = {"status": "ok", "regions": [{
            "id": "R1", "mark_index": 0, "classification": "ambiguous",
            "selected_labels": ["A", "B"], "confidence": 0.88, "reason": "large loop crosses rows",
            "labels": {
                "A": {"code": "99395", "score": 0.6691, "source": "residual_geometry", "kind": "procedure"},
                "B": {"code": "99215", "score": 0.6274, "source": "residual_geometry", "kind": "procedure"},
            },
        }]}
        p, d, possible, suppressed, _ = apply_mark_review([], [], [mark], review, self.template)
        self.assertEqual(p, [])
        self.assertEqual(d, [])
        self.assertEqual(suppressed, [])
        self.assertEqual(len(possible), 1)
        self.assertEqual({x["code"] for x in possible[0]["display_candidate_codes"]}, {"99395", "99215"})

    def test_scribble_stays_possible_not_confirmed(self):
        mark = self._mark(["81003", "81000", "80305"])
        review = {"status": "ok", "regions": [{
            "id": "R1", "mark_index": 0, "classification": "scribble",
            "selected_labels": ["A", "B"], "confidence": 0.97, "reason": "irregular overwriting",
            "labels": {
                "A": {"code": "81003", "score": 0.79, "source": "residual_geometry", "kind": "procedure"},
                "B": {"code": "81000", "score": 0.75, "source": "residual_geometry", "kind": "procedure"},
                "C": {"code": "80305", "score": 0.45, "source": "residual_geometry", "kind": "procedure"},
            },
        }]}
        p, d, possible, suppressed, _ = apply_mark_review([], [], [mark], review, self.template)
        self.assertEqual(p, [])
        self.assertEqual(d, [])
        self.assertEqual(suppressed, [])
        self.assertEqual(possible[0]["reason"], "scribbled_region")
        self.assertEqual([x["code"] for x in possible[0]["display_candidate_codes"]], ["81003", "81000"])


    def test_weak_single_candidate_cannot_be_promoted_by_ai(self):
        mark = self._mark(["M25.729"], kind="diagnosis", reason="partial_circle_low_confidence")
        mark["candidate_codes"][0]["score"] = 0.3412
        review = {"status": "ok", "regions": [{
            "id": "R1", "mark_index": 0, "classification": "circle_single",
            "selected_labels": ["A"], "confidence": 0.99, "reason": "model thinks it sees a circle",
            "labels": {
                "A": {"code": "M25.729", "score": 0.3412, "source": "residual_geometry", "kind": "diagnosis"},
            },
        }]}
        p, d, possible, suppressed, summary = apply_mark_review([], [], [mark], review, self.template)
        self.assertEqual(p, [])
        self.assertEqual(d, [])
        self.assertEqual(len(possible), 1)
        self.assertEqual(possible[0]["candidate_codes"][0]["code"], "M25.729")
        self.assertFalse(possible[0]["ai_review"]["promotion_geometry_eligible"])
        self.assertEqual(summary["promoted_count"], 0)
        self.assertEqual(suppressed, [])

    def test_prepare_regions_skips_weak_geometry(self):
        import numpy as np
        image = np.full((1700, 2100, 3), 255, np.uint8)
        weak = self._mark(["M25.729"], kind="diagnosis", reason="partial_circle_low_confidence")
        weak["candidate_codes"][0]["score"] = 0.3412
        strong = self._mark(["99395", "99215"])
        strong["candidate_codes"][0]["score"] = 0.667
        strong["candidate_codes"][1]["score"] = 0.5635
        collage, regions = _prepare_regions(image, [weak, strong], self.template)
        self.assertIsNotNone(collage)
        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0]["mark_index"], 1)
        self.assertEqual(regions[0]["labels"]["A"]["code"], "99395")

    def test_model_cannot_promote_code_not_in_candidate_label_map(self):
        mark = self._mark(["99395", "99215"])
        review = {"status": "ok", "regions": [{
            "id": "R1", "mark_index": 0, "classification": "circle_single",
            "selected_labels": ["Z"], "confidence": 0.99, "reason": "bad model output",
            "labels": {
                "A": {"code": "99395", "score": 0.66, "source": "residual_geometry", "kind": "procedure"},
                "B": {"code": "99215", "score": 0.62, "source": "residual_geometry", "kind": "procedure"},
            },
        }]}
        p, d, possible, suppressed, summary = apply_mark_review([], [], [mark], review, self.template)
        self.assertEqual(p, [])
        self.assertEqual(d, [])
        self.assertEqual(len(possible), 1)
        self.assertEqual(summary["promoted_count"], 0)

    def test_ai_none_cannot_hide_strong_geometry(self):
        mark = self._mark(["99395", "99215"])
        mark["candidate_codes"][0]["score"] = 0.66
        mark["candidate_codes"][1]["score"] = 0.52
        review = {"status": "ok", "regions": [{
            "id": "R1", "mark_index": 0, "classification": "none",
            "selected_labels": [], "confidence": 0.99, "reason": "no mark",
            "labels": {
                "A": {"code": "99395", "score": 0.66, "source": "residual_geometry", "kind": "procedure"},
                "B": {"code": "99215", "score": 0.52, "source": "residual_geometry", "kind": "procedure"},
            },
        }]}
        p, d, possible, suppressed, _ = apply_mark_review([], [], [mark], review, self.template)
        self.assertEqual(p, [])
        self.assertEqual(d, [])
        self.assertEqual(len(possible), 1)
        self.assertEqual(suppressed, [])

    def test_ai_none_can_suppress_only_weak_geometry_but_preserves_audit(self):
        mark = self._mark(["G0477", "87880"])
        mark["candidate_codes"][0]["score"] = 0.40
        mark["candidate_codes"][1]["score"] = 0.39
        review = {"status": "ok", "regions": [{
            "id": "R1", "mark_index": 0, "classification": "none",
            "selected_labels": [], "confidence": 0.99, "reason": "no deliberate mark",
            "labels": {
                "A": {"code": "G0477", "score": 0.40, "source": "residual_geometry", "kind": "procedure"},
                "B": {"code": "87880", "score": 0.39, "source": "residual_geometry", "kind": "procedure"},
            },
        }]}
        p, d, possible, suppressed, _ = apply_mark_review([], [], [mark], review, self.template)
        self.assertEqual(p, [])
        self.assertEqual(d, [])
        self.assertEqual(possible, [])
        self.assertEqual(len(suppressed), 1)
        self.assertEqual(suppressed[0]["candidate_codes"][0]["code"], "G0477")

    def test_non_office_adjacent_single_circle_stays_manual(self):
        mark = self._mark(["F11.20", "36415"], kind="diagnosis")
        review = {"status": "ok", "regions": [{
            "id": "R1", "mark_index": 0, "classification": "circle_single",
            "selected_labels": ["B"], "confidence": 0.99, "reason": "single loop",
            "labels": {
                "A": {"code": "F11.20", "score": 0.5867, "source": "residual_geometry", "kind": "diagnosis"},
                "B": {"code": "36415", "score": 0.5335, "source": "residual_geometry", "kind": "procedure"},
            },
        }]}
        p_out, d_out, possible, suppressed, summary = apply_mark_review([], [], [mark], review, self.template)
        self.assertEqual(p_out, [])
        self.assertEqual(d_out, [])
        self.assertEqual(len(possible), 1)
        self.assertTrue(possible[0].get("manual_review"))
        self.assertEqual(summary["promoted_count"], 0)
        self.assertEqual(suppressed, [])

    def test_office_adjacent_single_circle_can_still_promote(self):
        mark = self._mark(["99395", "99215"], kind="procedure")
        review = {"status": "ok", "regions": [{
            "id": "R1", "mark_index": 0, "classification": "circle_single",
            "selected_labels": ["A"], "confidence": 0.96, "reason": "one clear office loop",
            "labels": {
                "A": {"code": "99395", "score": 0.6691, "source": "residual_geometry", "kind": "procedure"},
                "B": {"code": "99215", "score": 0.6274, "source": "residual_geometry", "kind": "procedure"},
            },
        }]}
        p_out, d_out, possible, suppressed, summary = apply_mark_review([], [], [mark], review, self.template)
        self.assertEqual([x["code"] for x in p_out], ["99395"])
        self.assertEqual(d_out, [])
        self.assertEqual(possible, [])
        self.assertEqual(summary["promoted_codes"], ["99395"])
        self.assertEqual(suppressed, [])


if __name__ == "__main__":
    unittest.main()
