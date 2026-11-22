#!/usr/bin/env python3
"""Remove people by borrowing REAL pixels from another frame of the same scene (multi-frame fill).

When you shot a burst (or a second frame a few seconds apart) the person standing in front of
the bench in frame B is usually somewhere else in frame A. Aligning A onto B and copying the
covered area gives true background — no hallucination, unlike single-image inpainting.

  python3 scripts/fill.py TARGET.JPG --source OTHER.JPG --keep 0 -o out.JPG
  python3 scripts/fill.py TARGET.JPG --source A.JPG --source B.JPG --remove 1 -o out.JPG

Pipeline
  1. people() on target (and every source) — YOLOv8-seg, same ids as `pe.py people`
  2. align each source -> target: ORB features on static background only (people masked out),
     ratio test, RANSAC homography; rejected if too few inliers or large reprojection error
  3. per target pixel to fill, take the first source where that pixel is valid (inside the frame
     and not covered by a person in the source)
  4. colour-match the source to the target on a ring around the hole (per-channel gain/offset),
     feather-blend; anything no source could cover falls back to LaMa inpainting (erase.inpaint)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine as E  # noqa: E402
import erase as R  # noqa: E402


def _people_mask(img, ps, grow=0.04):
    return R.person_mask(img.shape, ps, grow) if ps else np.zeros(img.shape[:2], np.uint8)


def align(src, dst, src_people, dst_people, work=1600, roi=None):
    """Homography mapping src -> dst, estimated on the static background (optionally only inside
    `roi` = (x0, y0, x1, y1) in target pixels: a LOCAL fit for the depth layer around the hole). Returns (H, stats)."""
    h, w = dst.shape[:2]
    s = work / max(h, w)
    g = lambda im: cv2.cvtColor((cv2.resize(im, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) * 255)
                                .astype(np.uint8), cv2.COLOR_RGB2GRAY)
    gs, gd = g(src), g(dst)
    ms = cv2.resize(255 - _people_mask(src, src_people), (gs.shape[1], gs.shape[0]), interpolation=cv2.INTER_NEAREST)
    md = cv2.resize(255 - _people_mask(dst, dst_people), (gd.shape[1], gd.shape[0]), interpolation=cv2.INTER_NEAREST)
    if roi is not None:
        x0, y0, x1, y1 = [int(v * s) for v in roi]
        r = np.zeros_like(md); r[max(0, y0):y1, max(0, x0):x1] = 255
        md = cv2.bitwise_and(md, r)
    orb = cv2.ORB_create(8000, scaleFactor=1.2, nlevels=8)
    ks, ds = orb.detectAndCompute(gs, ms)
    kd, dd = orb.detectAndCompute(gd, md)
    if ds is None or dd is None or len(ks) < 50 or len(kd) < 50:
        return None, {"reason": "too few features"}
    matches = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(ds, dd, k=2)
    good = [m for m, n in (p for p in matches if len(p) == 2) if m.distance < 0.75 * n.distance]
    if len(good) < 40:
        return None, {"reason": f"only {len(good)} good matches"}
    ps_ = np.float32([ks[m.queryIdx].pt for m in good])
    pd_ = np.float32([kd[m.trainIdx].pt for m in good])
    Hs, inl = cv2.findHomography(ps_, pd_, cv2.RANSAC, 3.0)
    if Hs is None:
        return None, {"reason": "homography failed"}
    inl = inl.ravel().astype(bool)
    err = np.linalg.norm(cv2.perspectiveTransform(ps_[inl][None], Hs)[0] - pd_[inl], axis=1)
    stats = {"matches": len(good), "inliers": int(inl.sum()), "reproj_px": round(float(err.mean() / s), 2)}
    if inl.sum() < 30:
        return None, {**stats, "reason": "too few inliers"}
    S = np.diag([s, s, 1.0])
    return np.linalg.inv(S) @ Hs @ S, stats


def refine_flow(warped, target, hole, work=1500):
    """Fix parallax left by a single homography (near bench vs far wall move differently):
    dense DIS optical flow target->warped on the background, flow inside the hole (occluded in
    the target, so unmeasurable there) is completed from its surroundings, then warped is remapped."""
    h, w = target.shape[:2]
    s = work / max(h, w)
    sw, sh = int(w * s), int(h * s)
    g = lambda im: cv2.cvtColor((np.clip(cv2.resize(im, (sw, sh), interpolation=cv2.INTER_AREA), 0, 1) * 255)
                                .astype(np.uint8), cv2.COLOR_RGB2GRAY)
    dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
    flow = dis.calc(g(target), g(warped), None)  # target pixel -> where it is in warped
    hs = cv2.resize(hole, (sw, sh), interpolation=cv2.INTER_NEAREST)
    bad = cv2.dilate((hs > 0).astype(np.uint8), np.ones((9, 9), np.uint8))
    # complete the flow field inside the hole from the ring (scaled to 8-bit per channel for cv2.inpaint)
    done = flow.copy()
    for c in range(2):
        f = flow[..., c]
        lo, hi = float(np.percentile(f, 1)), float(np.percentile(f, 99))
        rng = max(hi - lo, 1e-3)
        u8 = (np.clip((f - lo) / rng, 0, 1) * 255).astype(np.uint8)
        filled = cv2.inpaint(u8, bad, 25, cv2.INPAINT_TELEA).astype(np.float32) / 255 * rng + lo
        done[..., c] = np.where(bad > 0, filled, f)
    done = cv2.GaussianBlur(done, (0, 0), 3)
    full = cv2.resize(done, (w, h), interpolation=cv2.INTER_LINEAR) / s
    gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    return cv2.remap(warped.astype(np.float32), gx + full[..., 0], gy + full[..., 1], cv2.INTER_CUBIC,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=(-1, -1, -1))


def _color_match(src, dst, ring):
    out = src.copy()
    m = ring > 0
    if m.sum() < 500:
        return out
    for c in range(3):
        a, b = src[..., c][m], dst[..., c][m]
        gain = np.clip(b.std() / max(a.std(), 1e-4), 0.7, 1.4)
        out[..., c] = (src[..., c] - a.mean()) * gain + b.mean()
    return np.clip(out, 0, 1)


def fill(target, sources, spec, grow=0.08, feather=0.004, use_flow=False):
    """target/sources: float sRGB images (sources any size of the same scene). Returns (out, report)."""
    h, w = target.shape[:2]
    tps = R.people(target)
    chosen = R.select(tps, spec)
    report = {"people": len(tps), "removed": [p["id"] for p in chosen], "sources": []}
    if not chosen:
        return target, report
    hole = R.person_mask(target.shape, chosen, grow)          # what must be replaced
    keep = R.person_mask(target.shape, [p for p in tps if p not in chosen], 0.0)
    hole[keep > 0] = 0                                        # never paint over the subject
    todo = hole > 0
    out = target.copy()
    acc = np.zeros((h, w), np.float32)                        # filled coverage
    for si, src in enumerate(sources):
        sps = R.people(src)
        H, st = align(src, target, sps, tps)
        # local fit around the hole: a single global homography locks onto the far buildings and
        # misplaces near objects (parallax); features from the hole's neighbourhood follow its layer
        ys, xs = np.nonzero(hole)
        bw_, bh_ = xs.max() - xs.min(), ys.max() - ys.min()
        roi = (xs.min() - 0.6 * bw_, ys.min() - 0.25 * bh_, xs.max() + 0.6 * bw_, ys.max() + 0.25 * bh_)
        Hl, stl = align(src, target, sps, tps, roi=roi)
        if Hl is not None and stl.get("inliers", 0) >= 40:
            H, st = Hl, {**stl, "fit": "local"}
        else:
            st = {**st, "fit": "global"}
        st["source"] = si
        report["sources"].append(st)
        if H is None:
            continue
        warped = cv2.warpPerspective(src, H, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=-1)
        if use_flow:  # experimental: tends to warp straight structures on large holes
            warped = refine_flow(np.where(warped < 0, 0, warped), target, hole)
        inside = warped[..., 0] >= 0
        src_people = cv2.warpPerspective(_people_mask(src, sps, 0.06), H, (w, h), flags=cv2.INTER_NEAREST)
        valid = todo & inside & (src_people == 0) & (acc < 0.5)
        if valid.sum() == 0:
            continue
        ring = cv2.dilate(hole, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(0.03 * w) | 1,) * 2))
        ring = (ring > 0) & ~(hole > 0) & inside & (keep == 0)
        warped = _color_match(np.where(warped < 0, 0, warped), target, ring.astype(np.uint8))
        sigma = max(1.0, feather * max(h, w))
        a = cv2.GaussianBlur(valid.astype(np.float32), (0, 0), sigma)
        a = np.maximum(a, valid.astype(np.float32)) * inside
        a[keep > 0] = 0
        out = out * (1 - a[..., None]) + warped * a[..., None]
        acc = np.maximum(acc, valid.astype(np.float32))
        st["filled_px"] = int(valid.sum())
        # post-check: how well does the aligned source agree with the target just outside the hole?
        if ring.any():
            st["ring_diff"] = round(float(np.abs(warped[ring] - target[ring]).mean()), 4)
    left = (todo & (acc < 0.5)).astype(np.uint8) * 255
    report["fallback_px"] = int((left > 0).sum())
    report["hole_px"] = int(todo.sum())
    report["real_pct"] = round(100 * (1 - report["fallback_px"] / max(report["hole_px"], 1)), 1)
    if left.any():  # nothing in any source covers it -> inpaint just that bit
        out = R.inpaint(out, left)
    out = np.clip(out, 0, 1)
    # safety: a person pasted from a source (same spot in both frames, partial source mask) shows up
    # again inside the hole -> flag it instead of handing over a ghost
    warn = []
    ghosts = [p for p in R.people(out)
              if (R.person_mask(out.shape, [p], 0.0)[hole > 0] > 0).mean() > 0.3]
    if ghosts:
        warn.append(f"{len(ghosts)} person-like shape(s) inside the filled area (pasted from a source?)")
    if report["real_pct"] < 50:
        warn.append(f"only {report['real_pct']}% of the hole came from real pixels; the rest is LaMa")
    bad = [st for st in report["sources"] if st.get("ring_diff", 0) > 0.05]
    if bad:
        warn.append("source(s) " + ",".join(str(st["source"]) for st in bad) + " misaligned or relit (ring_diff > 0.05)")
    report["warnings"] = warn
    return out, report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target")
    ap.add_argument("--source", action="append", required=True, help="other frame(s) of the same scene")
    ap.add_argument("--remove", default="bystanders", help="'bystanders', 'all' or ids '1,3'")
    ap.add_argument("--keep", help="ids to keep; everyone else is removed")
    ap.add_argument("--grow", type=float, default=0.08)
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args()
    spec = ({"keep": [int(x) for x in a.keep.split(",")]} if a.keep else
            a.remove if a.remove in ("bystanders", "all") else [int(x) for x in a.remove.split(",")])
    tgt = E.load_image(a.target)
    srcs = [E.load_image(s) for s in a.source]
    out, rep = fill(tgt, srcs, spec, a.grow)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    E.save_image(out, a.out, a.target, 95)
    print(rep)
    print(a.out)


if __name__ == "__main__":
    main()
