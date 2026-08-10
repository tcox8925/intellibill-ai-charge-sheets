"""Deterministic locked-template circle extraction for the NWA Internal Medicine superbill.

Runtime code selection uses NO LLM/OCR. The form is aligned to a PHI-sanitized
locked reference, then physical hand-drawn loops are detected against fixed code
cell coordinates. A model may still be used elsewhere for handwritten header/
notes, but it cannot add, remove, rename, or classify selected billing codes.
"""
from __future__ import annotations

import json, math, os
from dataclasses import dataclass
from typing import Dict, List, Tuple

import cv2
import numpy as np

DEFAULT_THRESHOLDS = {
    "match_min": 0.78,
    "color_sat_min": 60,
    "color_ellipse_min": 0.28,
    "residual_pixel_min": 40,
    "black_min": 0.30,
    "black_strong": 0.47,
    "black_pair_min": 0.43,
    "black_local_margin": 0.035,
    "black_alignment_min": 0.90,
}


@dataclass
class Alignment:
    color: np.ndarray
    gray: np.ndarray
    rotation_ccw: int
    match_score: float
    orb_inlier_ratio: float


class LockedTemplate:
    def __init__(self, *, catalog: dict | None = None, reference_bytes: bytes | None = None,
                 catalog_path: str | None = None, reference_path: str | None = None,
                 thresholds: dict | None = None):
        if catalog is not None:
            self.catalog = catalog
        elif catalog_path:
            with open(catalog_path, encoding="utf-8") as f:
                self.catalog = json.load(f)
        else:
            raise RuntimeError("locked template catalog is required")

        if reference_bytes is not None:
            arr = np.frombuffer(reference_bytes, dtype=np.uint8)
            self.ref = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        elif reference_path:
            self.ref = cv2.imread(reference_path, cv2.IMREAD_GRAYSCALE)
        else:
            raise RuntimeError("locked template reference is required")
        if self.ref is None:
            raise RuntimeError("locked template reference could not be decoded")

        self.thresholds = dict(DEFAULT_THRESHOLDS)
        self.thresholds.update(thresholds or {})
        self.h, self.w = self.ref.shape
        self.cells = self.catalog["cells"]
        self.by_pos = {(c["column"], int(c["row"])): c for c in self.cells}
        self.code_columns = {k: tuple(v) for k, v in self.catalog["code_columns"].items()}
        self.row_lines = [int(v) for v in self.catalog["row_lines"]]
        self.row_centers = [(self.row_lines[r] + self.row_lines[r + 1]) // 2
                            for r in range(len(self.row_lines) - 1)]
        self._orb = cv2.ORB_create(nfeatures=4500, fastThreshold=10)
        self._ref_kp, self._ref_des = self._orb.detectAndCompute(self.ref, None)
        self._matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        self._ecc_ref = cv2.GaussianBlur(self.ref, (5, 5), 0).astype(np.float32) / 255.0
        self._ecc_mask = np.zeros_like(self.ref, dtype=np.uint8)
        self._ecc_mask[140:min(1590, self.h), 8:min(2105, self.w)] = 255

    # ---------------- alignment / recognition ----------------
    def _orb_align(self, gray: np.ndarray, color: np.ndarray):
        gray = cv2.resize(gray, (self.w, self.h), interpolation=cv2.INTER_AREA)
        color = cv2.resize(color, (self.w, self.h), interpolation=cv2.INTER_AREA)
        kp, des = self._orb.detectAndCompute(gray, None)
        if des is None or self._ref_des is None:
            return gray, color, 0.0
        pairs = self._matcher.knnMatch(des, self._ref_des, k=2)
        good = [m for m, n in pairs if m.distance < 0.78 * n.distance]
        if len(good) < 20:
            return gray, color, 0.0
        src = np.float32([kp[m.queryIdx].pt for m in good])
        dst = np.float32([self._ref_kp[m.trainIdx].pt for m in good])
        M, inliers = cv2.estimateAffinePartial2D(
            src, dst, method=cv2.RANSAC, ransacReprojThreshold=3.0,
            maxIters=3000, confidence=0.995)
        if M is None:
            return gray, color, 0.0
        ratio = float(inliers.sum() / len(inliers)) if inliers is not None and len(inliers) else 0.0
        ag = cv2.warpAffine(gray, M, (self.w, self.h), borderValue=255)
        ac = cv2.warpAffine(color, M, (self.w, self.h), borderValue=(255, 255, 255))
        return ag, ac, ratio

    def _ecc_refine(self, gray: np.ndarray, color: np.ndarray):
        page_bin = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY)[1]
        page_f = cv2.GaussianBlur(page_bin, (5, 5), 0).astype(np.float32) / 255.0
        warp = np.eye(2, 3, dtype=np.float32)
        try:
            cc, warp = cv2.findTransformECC(
                self._ecc_ref, page_f, warp, cv2.MOTION_AFFINE,
                (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 45, 1e-5),
                self._ecc_mask, 3)
            gray2 = cv2.warpAffine(gray, warp, (self.w, self.h),
                                   flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
                                   borderValue=255)
            color2 = cv2.warpAffine(color, warp, (self.w, self.h),
                                    flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
                                    borderValue=(255, 255, 255))
            return gray2, color2, float(cc)
        except cv2.error:
            return gray, color, 0.0

    @staticmethod
    def _rot(img: np.ndarray, deg: int) -> np.ndarray:
        if deg == 0:
            return img.copy()
        return np.rot90(img, deg // 90).copy()  # CCW

    def align(self, image) -> Alignment:
        # OpenCV RANSAC/feature routines may use a process-global RNG. Pin it so
        # repeated runs on the same bytes do not choose different affine seeds.
        cv2.setRNGSeed(8342026)
        if isinstance(image, np.ndarray):
            color0 = image.copy()
        else:
            color0 = cv2.imread(str(image), cv2.IMREAD_COLOR)
        if color0 is None:
            raise RuntimeError(f"cannot read page image: {image}")
        gray0 = cv2.cvtColor(color0, cv2.COLOR_BGR2GRAY)
        # Pick orientation cheaply from grid/text edge overlap, then run the
        # expensive ORB+ECC registration only once. This is deterministic and
        # avoids a vision-model orientation call.
        rotations = (90, 270) if gray0.shape[0] > gray0.shape[1] else (0, 180)
        ref_edges = cv2.Canny(self.ref, 50, 150)
        ref_d = cv2.dilate(ref_edges, np.ones((3, 3), np.uint8))
        orient_scores = []
        for deg0 in rotations:
            g0 = self._rot(gray0, deg0)
            g0 = cv2.resize(g0, (self.w, self.h), interpolation=cv2.INTER_AREA)
            e0 = cv2.Canny(g0, 50, 150)
            e0d = cv2.dilate(e0, np.ones((3, 3), np.uint8))
            a = float(((e0 > 0) & (ref_d > 0)).sum()) / max(1, int((e0 > 0).sum()))
            b = float(((ref_edges > 0) & (e0d > 0)).sum()) / max(1, int((ref_edges > 0).sum()))
            orient_scores.append(((a + b) / 2.0, deg0))
        _, deg = max(orient_scores)
        g = self._rot(gray0, deg)
        c = self._rot(color0, deg)
        g, c, orb_ratio = self._orb_align(g, c)
        g, c, score = self._ecc_refine(g, c)
        return Alignment(color=c, gray=g, rotation_ccw=deg,
                         match_score=float(score), orb_inlier_ratio=float(orb_ratio))

    def is_match(self, alignment: Alignment) -> bool:
        return alignment.match_score >= self.thresholds["match_min"]

    # ---------------- circle geometry ----------------
    @staticmethod
    def _ellipse_score(mask_or_diff: np.ndarray, cxbase: int, cybase: int,
                       threshold: int, binary: bool = False) -> float:
        h, w = mask_or_diff.shape[:2]
        xa, xb = max(0, cxbase - 90), min(w, cxbase + 91)
        ya, yb = max(0, cybase - 45), min(h, cybase + 46)
        patch = mask_or_diff[ya:yb, xa:xb]
        best = 0.0
        for dx in (-10, 0, 10):
            for dy in (-5, 0, 5):
                cx, cy = cxbase + dx - xa, cybase + dy - ya
                for rx in (30, 42, 54, 66):
                    for ry in (13, 19, 25):
                        em = np.zeros(patch.shape, dtype=np.uint8)
                        cv2.ellipse(em, (cx, cy), (rx, ry), 0, 0, 360, 1, 3)
                        yy, xx = np.where(em > 0)
                        if not len(xx):
                            continue
                        vals = patch[yy, xx]
                        hit = vals > 0 if binary else vals > threshold
                        frac = float(hit.mean())
                        ang = np.arctan2((yy - cy) / max(ry, 1), (xx - cx) / max(rx, 1))
                        quads = []
                        for lo, hi in ((-math.pi, -math.pi / 2), (-math.pi / 2, 0),
                                       (0, math.pi / 2), (math.pi / 2, math.pi)):
                            q = (ang >= lo) & (ang < hi)
                            quads.append(float(hit[q].mean()) if q.any() else 0.0)
                        # A real loop normally lights at least three quadrants;
                        # one weak quadrant is tolerated for an imperfect hand circle.
                        q3 = sum(sorted(quads)[1:]) / 3.0
                        score = 0.60 * frac + 0.40 * q3
                        if score > best:
                            best = score
        return best

    def _color_candidates(self, color: np.ndarray):
        hsv = cv2.cvtColor(color, cv2.COLOR_BGR2HSV)
        mask = ((hsv[:, :, 1] > self.thresholds["color_sat_min"]) & (hsv[:, :, 2] > 50)).astype(np.uint8) * 255
        mask[:140, :] = 0
        if self.h > 1590:
            mask[1590:, :] = 0
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
        n, labels, stats, cents = cv2.connectedComponentsWithStats(mask, 8)
        proposed = {}
        for j in range(1, n):
            x, y, ww, hh, area = [int(v) for v in stats[j]]
            if area < 80 or ww < 25 or hh < 12:
                continue
            cx, cy = [float(v) for v in cents[j]]
            col = None
            for name, (x0, x1) in self.code_columns.items():
                if x0 - 8 <= cx <= x1 + 8:
                    col = name
                    break
            if not col:
                continue
            row = min(range(len(self.row_centers)), key=lambda r: abs(self.row_centers[r] - cy))
            if abs(self.row_centers[row] - cy) > 22:
                continue
            cell = self.by_pos.get((col, row))
            if not cell:
                continue
            score = self._ellipse_score(mask, int(cell["center"][0]), int(cell["center"][1]),
                                        threshold=0, binary=True)
            if score < self.thresholds["color_ellipse_min"]:
                continue
            key = (col, row)
            if key not in proposed or score > proposed[key]:
                proposed[key] = score
        # One colored hand circle can be split by a printed grid line into two
        # adjacent connected components. Collapse adjacent-row proposals in the
        # same code column to the stronger row unless BOTH are independently
        # very strong loops. This prevents one circle from selecting two codes.
        pruned = dict(proposed)
        for col in self.code_columns:
            rows = sorted(r for (c, r) in proposed if c == col)
            for r in rows:
                if (col, r) not in pruned or (col, r + 1) not in pruned:
                    continue
                a, b = pruned[(col, r)], pruned[(col, r + 1)]
                if a >= 0.55 and b >= 0.55:
                    continue
                if a >= b:
                    pruned.pop((col, r + 1), None)
                else:
                    pruned.pop((col, r), None)
        return pruned, mask

    def detect(self, alignment: Alignment) -> Tuple[List[dict], List[dict], dict]:
        if not self.is_match(alignment):
            return [], [], {
                "mode": "locked_coordinates_fail_closed",
                "template_match": alignment.match_score,
                "error": "locked_template_mismatch"
            }

        color_hits, color_mask = self._color_candidates(alignment.color)

        # IMPORTANT: template subtraction becomes unsafe when registration is
        # only marginal.  At those scores the printed grid/text leaves elliptical
        # ghost residuals that can look like hand-drawn black circles.  The user's
        # precision requirement is fail-closed: keep direct color-loop evidence,
        # but disable black/residual selection until registration is strong.
        residual_enabled = alignment.match_score >= self.thresholds["black_alignment_min"]
        residual = cv2.subtract(self.ref, alignment.gray) if residual_enabled else None

        # Score every locked code cell against hand-drawn elliptical residual ink
        # only when the subtraction is geometrically trustworthy.
        black_scores: Dict[Tuple[str, int], float] = {}
        if residual_enabled:
            for cell in self.cells:
                key = (cell["column"], int(cell["row"]))
                cx, cy = int(cell["center"][0]), int(cell["center"][1])
                patch = residual[max(0, cy-34):min(self.h, cy+35),
                                 max(0, cx-76):min(self.w, cx+77)]
                # Cheap gate: clean cells never enter the ellipse search.
                if patch.size == 0 or float((patch > self.thresholds["residual_pixel_min"]).mean()) < 0.015:
                    black_scores[key] = 0.0
                else:
                    black_scores[key] = self._ellipse_score(
                        residual, cx, cy, self.thresholds["residual_pixel_min"], binary=False)

        selected = {}
        evidence = []
        for key, score in color_hits.items():
            selected[key] = ("color_geometry", score)
            evidence.append({"column": key[0], "row": key[1], "source": "color_geometry",
                             "score": round(score, 4)})

        # Residual/black-ink path. A color loop is authoritative locally: residual
        # from that same physical loop is forbidden from selecting an adjacent row.
        for key, score in black_scores.items():
            col, row = key
            if key in selected:
                continue
            if any(c == col and abs(r - row) <= 1 for c, r in color_hits):
                continue
            if score < self.thresholds["black_min"]:
                continue
            prev_s = black_scores.get((col, row - 1), 0.0)
            next_s = black_scores.get((col, row + 1), 0.0)
            local_ok = score >= max(prev_s, next_s) + self.thresholds["black_local_margin"]
            adjacent_real_pair = (score >= self.thresholds["black_pair_min"] and
                                  max(prev_s, next_s) >= self.thresholds["black_pair_min"])
            if score < self.thresholds["black_strong"] and not local_ok and not adjacent_real_pair:
                continue
            selected[key] = ("residual_geometry", score)
            evidence.append({"column": col, "row": row, "source": "residual_geometry",
                             "score": round(score, 4),
                             "neighbor_scores": [round(prev_s, 4), round(next_s, 4)]})

        procedures, diagnoses = [], []
        for key, (source, score) in sorted(selected.items(), key=lambda kv:(kv[0][1], kv[0][0])):
            cell = self.by_pos[key]
            item = {
                "code": cell["code"],
                "description": cell.get("description", ""),
                "section": cell.get("section", ""),
                "mark": "circle",
                "confidence": round(min(0.99, 0.62 + score * 0.65), 2),
                "detection": source,
            }
            (procedures if cell["kind"] == "procedure" else diagnoses).append(item)

        debug = {
            "mode": "locked_coordinate_circle_geometry",
            "template_id": self.catalog["template_id"],
            "template_match": round(alignment.match_score, 4),
            "orb_inlier_ratio": round(alignment.orb_inlier_ratio, 4),
            "selected_count": len(selected),
            "color_selected_count": sum(1 for v in selected.values() if v[0] == "color_geometry"),
            "residual_selected_count": sum(1 for v in selected.values() if v[0] == "residual_geometry"),
            "residual_geometry_enabled": residual_enabled,
            "residual_alignment_min": self.thresholds["black_alignment_min"],
            "residual_disabled_reason": (None if residual_enabled else "alignment_below_residual_safety_threshold"),
            "evidence": evidence,
        }
        return procedures, diagnoses, debug


_LOCKED = None

def get_locked_template() -> LockedTemplate:
    global _LOCKED
    if _LOCKED is None:
        _LOCKED = LockedTemplate()
    return _LOCKED
