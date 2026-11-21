"""Per-photo analysis + conservative 'Auto' corrections (the 'fix it first' layer).

A photographer corrects each frame (exposure, white balance, highlight/shadow
recovery, noise) *before* applying a look. This module measures the photo and
proposes that correction recipe; the style is stacked on top afterwards.
"""
from __future__ import annotations

import math

import cv2
import numpy as np
from PIL import ExifTags, Image

import engine as E


def read_exif(path):
    out = {}
    try:
        im = Image.open(path)
        ex = im.getexif()
        sub = ex.get_ifd(0x8769)
        tags = {**{ExifTags.TAGS.get(k, k): v for k, v in ex.items()},
                **{ExifTags.TAGS.get(k, k): v for k, v in sub.items()}}
        for k in ("Model", "LensModel", "DateTimeOriginal", "ExposureTime", "FNumber",
                  "ISOSpeedRatings", "FocalLength", "ExposureBiasValue", "Flash"):
            v = tags.get(k)
            if v is None:
                continue
            try:
                v = float(v) if k in ("ExposureTime", "FNumber", "FocalLength", "ExposureBiasValue") else v
            except Exception:
                v = str(v)
            out[k] = v if isinstance(v, (int, float, str)) else str(v)
    except Exception:
        pass
    return out


_FACE = None


