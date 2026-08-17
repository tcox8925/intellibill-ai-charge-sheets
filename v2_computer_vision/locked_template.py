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
    "residual_pixel_min": 40,
    "black_alignment_min": 0.90,

    # Partial-arc detector.  These are deliberately conservative: incomplete
    # circles can auto-select when the arc is coherent, while uncertain marks
    # become possible_marks instead of billing-code selections.
    "ring_sector_hit_min": 0.18,
    "ring_side_hit_min": 0.16,
    "ring_confirm_sectors": 6,
    "ring_confirm_longest": 5,
    "ring_confirm_sides": 0.50,
    "color_possible_min": 0.30,
    "color_confirm_min": 0.42,
    "black_possible_min": 0.32,
    "black_confirm_min": 0.44,
    "candidate_winner_margin": 0.10,
    "adjacent_pair_strong_min": 0.68,
    "adjacent_pair_sectors": 9,
    "adjacent_pair_longest": 8,
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
    def _ring_evidence(self, mask_or_diff: np.ndarray, cxbase: int, cybase: int,
                       threshold: int, binary: bool = False) -> dict:
        """Measure partial-loop evidence around one locked code coordinate.

        Unlike the v1 ellipse score, this does not require one connected component
        or a nearly complete ellipse.  It evaluates the expected ring around the
        known code location in angular sectors, so pen lifts, grid-line splits, and
        circles crossing printed text can still be recognized deterministically.
        """
        h, w = mask_or_diff.shape[:2]
        xa, xb = max(0, cxbase - 90), min(w, cxbase + 91)
        ya, yb = max(0, cybase - 45), min(h, cybase + 46)
        patch = mask_or_diff[ya:yb, xa:xb]
        if patch.size == 0:
            return {
                "score": 0.0, "ring_coverage": 0.0, "sector_coverage": 0,
                "longest_arc_sectors": 0, "side_coverage": 0.0,
                "interior_ink": 0.0, "ring_capture": 0.0,
                "center_offset": [0, 0], "radius": [0, 0],
            }

        foreground = patch > 0 if binary else patch > threshold
        best = None
        sector_hit_min = float(self.thresholds["ring_sector_hit_min"])
        side_hit_min = float(self.thresholds["ring_side_hit_min"])

        # Search a small deterministic neighborhood because hand-drawn circles are
        # rarely centered perfectly on the printed code.
        for dx in (-10, -5, 0, 5, 10):
            for dy in (-5, 0, 5):
                cx, cy = cxbase + dx - xa, cybase + dy - ya
                for rx in (30, 42, 54, 66):
                    for ry in (13, 19, 25):
                        ring_img = np.zeros(patch.shape, dtype=np.uint8)
                        cv2.ellipse(ring_img, (cx, cy), (rx, ry), 0, 0, 360, 255, 5)
                        ring = ring_img > 0
                        yy, xx = np.where(ring)
                        if not len(xx):
                            continue

                        hit = foreground[yy, xx]
                        ring_coverage = float(hit.mean())

                        # Twelve sectors are intentionally more tolerant than the
                        # old four-quadrant test.  A real partial loop can miss a
                        # substantial section and still have a long coherent arc.
                        ang = (np.arctan2(
                            (yy - cy) / max(ry, 1),
                            (xx - cx) / max(rx, 1),
                        ) + 2 * math.pi) % (2 * math.pi)
                        sector_rates = []
                        for sector in range(12):
                            lo = 2 * math.pi * sector / 12.0
                            hi = 2 * math.pi * (sector + 1) / 12.0
                            q = (ang >= lo) & (ang < hi)
                            sector_rates.append(float(hit[q].mean()) if q.any() else 0.0)

                        present = [v >= sector_hit_min for v in sector_rates]
                        sector_coverage = int(sum(present))

                        # Circular longest run.  This rewards one long continuous
                        # arc even when the circle never closes.
                        longest = cur = 0
                        for value in present + present:
                            cur = cur + 1 if value else 0
                            longest = max(longest, cur)
                        longest = min(longest, 12)

                        quadrants = [
                            float(np.mean(sector_rates[i:i + 3]))
                            for i in range(0, 12, 3)
                        ]
                        side_coverage = float(sum(v >= side_hit_min for v in quadrants) / 4.0)

                        # Interior ink is a small negative feature.  It prevents a
                        # slash/check/scribble from scoring like a ring while not
                        # punishing circles that happen to cross printed text.
                        inner_img = np.zeros(patch.shape, dtype=np.uint8)
                        cv2.ellipse(
                            inner_img,
                            (cx, cy),
                            (max(6, int(rx * 0.55)), max(5, int(ry * 0.48))),
                            0, 0, 360, 255, -1,
                        )
                        inner = inner_img > 0
                        interior_ink = float(foreground[inner].mean()) if inner.any() else 0.0

                        # How much of the local foreground is actually explained by
                        # the candidate ring.  This is useful when handwriting is
                        # present near the code but not around it.
                        outer_img = np.zeros(patch.shape, dtype=np.uint8)
                        cv2.ellipse(
                            outer_img,
                            (cx, cy),
                            (rx + 8, ry + 8),
                            0, 0, 360, 255, -1,
                        )
                        local_fg = foreground & (outer_img > 0)
                        ring_capture = float((foreground & ring).sum() / max(1, int(local_fg.sum())))

                        score = (
                            0.36 * ring_coverage
                            + 0.24 * (sector_coverage / 12.0)
                            + 0.18 * (longest / 12.0)
                            + 0.12 * side_coverage
                            + 0.10 * ring_capture
                            - 0.05 * min(interior_ink, 0.60)
                        )
                        candidate = {
                            "score": float(max(0.0, score)),
                            "ring_coverage": ring_coverage,
                            "sector_coverage": sector_coverage,
                            "longest_arc_sectors": int(longest),
                            "side_coverage": side_coverage,
                            "interior_ink": interior_ink,
                            "ring_capture": ring_capture,
                            "center_offset": [int(dx), int(dy)],
                            "radius": [int(rx), int(ry)],
                        }
                        if best is None or candidate["score"] > best["score"]:
                            best = candidate

        return best or {
            "score": 0.0, "ring_coverage": 0.0, "sector_coverage": 0,
            "longest_arc_sectors": 0, "side_coverage": 0.0,
            "interior_ink": 0.0, "ring_capture": 0.0,
            "center_offset": [0, 0], "radius": [0, 0],
        }

    # Backward-compatible helper for any external callers/tests that used the v1
    # private method.  Runtime selection uses _ring_evidence directly.
    def _ellipse_score(self, mask_or_diff: np.ndarray, cxbase: int, cybase: int,
                       threshold: int, binary: bool = False) -> float:
        return float(self._ring_evidence(
            mask_or_diff, cxbase, cybase, threshold, binary=binary
        )["score"])

    @staticmethod
    def _evidence_json(evidence: dict) -> dict:
        return {
            "score": round(float(evidence.get("score", 0.0)), 4),
            "ring_coverage": round(float(evidence.get("ring_coverage", 0.0)), 4),
            "sector_coverage": int(evidence.get("sector_coverage", 0)),
            "longest_arc_sectors": int(evidence.get("longest_arc_sectors", 0)),
            "side_coverage": round(float(evidence.get("side_coverage", 0.0)), 4),
            "interior_ink": round(float(evidence.get("interior_ink", 0.0)), 4),
            "ring_capture": round(float(evidence.get("ring_capture", 0.0)), 4),
            "center_offset": [int(v) for v in evidence.get("center_offset", [0, 0])],
            "radius": [int(v) for v in evidence.get("radius", [0, 0])],
        }

    def _score_color_cells(self, color: np.ndarray):
        """Score colored ink around every known code instead of components first."""
        hsv = cv2.cvtColor(color, cv2.COLOR_BGR2HSV)
        mask = ((hsv[:, :, 1] > self.thresholds["color_sat_min"]) &
                (hsv[:, :, 2] > 50)).astype(np.uint8) * 255
        mask[:140, :] = 0
        if self.h > 1590:
            mask[1590:, :] = 0
        mask = cv2.morphologyEx(
            mask,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)),
        )

        scores: Dict[Tuple[str, int], dict] = {}
        for cell in self.cells:
            key = (cell["column"], int(cell["row"]))
            cx, cy = int(cell["center"][0]), int(cell["center"][1])
            patch = mask[max(0, cy - 38):min(self.h, cy + 39),
                         max(0, cx - 82):min(self.w, cx + 83)]
            # A partial circle still occupies comfortably more than this.  The gate
            # avoids an expensive ring search on the many completely clean cells.
            if patch.size == 0 or float((patch > 0).mean()) < 0.004:
                continue
            ev = self._ring_evidence(mask, cx, cy, threshold=0, binary=True)
            if ev["score"] >= self.thresholds["color_possible_min"]:
                scores[key] = ev
        return scores, mask

    def _score_black_cells(self, residual: np.ndarray):
        scores: Dict[Tuple[str, int], dict] = {}
        for cell in self.cells:
            key = (cell["column"], int(cell["row"]))
            cx, cy = int(cell["center"][0]), int(cell["center"][1])
            patch = residual[max(0, cy - 38):min(self.h, cy + 39),
                             max(0, cx - 82):min(self.w, cx + 83)]
            if (patch.size == 0 or
                    float((patch > self.thresholds["residual_pixel_min"]).mean()) < 0.012):
                continue
            ev = self._ring_evidence(
                residual, cx, cy,
                self.thresholds["residual_pixel_min"],
                binary=False,
            )
            if ev["score"] >= self.thresholds["black_possible_min"]:
                scores[key] = ev
        return scores

    def _is_confirmed_candidate(self, candidate: dict) -> bool:
        ev = candidate["evidence"]
        source = candidate["source"]
        score_min = self.thresholds[
            "color_confirm_min" if source == "color_geometry" else "black_confirm_min"
        ]
        return (
            ev["score"] >= score_min
            and ev["sector_coverage"] >= self.thresholds["ring_confirm_sectors"]
            and ev["longest_arc_sectors"] >= self.thresholds["ring_confirm_longest"]
            and ev["side_coverage"] >= self.thresholds["ring_confirm_sides"]
        )

    def _independently_strong(self, candidate: dict) -> bool:
        """True when adjacent rows clearly contain two separate strong loops."""
        ev = candidate["evidence"]
        dx, dy = ev.get("center_offset", [99, 99])
        return (
            self._is_confirmed_candidate(candidate)
            and ev["score"] >= self.thresholds["adjacent_pair_strong_min"]
            and ev["sector_coverage"] >= self.thresholds["adjacent_pair_sectors"]
            and ev["longest_arc_sectors"] >= self.thresholds["adjacent_pair_longest"]
            and abs(int(dx)) <= 6
            and abs(int(dy)) <= 5
        )

    def _review_region(self, keys: List[Tuple[str, int]]) -> dict:
        centers = [self.by_pos[k]["center"] for k in keys]
        x1 = max(0, min(int(c[0]) for c in centers) - 90)
        x2 = min(self.w, max(int(c[0]) for c in centers) + 91)
        y1 = max(0, min(int(c[1]) for c in centers) - 45)
        y2 = min(self.h, max(int(c[1]) for c in centers) + 46)
        return {"x1": x1, "y1": y1, "x2": x2, "y2": y2}

    def _resolve_candidates(self, candidates: Dict[Tuple[str, int], dict]):
        """Resolve strong winners; return genuine ambiguity as possible_marks.

        Resolution is local to a code column because adjacent rows are where one
        physical circle can overlap two candidate codes.  Two independently strong
        neighboring loops remain valid and can both be selected.
        """
        selected: Dict[Tuple[str, int], dict] = {}
        possible_marks: List[dict] = []
        consumed = set()

        for col in self.code_columns:
            rows = sorted(r for (c, r) in candidates if c == col)
            rowset = set(rows)

            # First identify adjacent candidate groups.
            groups = []
            i = 0
            while i < len(rows):
                group = [rows[i]]
                while i + 1 < len(rows) and rows[i + 1] == rows[i] + 1:
                    i += 1
                    group.append(rows[i])
                groups.append(group)
                i += 1

            for group_rows in groups:
                group_keys = [(col, r) for r in group_rows]
                group = [candidates[k] for k in group_keys]

                if len(group) == 1:
                    key = group_keys[0]
                    cand = group[0]
                    if self._is_confirmed_candidate(cand):
                        selected[key] = cand
                    else:
                        cell = self.by_pos[key]
                        possible_marks.append({
                            "type": "possible_circle",
                            "reason": "partial_circle_low_confidence",
                            "kind": cell["kind"],
                            "candidate_codes": [{
                                "code": cell["code"],
                                "score": round(float(cand["evidence"]["score"]), 4),
                                "source": cand["source"],
                            }],
                            "review_region": self._review_region([key]),
                            "evidence": self._evidence_json(cand["evidence"]),
                        })
                    consumed.add(key)
                    continue

                # Allow adjacent selections when two or more candidates look like
                # separate, independently centered strong loops.  Weak spillover
                # candidates immediately above/below do not turn a real pair into
                # an exception.  This also supports three legitimate adjacent
                # circles when all three are independently strong.
                independent = [
                    (key, cand) for key, cand in zip(group_keys, group)
                    if self._independently_strong(cand)
                ]
                if len(independent) >= 2:
                    independent_keys = {key for key, _ in independent}
                    for key, cand in independent:
                        selected[key] = cand

                    # If an extra candidate is strong enough to be a real partial
                    # third circle but not independently strong enough to auto-select,
                    # preserve it as review instead of silently discarding it.
                    for key, cand in zip(group_keys, group):
                        if key in independent_keys or not self._is_confirmed_candidate(cand):
                            continue
                        cell = self.by_pos[key]
                        possible_marks.append({
                            "type": "possible_circle",
                            "reason": "partial_circle_adjacent_to_confirmed",
                            "kind": cell["kind"],
                            "candidate_codes": [{
                                "code": cell["code"],
                                "score": round(float(cand["evidence"]["score"]), 4),
                                "source": cand["source"],
                            }],
                            "review_region": self._review_region([key]),
                            "evidence": self._evidence_json(cand["evidence"]),
                        })
                    for key in group_keys:
                        consumed.add(key)
                    continue

                ranked = sorted(
                    zip(group_keys, group),
                    key=lambda kv: float(kv[1]["evidence"]["score"]),
                    reverse=True,
                )
                best_key, best = ranked[0]
                second_key, second = ranked[1]
                margin = float(best["evidence"]["score"] - second["evidence"]["score"])

                # A clear confirmed winner suppresses spillover evidence on an
                # adjacent row.  Otherwise, fail closed and put the candidates in
                # the same JSON as an exception/review item.
                second_confirmed = self._is_confirmed_candidate(second)
                if (self._is_confirmed_candidate(best) and not second_confirmed and
                        margin >= self.thresholds["candidate_winner_margin"]):
                    selected[best_key] = best
                    for key in group_keys:
                        consumed.add(key)
                    continue

                # Keep only plausible competing codes in the exception payload.
                # Weak spillover rows are useful for scoring but should not clutter
                # the human review choice.
                plausible = [
                    (key, cand) for key, cand in ranked
                    if self._is_confirmed_candidate(cand)
                    or float(best["evidence"]["score"] - cand["evidence"]["score"])
                       < self.thresholds["candidate_winner_margin"]
                ]
                if len(plausible) < 2:
                    plausible = ranked[:2]

                candidate_codes = []
                kinds = set()
                plausible_keys = []
                for key, cand in plausible:
                    plausible_keys.append(key)
                    cell = self.by_pos[key]
                    kinds.add(cell["kind"])
                    candidate_codes.append({
                        "code": cell["code"],
                        "score": round(float(cand["evidence"]["score"]), 4),
                        "source": cand["source"],
                    })
                possible_marks.append({
                    "type": "ambiguous_circle",
                    "reason": "adjacent_code_overlap",
                    "kind": next(iter(kinds)) if len(kinds) == 1 else "mixed",
                    "candidate_codes": candidate_codes,
                    "winner_margin": round(margin, 4),
                    "review_region": self._review_region(plausible_keys),
                })
                for key in group_keys:
                    consumed.add(key)

        # Defensive fallback if a future catalog contains a column not listed in
        # code_columns: never silently drop a candidate.
        for key, cand in candidates.items():
            if key in consumed:
                continue
            if self._is_confirmed_candidate(cand):
                selected[key] = cand
            else:
                cell = self.by_pos[key]
                possible_marks.append({
                    "type": "possible_circle",
                    "reason": "partial_circle_low_confidence",
                    "kind": cell["kind"],
                    "candidate_codes": [{
                        "code": cell["code"],
                        "score": round(float(cand["evidence"]["score"]), 4),
                        "source": cand["source"],
                    }],
                    "review_region": self._review_region([key]),
                    "evidence": self._evidence_json(cand["evidence"]),
                })

        return selected, possible_marks

    def detect(self, alignment: Alignment) -> Tuple[List[dict], List[dict], List[dict], dict]:
        if not self.is_match(alignment):
            return [], [], [], {
                "mode": "locked_coordinates_fail_closed",
                "template_match": alignment.match_score,
                "error": "locked_template_mismatch",
            }

        color_scores, _ = self._score_color_cells(alignment.color)

        # Template subtraction remains fail-closed when registration is marginal.
        residual_enabled = alignment.match_score >= self.thresholds["black_alignment_min"]
        residual = cv2.subtract(self.ref, alignment.gray) if residual_enabled else None
        black_scores = self._score_black_cells(residual) if residual_enabled else {}

        # Merge color and black evidence for the same locked cell.  Direct colored
        # ink is authoritative when it exists; black residual evidence is then used
        # only for cells without a color candidate.  Residual spillover adjacent to
        # a colored loop is suppressed because it can be caused by that same mark.
        candidates: Dict[Tuple[str, int], dict] = {}
        for key, ev in color_scores.items():
            candidates[key] = {"source": "color_geometry", "evidence": ev}

        for key, ev in black_scores.items():
            col, row = key
            if key in candidates:
                continue
            if any(c == col and abs(r - row) <= 1 for c, r in color_scores):
                continue
            candidates[key] = {"source": "residual_geometry", "evidence": ev}

        selected, possible_marks = self._resolve_candidates(candidates)

        procedures, diagnoses = [], []
        evidence = []
        for key, cand in sorted(selected.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            cell = self.by_pos[key]
            source = cand["source"]
            ev = cand["evidence"]
            score = float(ev["score"])
            item = {
                "code": cell["code"],
                "description": cell.get("description", ""),
                "section": cell.get("section", ""),
                "mark": "circle",
                "confidence": round(min(0.99, 0.58 + score * 0.46), 2),
                # Keep the existing detection-source contract for DB compatibility.
                "detection": source,
                "geometry_mode": (
                    "partial_arc" if ev["sector_coverage"] < 11 else "full_loop"
                ),
            }
            (procedures if cell["kind"] == "procedure" else diagnoses).append(item)
            evidence.append({
                "column": key[0],
                "row": key[1],
                "code": cell["code"],
                "source": source,
                **self._evidence_json(ev),
            })

        debug = {
            "mode": "locked_coordinate_partial_arc_geometry_v2",
            "template_id": self.catalog["template_id"],
            "template_match": round(alignment.match_score, 4),
            "orb_inlier_ratio": round(alignment.orb_inlier_ratio, 4),
            "selected_count": len(selected),
            "possible_mark_count": len(possible_marks),
            "color_selected_count": sum(
                1 for v in selected.values() if v["source"] == "color_geometry"
            ),
            "residual_selected_count": sum(
                1 for v in selected.values() if v["source"] == "residual_geometry"
            ),
            "residual_geometry_enabled": residual_enabled,
            "residual_alignment_min": self.thresholds["black_alignment_min"],
            "residual_disabled_reason": (
                None if residual_enabled else "alignment_below_residual_safety_threshold"
            ),
            "candidate_count": len(candidates),
            "evidence": evidence,
        }
        return procedures, diagnoses, possible_marks, debug


_LOCKED = None

def get_locked_template() -> LockedTemplate:
    global _LOCKED
    if _LOCKED is None:
        _LOCKED = LockedTemplate()
    return _LOCKED
