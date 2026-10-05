"""Optional OpenCV reference registration with explicit failure.

Carinthia-S has no repeated-pattern reference pairs, so ``plan_reference_registration`` reports
that registration is not applicable instead of fabricating references. ``register`` is the API for
data that does have references; it never returns a silent identity transform on failure.
"""

import numpy as np

from sem_images.model import BackendUnavailable


class RegistrationFailed(RuntimeError):
    pass


def import_cv2():
    try:
        import cv2
    except ImportError as exc:
        raise BackendUnavailable("OpenCV (cv2) is optional and not installed in the SEM env; registration "
                                 "is unavailable. Install opencv-python-headless in an isolated env to use it.") from exc
    return cv2


def plan_reference_registration(reference_pairs):
    if not reference_pairs:
        return {"status": "not_applicable", "pairs": 0,
                "reason": "no reference image pairs exist for this dataset; registration was not attempted"}
    return {"status": "ready", "pairs": len(reference_pairs)}


def register(image, reference, min_matches=12, max_reproj_px=3.0, min_inlier_ratio=0.5, cv2=None):
    """ORB + RANSAC homography from ``image`` to ``reference``. Raises RegistrationFailed with a reason."""
    cv2 = cv2 or import_cv2()
    to_u8 = lambda a: np.clip(np.asarray(a, dtype=np.float64) * (255 if a.max() <= 1 else 1), 0, 255).astype(np.uint8)
    img, ref = to_u8(image), to_u8(reference)
    orb = cv2.ORB_create(nfeatures=2000)
    k1, d1 = orb.detectAndCompute(img, None)
    k2, d2 = orb.detectAndCompute(ref, None)
    if d1 is None or d2 is None or len(k1) < min_matches or len(k2) < min_matches:
        raise RegistrationFailed("insufficient keypoints for registration")
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(d1, d2)
    if len(matches) < min_matches:
        raise RegistrationFailed(f"only {len(matches)} matches (< {min_matches})")
    src = np.float32([k1[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
    dst = np.float32([k2[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)
    h, inliers = cv2.findHomography(src, dst, cv2.RANSAC, max_reproj_px)
    if h is None or inliers is None:
        raise RegistrationFailed("homography estimation failed")
    ratio = float(inliers.sum()) / len(matches)
    if ratio < min_inlier_ratio:
        raise RegistrationFailed(f"inlier ratio {ratio:.2f} below {min_inlier_ratio}")
    return {"status": "registered", "homography": h.tolist(), "matches": len(matches), "inlier_ratio": ratio,
            "note": "Keep both original images; warping can interpolate away small defects."}
