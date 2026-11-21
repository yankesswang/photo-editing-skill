"""Derive a slider recipe that moves a photo toward a reference photo's look.

Works on the *distribution* (tones + colour cast per tonal zone + saturation),
not on content, so the reference can be any photo whose grading you like.
Output is an ordinary recipe (curves + color grade + saturation), so it can be
tweaked, saved as a style, or exported to Lightroom.
"""
from __future__ import annotations

import math

import numpy as np

import engine as E

QS = [0.5, 5, 20, 40, 60, 80, 95, 99.5]
ZONES = {"shadows": (0.0, 0.33), "midtones": (0.33, 0.66), "highlights": (0.66, 1.01)}


def _stats(img):
    L = E.luminance(img)
    q = np.percentile(L, QS)
    hsv = E.rgb_to_hsv(img)
    sat = float(hsv[..., 1][(L > 0.08) & (L < 0.95)].mean())
    chroma = img - L[..., None]
    # grading tint is measured on near-neutral pixels only, so the reference's
    # *content* colours (orange leaves, blue sky) don't leak into the recipe
    neutral = hsv[..., 1] < 0.28
    zones = {}
    for z, (lo, hi) in ZONES.items():
        m = (L >= lo) & (L < hi) & neutral
        zones[z] = np.median(chroma[m], 0) if m.mean() > 0.005 else np.zeros(3, np.float32)
    return q, sat, zones


def _vec_to_hue_sat(v, weight):
    mag = float(np.abs(v).max())
    if mag < 1e-4:
        return 0, 0
    rgb = np.clip(0.5 + v / mag * 0.5, 0, 1).astype(np.float32)
    hue = float(E.rgb_to_hsv(rgb[None, None, :])[0, 0, 0])
    sat = mag / (0.22 * max(weight, 0.15)) * 100
    return int(round(hue)) % 360, int(round(min(sat, 30)))


def match_recipe(image_path, ref_paths, strength=0.8):
    img = E.load_image(image_path, 640)
    refs = [E.load_image(p, 640) for p in ref_paths]
    rq = np.mean([_stats(r)[0] for r in refs], 0)
    rsat = float(np.mean([_stats(r)[1] for r in refs]))
    rz = {z: np.mean([_stats(r)[2][z] for r in refs], 0) for z in ZONES}

    recipe = {"name": "matched", "description": f"matched to {', '.join(map(str, ref_paths))}"}
    # 1) tone curve: luminance quantile mapping (blended toward identity by strength)
    q, sat, _ = _stats(img)
    mid = QS.index(40) + 1  # ~median anchor (40th & 60th percentiles averaged)
    t_mid, r_mid = (q[3] + q[4]) / 2, (rq[3] + rq[4]) / 2
    # transfer the *shape* around the median (contrast, matte/fade), keep this photo's brightness
    want = t_mid + (rq - r_mid)
    want[0] = rq[0]   # black point / fade level transfers directly
    want[-1] = rq[-1]  # white point / highlight roll-off
    ys_mid = q + (want - q) * strength
    xs = [0.0] + list(q) + [1.0]
    ys = [rq[0] * strength * 0.9] + list(ys_mid) + [1.0 - (1 - rq[-1]) * strength]
    pts, last_x, last_y = [], -1, -1
    for x, y in zip(xs, ys):
        x, y = int(round(x * 255)), int(round(np.clip(y, 0, 1) * 255))
        if x <= last_x + 6:
            continue
        y = max(y, last_y)
        pts.append([x, y]); last_x, last_y = x, y
    if pts[-1][0] != 255:
        pts.append([255, max(pts[-1][1], 235)])
    recipe["tone_curve"] = {"master": pts}

    # 2) saturation + 3) per-zone colour cast, refined twice against the render
    recipe["saturation"] = 0
    recipe["color_grade"] = {"blending": 50, "balance": 0}
    for _ in range(3):
        out = E.develop(img, recipe)
        q2, sat2, z2 = _stats(out)
        if sat2 > 1e-3:
            recipe["saturation"] = int(np.clip(recipe["saturation"] + (rsat / sat2 - 1) * 60 * strength, -40, 25))
        for z in ZONES:
            cur = recipe["color_grade"].get(z, {"hue": 0, "sat": 0})
            cur_vec = E.hue_vec(cur["hue"]) * cur["sat"] / 100 * 0.22 * 0.6 if cur["sat"] else np.zeros(3)
            want = cur_vec + (rz[z] - z2[z]) * strength
            h, s = _vec_to_hue_sat(want, 0.6)
            recipe["color_grade"][z] = {"hue": h, "sat": s}
    return recipe
