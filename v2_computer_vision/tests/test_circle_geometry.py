import unittest

import cv2
import numpy as np

from locked_template import Alignment, LockedTemplate
from template_registry import load_manifest_and_catalog


class PartialCircleGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, catalog, reference = load_manifest_and_catalog()
        cls.template = LockedTemplate(catalog=catalog, reference_path=str(reference))
        cls.a12 = cls.template.by_pos[("A", 12)]
        cls.a13 = cls.template.by_pos[("A", 13)]

    def _alignment(self, arcs):
        t = self.template
        color = np.full((t.h, t.w, 3), 255, np.uint8)
        for center, axes, start, end in arcs:
            cv2.ellipse(color, tuple(center), tuple(axes), 0, start, end, (0, 0, 255), 4)
        return Alignment(
            color=color,
            gray=t.ref.copy(),
            rotation_ccw=0,
            match_score=0.95,
            orb_inlier_ratio=0.90,
        )

    @staticmethod
    def _codes(procedures, diagnoses):
        return {x["code"] for x in procedures + diagnoses}

    def test_incomplete_circle_auto_selects(self):
        a = self.a12
        alignment = self._alignment([
            (a["center"], (54, 19), 0, 180),
        ])
        procedures, diagnoses, possible, geometry = self.template.detect(alignment)
        self.assertIn(a["code"], self._codes(procedures, diagnoses))
        self.assertEqual(possible, [])
        self.assertEqual(geometry["possible_mark_count"], 0)

    def test_split_circle_fragments_auto_select(self):
        a = self.a12
        alignment = self._alignment([
            (a["center"], (54, 19), 0, 105),
            (a["center"], (54, 19), 140, 235),
            (a["center"], (54, 19), 275, 350),
        ])
        procedures, diagnoses, possible, _ = self.template.detect(alignment)
        self.assertIn(a["code"], self._codes(procedures, diagnoses))
        self.assertEqual(possible, [])

    def test_incomplete_black_circle_auto_selects_with_strong_alignment(self):
        t, a = self.template, self.a12
        gray = t.ref.copy()
        cv2.ellipse(gray, tuple(a["center"]), (54, 19), 0, 0, 180, 0, 4)
        color = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        alignment = Alignment(
            color=color, gray=gray, rotation_ccw=0,
            match_score=0.95, orb_inlier_ratio=0.90,
        )
        procedures, diagnoses, possible, geometry = t.detect(alignment)
        self.assertIn(a["code"], self._codes(procedures, diagnoses))
        self.assertEqual(possible, [])
        self.assertEqual(geometry["residual_selected_count"], 1)

    def test_circle_centered_between_rows_becomes_exception(self):
        a, b = self.a12, self.a13
        center = (a["center"][0], (a["center"][1] + b["center"][1]) // 2)
        alignment = self._alignment([
            (center, (54, 19), 0, 360),
        ])
        procedures, diagnoses, possible, _ = self.template.detect(alignment)
        codes = self._codes(procedures, diagnoses)
        self.assertNotIn(a["code"], codes)
        self.assertNotIn(b["code"], codes)
        self.assertEqual(len(possible), 1)
        self.assertEqual(possible[0]["reason"], "adjacent_code_overlap")
        candidate_codes = {x["code"] for x in possible[0]["candidate_codes"]}
        self.assertEqual(candidate_codes, {a["code"], b["code"]})

    def test_two_real_adjacent_circles_both_select(self):
        a, b = self.a12, self.a13
        alignment = self._alignment([
            (a["center"], (54, 19), 0, 360),
            (b["center"], (54, 19), 0, 360),
        ])
        procedures, diagnoses, possible, _ = self.template.detect(alignment)
        codes = self._codes(procedures, diagnoses)
        self.assertIn(a["code"], codes)
        self.assertIn(b["code"], codes)
        self.assertEqual(possible, [])

    def test_checkmark_is_not_selected_as_circle(self):
        t, a = self.template, self.a12
        color = np.full((t.h, t.w, 3), 255, np.uint8)
        cx, cy = a["center"]
        cv2.line(color, (cx - 25, cy), (cx - 10, cy + 13), (0, 0, 255), 4)
        cv2.line(color, (cx - 10, cy + 13), (cx + 30, cy - 20), (0, 0, 255), 4)
        alignment = Alignment(
            color=color, gray=t.ref.copy(), rotation_ccw=0,
            match_score=0.95, orb_inlier_ratio=0.90,
        )
        procedures, diagnoses, _, _ = t.detect(alignment)
        self.assertNotIn(a["code"], self._codes(procedures, diagnoses))


if __name__ == "__main__":
    unittest.main()
