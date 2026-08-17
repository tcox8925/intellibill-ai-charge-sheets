import unittest

import numpy as np

from locked_template import Alignment, LockedTemplate
from template_registry import load_manifest_and_catalog, template_paths


class FailClosedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _, catalog, reference = load_manifest_and_catalog()
        cls.template = LockedTemplate(catalog=catalog, reference_path=str(reference))

    def test_low_alignment_disables_black_residual_geometry(self):
        t = self.template
        threshold = float(t.thresholds["black_alignment_min"])
        score = max(float(t.thresholds["match_min"]), threshold - 0.03)
        a = Alignment(
            color=np.full((t.h, t.w, 3), 255, np.uint8),
            gray=np.full((t.h, t.w), 255, np.uint8),
            rotation_ccw=0,
            match_score=score,
            orb_inlier_ratio=0.1,
        )
        _, _, possible, geometry = t.detect(a)
        self.assertEqual(possible, [])
        self.assertFalse(geometry["residual_geometry_enabled"])
        self.assertEqual(geometry["residual_selected_count"], 0)


if __name__ == "__main__":
    unittest.main()
