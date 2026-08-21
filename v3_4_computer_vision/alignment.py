"""Page registration against the locked reference.

This is the alignment half of v2's locked_template.py, extracted unchanged in
behaviour: cheap orientation pick from grid/text edge overlap, ORB partial
affine, then ECC refinement. The RNG seed is pinned so repeated runs on the same
bytes choose the same affine seed.

The ring/arc scoring that used to live alongside it is deliberately NOT here.
v3 does not score rows; see mark_localizer.py.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

MATCH_MIN = 0.78          # ECC correlation floor for "this is the locked form"
CONTENT_Y_MIN = 140       # ECC mask: active code-table band on the reference
CONTENT_Y_MAX = 1590
RNG_SEED = 8342026


@dataclass
class Alignment:
    color: np.ndarray
    gray: np.ndarray
    rotation_ccw: int
    match_score: float
    orb_inlier_ratio: float


class LockedTemplate:
    """Alignment + locked catalog access. No code-selection logic."""

    def __init__(self, catalog: dict, reference_path: str,
                 match_min: float = MATCH_MIN):
        self.catalog = catalog
        self.ref = cv2.imread(str(reference_path), cv2.IMREAD_GRAYSCALE)
        if self.ref is None:
            raise RuntimeError(f"locked reference could not be decoded: {reference_path}")
        self.match_min = float(match_min)
        self.h, self.w = self.ref.shape
        self.cells = catalog["cells"]
        self.by_code = {c["code"]: c for c in self.cells}

        self._orb = cv2.ORB_create(nfeatures=4500, fastThreshold=10)
        self._ref_kp, self._ref_des = self._orb.detectAndCompute(self.ref, None)
        self._matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        self._ecc_ref = cv2.GaussianBlur(self.ref, (5, 5), 0).astype(np.float32) / 255.0
        self._ecc_mask = np.zeros_like(self.ref, dtype=np.uint8)
        self._ecc_mask[CONTENT_Y_MIN:min(CONTENT_Y_MAX, self.h),
                       8:min(self.w - 17, self.w)] = 255

    # ------------------------------------------------------------------
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
            g = cv2.warpAffine(gray, warp, (self.w, self.h),
                               flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
                               borderValue=255)
            c = cv2.warpAffine(color, warp, (self.w, self.h),
                               flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
                               borderValue=(255, 255, 255))
            return g, c, float(cc)
        except cv2.error:
            return gray, color, 0.0

    @staticmethod
    def _rot(img: np.ndarray, deg: int) -> np.ndarray:
        return img.copy() if deg == 0 else np.rot90(img, deg // 90).copy()

    def align(self, image) -> Alignment:
        cv2.setRNGSeed(RNG_SEED)
        color0 = image.copy() if isinstance(image, np.ndarray) \
            else cv2.imread(str(image), cv2.IMREAD_COLOR)
        if color0 is None:
            raise RuntimeError(f"cannot read page image: {image}")
        gray0 = cv2.cvtColor(color0, cv2.COLOR_BGR2GRAY)

        rotations = (90, 270) if gray0.shape[0] > gray0.shape[1] else (0, 180)
        ref_edges = cv2.Canny(self.ref, 50, 150)
        ref_d = cv2.dilate(ref_edges, np.ones((3, 3), np.uint8))
        scores = []
        for deg0 in rotations:
            g0 = cv2.resize(self._rot(gray0, deg0), (self.w, self.h),
                            interpolation=cv2.INTER_AREA)
            e0 = cv2.Canny(g0, 50, 150)
            e0d = cv2.dilate(e0, np.ones((3, 3), np.uint8))
            a = float(((e0 > 0) & (ref_d > 0)).sum()) / max(1, int((e0 > 0).sum()))
            b = float(((ref_edges > 0) & (e0d > 0)).sum()) / max(1, int((ref_edges > 0).sum()))
            scores.append(((a + b) / 2.0, deg0))
        _, deg = max(scores)

        g, c, orb_ratio = self._orb_align(self._rot(gray0, deg), self._rot(color0, deg))
        g, c, score = self._ecc_refine(g, c)
        return Alignment(color=c, gray=g, rotation_ccw=deg,
                         match_score=float(score), orb_inlier_ratio=float(orb_ratio))

    def is_match(self, alignment: Alignment) -> bool:
        return alignment.match_score >= self.match_min