def find_faces(img):
    global _FACE
    try:
        if _FACE is None:
            _FACE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        g = cv2.cvtColor((img * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
        g = cv2.equalizeHist(g)
        faces = _FACE.detectMultiScale(g, scaleFactor=1.12, minNeighbors=7,
                                       minSize=(max(24, g.shape[1] // 25),) * 2)
        return [tuple(int(v) for v in f) for f in faces]
    except Exception:
        return []


def metering(Lg):
    """Center-weighted mean of non-blown pixels (like a camera's matrix/centre meter)."""
    h, w = Lg.shape
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    wgt = np.exp(-(((x / w - 0.5) / 0.35) ** 2 + ((y / h - 0.55) / 0.35) ** 2))
    wgt = wgt * (Lg < 0.93)
    return float((Lg * wgt).sum() / max(wgt.sum(), 1e-6))


def solve_wb(mean_lin):
    """temp/tint (engine units) that make mean_lin neutral."""
    best, bt, bg = 1e9, 0.0, 0.0
    for t in np.arange(-80, 81, 2.0):
        for g in np.arange(-60, 61, 2.0):
            v = E.white_balance(mean_lin[None, None, :].astype(np.float32), t, g)[0, 0]
            v = v / max(v.mean(), 1e-6)
            err = float(((v - 1) ** 2).sum())
            if err < best:
                best, bt, bg = err, t, g
    return bt, bg


_CACHE = {}


def analyze(path, img=None):
    key = str(path)
    if key in _CACHE:
        return _CACHE[key]
    res = _analyze(path, img)
    _CACHE[key] = res
    return res


def _analyze(path, img=None):
    img = img if img is not None else E.load_image(path, 900)
    exif = read_exif(path)
    lin = E.srgb_to_linear(img)
    Y = E.luminance(lin)
    Lg = E.linear_to_srgb(Y)
    q = {p: float(np.quantile(Lg, p / 100)) for p in (0.5, 2, 10, 25, 50, 75, 90, 98, 99.5)}
    hsv = E.rgb_to_hsv(img)
    sat = hsv[..., 1]
    clip_hi = float((img.max(-1) > 0.985).mean())
    clip_lo = float((Lg < 0.02).mean())
    mean_sat = float(sat[(Lg > 0.1) & (Lg < 0.95)].mean()) if ((Lg > 0.1) & (Lg < 0.95)).any() else 0.0
    iso = exif.get("ISOSpeedRatings") or 100
    try:
        iso = int(iso if not isinstance(iso, (tuple, list)) else iso[0])
    except Exception:
        iso = 100

    # ---- scene tags
    tags = []
    dr = q[98] - q[2]
    if q[50] < 0.22 and (iso >= 800 or q[90] < 0.6):
        tags.append("night/low-light")
    if q[50] > 0.62:
        tags.append("high-key/bright")
    if dr < 0.55:
        tags.append("flat/foggy/overcast")
    if clip_hi > 0.01 and q[10] < 0.12:
        tags.append("backlit/high-contrast")
    if iso >= 1600:
        tags.append(f"high-iso({iso})")
    sky = hsv[: img.shape[0] // 3]
    if ((sky[..., 0] > 190) & (sky[..., 0] < 250) & (sky[..., 1] > 0.25)).mean() > 0.25:
        tags.append("blue-sky")
    if ((hsv[..., 0] > 70) & (hsv[..., 0] < 160) & (hsv[..., 1] > 0.25)).mean() > 0.25:
        tags.append("foliage/green")
    if ((hsv[..., 0] > 15) & (hsv[..., 0] < 50) & (hsv[..., 1] > 0.35)).mean() > 0.2:
        tags.append("warm/autumn-or-tungsten")
    faces = find_faces(img)
    if faces:
        tags.append(f"people({len(faces)} face)")

    # ---- correction recipe
    night = "night/low-light" in tags
    corr = {}
    lin_of = lambda v: max(float(E.srgb_to_linear(np.float32(v))), 1e-4)
    meter = metering(Lg)
    if faces:
        face_l = float(np.mean([np.median(Lg[y:y + h, x:x + w]) for x, y, w, h in faces]))
        ev = math.log2(lin_of(0.56) / lin_of(face_l)) * 0.8
        meter_note = f"face {face_l:.2f}"
    else:
        target = 0.30 if night else 0.45
        ev = math.log2(lin_of(target) / lin_of(meter)) * 0.55
        meter_note = f"centre {meter:.2f}"
    ev = math.copysign(max(0.0, abs(ev) - 0.15), ev)  # dead-band: leave decent exposures alone
    if ev > 0:
        ev *= max(0.3, 1 - clip_hi * 15)  # don't blow already-clipped scenes
    corr["exposure"] = round(float(np.clip(ev, -0.8, 1.2)), 2)

    hl = -15.0
    if q[99.5] > 0.96 or clip_hi > 0.004:
        hl = -min(85.0, 30 + clip_hi * 1500)
    sh = 0.0
    if q[10] < 0.14:
        sh = min(55.0, (0.14 - q[10]) * 350 + 10)
    if night:
        sh *= 0.5
    corr["highlights"] = round(hl)
    corr["shadows"] = round(sh)
    corr["whites"] = round(float(np.clip((0.965 - q[99.5]) * 150, -30, 30)))
    corr["blacks"] = round(float(np.clip((0.035 - q[0.5]) * 220, -35, 20)))
    if dr < 0.55 and not night:
        corr["contrast"] = round(min(25.0, (0.55 - dr) * 80))
        corr["dehaze"] = round(min(20.0, (0.55 - dr) * 60))

    # white balance from near-neutral midtones (keep part of the cast = keep the mood)
    neutral = (sat < 0.22) & (Lg > 0.15) & (Lg < 0.92)
    if neutral.mean() > 0.04:
        m = lin[neutral].mean(0)
        t, g = solve_wb(m)
        keep = 0.35 if night or "warm/autumn-or-tungsten" in tags else 0.55
        corr["temp"] = round(t * keep)
        corr["tint"] = round(g * keep)
    else:
        # no neutral reference (e.g. a room lit only by tungsten): grey-world on midtones,
        # applied lightly so the scene stays warm but stops looking like a colour filter
        mids = (Lg > 0.1) & (Lg < 0.9)
        if mids.mean() > 0.2:
            t, g = solve_wb(lin[mids].mean(0))
            if abs(t) > 25 or abs(g) > 20:
                corr["temp"] = round(t * 0.3)
                corr["tint"] = round(g * 0.3)

    # noise / sharpening from ISO
    if iso >= 3200:
        corr["noise"] = {"luminance": 35, "color": 45}
        corr["sharpen"] = {"amount": 20, "radius": 1.0, "masking": 60}
    elif iso >= 1000:
        corr["noise"] = {"luminance": 20, "color": 35}
        corr["sharpen"] = {"amount": 25, "radius": 1.0, "masking": 40}
    else:
        corr["noise"] = {"luminance": 0, "color": 15}
        corr["sharpen"] = {"amount": 35, "radius": 1.0, "masking": 20}

    return {
        "file": str(path),
        "size": [int(img.shape[1]), int(img.shape[0])],
        "exif": exif,
        "stats": {
            "luma_percentiles": {str(k): round(v, 3) for k, v in q.items()},
            "dynamic_range_p2_p98": round(dr, 3),
            "clipped_highlights_pct": round(clip_hi * 100, 2),
            "crushed_shadows_pct": round(clip_lo * 100, 2),
            "mean_saturation": round(mean_sat, 3),
            "metering": meter_note,
        },
        "tags": tags,
        "auto_correction": corr,
    }
