"""Multi-frame noise reduction: align a handheld burst and merge it (night, indoor, high ISO).

Averaging N aligned frames cuts random noise by ~sqrt(N) without smearing detail, which no
single-frame denoiser can do. Moving things (people, cars, water) differ between frames; those
pixels fall back to the reference frame instead of turning into ghosts.

  pe.py stack F1.JPG F2.JPG F3.JPG ... -o merged.jpg [--ref F2.JPG]

Pipeline
  1. reference = the sharpest frame (variance of Laplacian), unless --ref is given
  2. align every other frame onto it: ORB features + RANSAC homography on a brightened grey copy
     (night frames are too dark for the detector otherwise); frames that don't align are skipped
  3. per frame: exposure gain matched to the reference, then a DIS optical-flow pass removes the
     residual parallax the homography leaves (1-6 px indoors)
  4. merge: per pixel, each frame's weight falls off with its difference from the reference,
     measured on a blurred copy, scaled by the pair's median difference (ghost rejection)
  5. the merged result is a normal image: develop it like any other (auto + style); its noise
     is lower, so use less noise reduction
"""
from __future__ import annotations

import cv2
import numpy as np

import engine as E


def sharpness(img):
    g = cv2.cvtColor(cv2.resize(img, None, fx=1200 / max(img.shape[:2]), fy=1200 / max(img.shape[:2]),
                                interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
    return float(cv2.Laplacian(g, cv2.CV_32F).var())


def _grey(img, s):
    small = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    g = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    g = g / max(float(np.percentile(g, 99)), 1e-3)  # brighten dark frames so ORB finds corners
    return (np.clip(g, 0, 1) ** 0.6 * 255).astype(np.uint8)


def align(src, ref, work=2000):
    """Homography src -> ref, or (None, reason)."""
    h, w = ref.shape[:2]
    s = work / max(h, w)
    gs, gr = _grey(src, s), _grey(ref, s)
    orb = cv2.ORB_create(10000, scaleFactor=1.2, nlevels=8, fastThreshold=10)
    ks, ds = orb.detectAndCompute(gs, None)
    kr, dr = orb.detectAndCompute(gr, None)
    if ds is None or dr is None or len(ks) < 80 or len(kr) < 80:
        return None, {"reason": "too few features"}
    matches = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(ds, dr, k=2)
    good = [m for m, n in (p for p in matches if len(p) == 2) if m.distance < 0.75 * n.distance]
    if len(good) < 60:
        return None, {"reason": f"only {len(good)} matches"}
    ps = np.float32([ks[m.queryIdx].pt for m in good])
    pr = np.float32([kr[m.trainIdx].pt for m in good])
    H, inl = cv2.findHomography(ps, pr, cv2.RANSAC, 2.0)
    if H is None or inl.sum() < 40:
        return None, {"reason": "homography failed"}
    inl = inl.ravel().astype(bool)
    err = np.linalg.norm(cv2.perspectiveTransform(ps[inl][None], H)[0] - pr[inl], axis=1)
    st = {"inliers": int(inl.sum()), "reproj_px": round(float(err.mean()), 2)}  # at work scale
    if st["reproj_px"] > 2.0:
        return None, {**st, "reason": "poor fit (parallax / rolling shutter)"}
    # features land within ~1 px at work scale; stacking needs sub-pixel at full res -> ECC refine
    # (intensity-based, uses every pixel; a person who moved just lowers the correlation)
    try:
        Hf = H.astype(np.float32)
        crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5)
        cc, Hf = cv2.findTransformECC(gr.astype(np.float32) / 255, gs.astype(np.float32) / 255,
                                      np.linalg.inv(Hf).astype(np.float32), cv2.MOTION_HOMOGRAPHY, crit, None, 3)
        H = np.linalg.inv(Hf)
        st["ecc"] = round(float(cc), 3)
    except cv2.error:
        st["ecc"] = None
    S = np.diag([s, s, 1.0])
    return np.linalg.inv(S) @ H @ S, st


def refine_flow(warped, ref, work=2000):
    """Residual per-pixel alignment after the homography: indoor / near-far scenes leave 1-6 px of
    parallax that no single global transform removes. DIS optical flow ref -> warped, smoothed."""
    h, w = ref.shape[:2]
    s = work / max(h, w)
    g = lambda im: (np.clip(cv2.resize(cv2.cvtColor(im, cv2.COLOR_RGB2GRAY), None, fx=s, fy=s,
                                       interpolation=cv2.INTER_AREA), 0, 1) ** 0.6 * 255).astype(np.uint8)
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    fl = cv2.GaussianBlur(dis.calc(g(ref), g(warped), None), (0, 0), 2)
    full = cv2.resize(fl, (w, h), interpolation=cv2.INTER_LINEAR) / s
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    return cv2.remap(warped, gx + full[..., 0], gy + full[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


def noise_sigma(img):
    """Per-pixel noise std estimate (MAD of a fine high-pass on the green channel)."""
    g = img[..., 1]
    hp = g - cv2.GaussianBlur(g, (0, 0), 1.0)
    return max(float(np.median(np.abs(hp))) * 1.4826 * 1.6, 1e-3)


def merge(frames, ref_idx=None, ghost=2.5):
    """frames: list of float sRGB images of one scene. Returns (merged, report)."""
    if ref_idx is None:
        ref_idx = int(np.argmax([sharpness(f) for f in frames]))
    ref = frames[ref_idx]
    h, w = ref.shape[:2]
    sig = noise_sigma(ref)
    acc = ref.astype(np.float32).copy()
    wsum = np.ones((h, w, 1), np.float32)
    refb = cv2.GaussianBlur(ref, (0, 0), 2.0)
    rep = {"ref": ref_idx, "noise_sigma": round(sig, 4), "frames": []}
    for i, f in enumerate(frames):
        if i == ref_idx:
            continue
        if f.shape[:2] != (h, w):
            f = cv2.resize(f, (w, h), interpolation=cv2.INTER_AREA)
        H, st = align(f, ref)
        st["frame"] = i
        if H is None:
            rep["frames"].append({**st, "used": False})
            continue
        warped = cv2.warpPerspective(f, H, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                                     borderValue=(-1, -1, -1))
        inside = (warped[..., 0] >= 0).astype(np.float32)
        inside = cv2.erode(inside, np.ones((9, 9), np.uint8))
        warped = np.where(warped < 0, ref, warped)
        # exposure drifts between burst frames (auto exposure): match per channel on the overlap
        ins = inside > 0
        gain = np.median(ref[ins], 0) / np.maximum(np.median(warped[ins], 0), 1e-4)
        st["gain"] = [round(float(x), 3) for x in gain]
        if np.any(np.abs(gain - 1) > 0.35):
            rep["frames"].append({**st, "used": False, "reason": "exposure too different"})
            continue
        warped = refine_flow(np.clip(warped * gain, 0, 1).astype(np.float32), ref)
        # ghost rejection on a blurred difference. Camera JPG noise is blotchy (low-frequency after
        # in-camera NR), so a threshold from the per-pixel noise alone rejects nearly everything:
        # use the pair's own median difference (most of a burst is static) as the scale.
        d = np.abs(cv2.GaussianBlur(warped, (0, 0), 2.0) - refb).max(-1)
        tau = float(np.clip(np.median(d[ins]) * ghost, sig * 0.5, 0.06))
        wgt = np.exp(-(d / tau) ** 2) * inside
        wgt = cv2.GaussianBlur(wgt, (0, 0), 1.5)[..., None]
        acc += warped * wgt
        wsum += wgt
        st["used"] = True
        st["static_pct"] = round(float((wgt > 0.5).mean() * 100), 1)
        rep["frames"].append(st)
    out = acc / wsum
    used = 1 + sum(1 for st in rep["frames"] if st.get("used"))
    rep["used"] = used
    rep["noise_after"] = round(noise_sigma(out.astype(np.float32)), 4)
    return np.clip(out, 0, 1).astype(np.float32), rep
