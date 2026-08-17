import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "templates" / "nwa_internal_medicine_superbill_locked_v1" / "v1"


class LockedTemplateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads((TPL / "manifest.json").read_text())
        cls.catalog = json.loads((TPL / "catalog.json").read_text())

    def test_artifact_hashes(self):
        sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
        self.assertEqual(sha(TPL / "reference.png"), self.manifest["reference_sha256"])
        self.assertEqual(sha(TPL / "catalog.json"), self.manifest["catalog_sha256"])

    def test_fail_closed_policy(self):
        policy = self.catalog["policy"]
        self.assertEqual(policy["selected_mark"], "circle_only")
        self.assertEqual(policy["runtime_code_source"], "locked_coordinates_only")
        self.assertEqual(policy["unknown_template"], "fail_closed")
        self.assertFalse(policy["ai_can_add_codes"])

    def test_catalog_cells_are_unique_and_typed(self):
        cells = self.catalog["cells"]
        self.assertGreater(len(cells), 150)
        pos = [(c["column"], int(c["row"])) for c in cells]
        self.assertEqual(len(pos), len(set(pos)))
        self.assertTrue(all(c["kind"] in {"procedure", "diagnosis"} for c in cells))
        self.assertTrue(all(c.get("code") for c in cells))

    def test_known_coordinates(self):
        by_code = {c["code"]: c for c in self.catalog["cells"]}
        for code in ("99214", "96372", "20550", "J1885", "J3301", "73630", "36415", "M72", "M54.5"):
            self.assertIn(code, by_code)


if __name__ == "__main__":
    unittest.main()
