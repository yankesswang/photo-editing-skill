"""Local adjustments (Lightroom 'Masking'): build soft masks and apply a local recipe through them.

A mask spec (all coordinates normalised 0..1, x to the right, y down):

  {"type": "linear",    "start": [0.5, 0.0], "end": [0.5, 0.45]}      # graduated filter: full at start, 0 at end
  {"type": "radial",    "center": [0.4, 0.5], "size": [0.2, 0.3], "feather": 50}   # ellipse radii as frac of W/H
  {"type": "luminance", "range": [0.0, 0.3], "feather": 0.1}           # gamma-encoded brightness band
  {"type": "color",     "hue": 220, "range": 35, "min_sat": 0.15}      # hue in degrees (HSV)
  {"type": "face",      "scale": 1.8}                                  # every detected face (head incl. hair)
  {"type": "subject",   "scale": 1.0}                                  # face-anchored upper-body ellipse(s)
  {"type": "sky"}                                                      # bright, smooth region connected to the top edge
  {"type": "boxes",     "boxes": [[x, y, w, h], ...], "feather": 40}   # explicit ellipses in given boxes
  {"type": "matte",     "name": "skin"}   # iPhone semantic matte: portrait|skin|hair|teeth|glasses|sky
                                          # (read from the <stem>.mattes/ sidecar `pe.py mattes` writes;
                                          #  no sidecar -> empty mask, so recipes stay safe on other cameras)

Common options: "invert": true, "amount": 0..1, "feather" (where relevant),
"intersect": [spec, ...]  (Lightroom 'intersect with'), "subtract": [spec, ...].

A mask entry in a recipe:
  {"name": "face lift", "mask": {...spec...}, "adjust": {"exposure": 0.5, "shadows": 20}}
Local adjust keys: temp, tint, exposure, contrast, highlights, shadows, whites, blacks,
clarity, texture, dehaze, saturation, vibrance, hsl.
"""
from __future__ import annotations

import cv2
import numpy as np

import engine as E

_FACE = None
_MATTES = {"dir": None, "shape": None, "recipe": None}


def set_mattes(d, pre_geom_shape, recipe):
    """Point 'matte' masks at an image's sidecar dir; shape/recipe let us replay crop/rotate on the matte."""
    _MATTES.update(dir=d, shape=pre_geom_shape, recipe=recipe)


_ORIENT = {2: [cv2.ROTATE_180], 3: [cv2.ROTATE_180], 4: [], 5: [cv2.ROTATE_90_CLOCKWISE],
           6: [cv2.ROTATE_90_CLOCKWISE], 7: [cv2.ROTATE_90_COUNTERCLOCKWISE], 8: [cv2.ROTATE_90_COUNTERCLOCKWISE]}
_FLIP = {2: 0, 4: 0, 5: 1, 7: 1}  # applied after rotation (cv2.flip code)


def _matte(name, h, w):
    import json
    from pathlib import Path
    d = _MATTES["dir"]
    p = Path(d) / f"{name}.png" if d else None
    if p is None or not p.exists():
        return np.zeros((h, w), np.float32)
    m = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE).astype(np.float32) / 255
    try:
        o = json.loads((Path(d) / "meta.json").read_text(encoding="utf-8-sig")).get("orientation", 1)
    except (OSError, ValueError):
        o = 1
    for rot in _ORIENT.get(o, []):
        m = cv2.rotate(m, rot)
    if o in _FLIP:
        m = cv2.flip(m, _FLIP[o])
    r = _MATTES["recipe"] or {}
    ph, pw = _MATTES["shape"] or (h, w)
    m = cv2.resize(m, (pw, ph), interpolation=cv2.INTER_LINEAR)
    if any(r.get(k) for k in ("rotate", "straighten", "crop")):
        m = E.geometry(np.repeat(m[..., None], 3, -1), r)[..., 0]
    if m.shape != (h, w):
        m = cv2.resize(m, (w, h), interpolation=cv2.INTER_LINEAR)
    return m


def detect_faces(img):
    """Haar faces on a downscaled copy; second CLAHE pass catches dark / backlit faces.
    Returns normalised boxes [x, y, w, h]."""
    global _FACE
    if _FACE is None:
        _FACE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    h, w = img.shape[:2]
    s = 900.0 / max(h, w)
    small = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA) if s < 1 else img
    g = cv2.cvtColor((np.clip(small, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2GRAY)
    sh, sw = g.shape
    minsz = (max(24, sw // 25),) * 2
    found = []
    for prep, neigh in ((cv2.equalizeHist(g), 7),
                        (cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(g), 5)):
        faces = _FACE.detectMultiScale(prep, scaleFactor=1.1, minNeighbors=neigh, minSize=minsz)
        for (x, y, fw, fh) in faces:
            box = [x / sw, y / sh, fw / sw, fh / sh]
            if all(_iou(box, b) < 0.3 for b in found):
                found.append(box)
        if found:
            break
    return found


def _iou(a, b):
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0.0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    return inter / max(a[2] * a[3] + b[2] * b[3] - inter, 1e-9)


def _grid(h, w):
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    return x / max(w - 1, 1), y / max(h - 1, 1)


def _ellipse(h, w, cx, cy, rx, ry, feather):
    x, y = _grid(h, w)
    d = np.sqrt(((x - cx) / max(rx, 1e-4)) ** 2 + ((y - cy) / max(ry, 1e-4)) ** 2)
    f = np.clip(feather / 100.0, 0.02, 1.0)
    return 1 - E.smoothstep(1 - f, 1.0, d)


def guided_refine(mask, img, radius_frac=0.008, eps=2e-3):
    """Edge-aware mask refinement (He et al. guided filter, gray guide, box filters):
    snaps a soft geometric/low-res mask to real object edges so local edits don't halo."""
    h, w = mask.shape
    s = min(1.0, 1600.0 / max(h, w))  # work at <=1600px, then upsample
    if s < 1:
        I = cv2.resize(E.luminance(img), (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
        p = cv2.resize(mask, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    else:
        I, p = E.luminance(img), mask
    r = max(2, int(max(I.shape) * radius_frac))
    box = lambda x: cv2.boxFilter(x, -1, (2 * r + 1, 2 * r + 1), borderType=cv2.BORDER_REFLECT)
    mI, mp = box(I), box(p)
    cov = box(I * p) - mI * mp
    var = box(I * I) - mI * mI
    a = cov / (var + eps)
    b = mp - a * mI
    q = box(a) * I + box(b)
    if s < 1:
        q = cv2.resize(q, (w, h), interpolation=cv2.INTER_LINEAR)
        # re-apply the fine luminance detail at full res via the upsampled linear model
    return np.clip(q, 0, 1).astype(np.float32)


def _sky(img):
    h, w = img.shape[:2]
    s = 480.0 / max(h, w)
    small = cv2.resize(img, (max(8, int(w * s)), max(8, int(h * s))), interpolation=cv2.INTER_AREA)
    L = E.linear_to_srgb(E.luminance(E.srgb_to_linear(small)))
    hsv = E.rgb_to_hsv(small)
    H, S = hsv[..., 0], hsv[..., 1]
    top = L[: max(2, L.shape[0] // 12)]
    thr = max(0.35, float(np.median(top)) - 0.22)
    mean = cv2.blur(L, (5, 5)); sq = cv2.blur(L * L, (5, 5))
    std = np.sqrt(np.maximum(sq - mean * mean, 0))
    foliage = (S > 0.25) & (H > 20) & (H < 170)
    cand = ((L > thr) & (std < 0.06) & ~foliage).astype(np.uint8)
    cand = cv2.morphologyEx(cand, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, lab = cv2.connectedComponents(cand)
    keep = np.zeros_like(cand)
    toprow = set(np.unique(lab[: max(2, lab.shape[0] // 20)])) - {0}
    for k in toprow:
        keep[lab == k] = 1
    m = cv2.GaussianBlur(keep.astype(np.float32), (0, 0), 3)
    m = cv2.resize(m, (w, h), interpolation=cv2.INTER_LINEAR)
    # pixel-accurate edge: dark structures (trees, roofs) cut through the soft mask
    Lf = E.linear_to_srgb(E.luminance(E.srgb_to_linear(img)))
    return np.clip(m * E.smoothstep(thr - 0.12, thr + 0.02, Lf), 0, 1)


def build_mask(spec, img, faces=None):
    h, w = img.shape[:2]
    t = spec.get("type")
    if t == "linear":
        (x0, y0), (x1, y1) = spec.get("start", [0.5, 0]), spec.get("end", [0.5, 0.5])
        x, y = _grid(h, w)
        dx, dy = (x1 - x0) * w, (y1 - y0) * h
        tt = ((x - x0) * w * dx + (y - y0) * h * dy) / max(dx * dx + dy * dy, 1e-6)
        m = 1 - E.smoothstep(0.0, 1.0, tt)
    elif t == "radial":
        cx, cy = spec.get("center", [0.5, 0.5]); rx, ry = spec.get("size", [0.25, 0.25])
        m = _ellipse(h, w, cx, cy, rx, ry, spec.get("feather", 50))
    elif t == "luminance":
        lo, hi = spec.get("range", [0.0, 0.3]); fe = max(spec.get("feather", 0.1), 1e-3)
        L = E.linear_to_srgb(E.luminance(E.srgb_to_linear(img)))
        m = E.smoothstep(lo - fe, lo, L) * (1 - E.smoothstep(hi, hi + fe, L))
        m = E.gblur(m.astype(np.float32), max(h, w) * 0.002)
    elif t == "color":
        hsv = E.rgb_to_hsv(img)
        d = np.abs(((hsv[..., 0] - spec.get("hue", 0) + 180) % 360) - 180)
        rng = spec.get("range", 30); ms = spec.get("min_sat", 0.15)
        m = (1 - E.smoothstep(rng * 0.6, rng, d)) * E.smoothstep(ms * 0.5, ms, hsv[..., 1])
        m = E.gblur(m.astype(np.float32), max(h, w) * 0.002)
    elif t in ("face", "subject", "boxes"):
        boxes = spec.get("boxes") if t == "boxes" else (faces if faces is not None else detect_faces(img))
        m = np.zeros((h, w), np.float32)
        sc = spec.get("scale", 1.8 if t == "face" else 1.0)
        for bx, by, bw, bh in boxes or []:
            cx, cy = bx + bw / 2, by + bh / 2
            if t == "face":
                m = np.maximum(m, _ellipse(h, w, cx, cy + bh * 0.05, bw / 2 * sc, bh / 2 * sc * 1.15,
                                           spec.get("feather", 60)))
            elif t == "subject":  # head (incl. hair) + shoulders/torso, face-anchored
                fe = spec.get("feather", 55)
                m = np.maximum(m, _ellipse(h, w, cx, cy, bw * 1.0 * sc, bh * 1.15 * sc, fe))
                m = np.maximum(m, _ellipse(h, w, cx, cy + bh * 1.9 * sc, bw * 1.6 * sc, bh * 2.0 * sc, fe))
            else:
                m = np.maximum(m, _ellipse(h, w, cx, cy, bw / 2, bh / 2, spec.get("feather", 40)))
    elif t == "sky":
        m = _sky(img)
    elif t == "matte":
        m = _matte(spec.get("name", "portrait"), h, w)
    else:
        raise ValueError(f"unknown mask type: {t}")
    m = m.astype(np.float32)
    if spec.get("refine", t in ("face", "subject", "sky", "boxes")):
        m = guided_refine(m, img)
    for sub in spec.get("intersect", []) or []:
        m *= build_mask(sub, img, faces)
    for sub in spec.get("subtract", []) or []:
        m *= 1 - build_mask(sub, img, faces)
    if spec.get("invert"):
        m = 1 - m
    return np.clip(m * spec.get("amount", 1.0), 0, 1).astype(np.float32)


def apply_local(img, adj, long_side):
    lin = E.srgb_to_linear(img)
    lin = E.white_balance(lin, adj.get("temp", 0), adj.get("tint", 0))
    if adj.get("exposure"):
        lin = lin * (2.0 ** adj["exposure"])
    if any(adj.get(k) for k in ("contrast", "highlights", "shadows", "whites", "blacks")):
        lin = E.tone_luminance(lin, adj, long_side)
    out = E.linear_to_srgb(np.clip(lin, 0, None))
    out = np.clip(out, 0, 1)
    out = E.dehaze(out, adj.get("dehaze", 0))
    out = E.local_contrast(out, adj.get("clarity", 0), adj.get("texture", 0), long_side)
    out = E.hsl_and_saturation(out, adj)
    return out


def apply_masks(img, entries, faces=None):
    if not entries:
        return img
    h, w = img.shape[:2]
    long_side = max(h, w)
    need_faces = any((e.get("mask") or {}).get("type") in ("face", "subject") for e in entries)
    if need_faces and faces is None:
        faces = detect_faces(img)
    out = img
    for e in entries:
        m = build_mask(e.get("mask") or {}, out, faces)
        if m.max() < 0.01:
            continue
        local = apply_local(out, e.get("adjust") or {}, long_side)
        out = out + (local - out) * m[..., None]
        del local
    return np.clip(out, 0, 1)


def overlay(img, entries, faces=None):
    """Red overlay of every mask (Lightroom 'show overlay') for visual verification."""
    if faces is None and any((e.get("mask") or {}).get("type") in ("face", "subject") for e in entries):
        faces = detect_faces(img)
    tiles = []
    for e in entries:
        m = build_mask(e.get("mask") or {}, img, faces)[..., None]
        red = np.array([1.0, 0.1, 0.1], np.float32)
        tiles.append((img * (1 - 0.55 * m) + red * 0.55 * m, e.get("name") or e["mask"].get("type")))
    return tiles


def sky_mask(img):
    """Best sky mask available: the iPhone's own sky matte when the sidecar has one, else the heuristic."""
    from pathlib import Path
    h, w = img.shape[:2]
    if _MATTES["dir"] and (Path(_MATTES["dir"]) / "sky.png").exists():
        return guided_refine(_matte("sky", h, w), img)
    m = _sky(img)
    # texture gate, measured at a fixed 1200px: real skies (clear, overcast, hazy) have a median
    # |Laplacian| < 0.7e-3; bright rocks / walls / snow the heuristic also grabs were 4.7e-3 and up
    s = 1200 / max(h, w)
    small = cv2.resize(img, (max(8, int(w * s)), max(8, int(h * s))), interpolation=cv2.INTER_AREA)
    g = cv2.GaussianBlur(cv2.cvtColor(small.astype(np.float32), cv2.COLOR_RGB2GRAY), (0, 0), 1)
    lap = np.abs(cv2.Laplacian(g, cv2.CV_32F))
    ms = cv2.resize(m, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_AREA) > 0.5
    if not ms.any() or float(np.median(lap[ms])) > 2e-3:
        return np.zeros((h, w), np.float32)  # not a sky
    # per region, not per pixel: a per-pixel gate also drops the sky right next to hair / branches
    # and leaves a bright halo there
    n, lab = cv2.connectedComponents(ms.astype(np.uint8))
    drop = np.zeros_like(ms)
    for k in range(1, n):
        reg = lab == k
        if float(np.median(lap[reg])) > 2e-3:
            drop |= reg
    if drop.any():
        keep = cv2.resize((~drop).astype(np.float32), (w, h), interpolation=cv2.INTER_LINEAR)
        m = m * keep
    return guided_refine(m, img)


def sky_enhance(img, sp, long_side):
    """Recipe key "sky": restore a washed-out sky instead of replacing it (no fake clouds, no new light).

      {"amount": 0..1,     overall strength (default 1)
       "recover": 0..100,  pull bright sky / clouds down so cloud detail shows (default 40)
       "grad": 0..100,     graduated darkening toward the top of the frame, inside the sky only (default 30)
       "blue": 0..100,     colour for a white / overcast sky: a faint blue gradient, strongest at the top,
                           only where the sky is near-neutral and not on bright cloud cores (default 0 = off)
       "sat": -100..100,   saturation of an already blue sky (default 15)
       "dehaze": 0..100}   haze removal inside the sky (default 10)
    The sky must be a real share of the frame (>3%) or nothing happens."""
    amt = float(sp.get("amount", 1.0))
    if amt <= 0:
        return img
    m = sky_mask(img)
    if m.mean() < 0.03:
        return img
    h, w = img.shape[:2]
    out = img.copy()
    lab = cv2.cvtColor(np.clip(img, 0, 1).astype(np.float32), cv2.COLOR_RGB2LAB)
    L, A, B = lab[..., 0] / 100, lab[..., 1], lab[..., 2]
    chroma = np.sqrt(A * A + B * B)
    sel = m > 0.5
    sky_chroma = float(np.median(chroma[sel])) if sel.any() else 0.0
    yy = np.linspace(0, 1, h, dtype=np.float32)[:, None]
    ys = np.nonzero(sel.any(1))[0]
    horizon = (ys.max() + 1) / h if len(ys) else 0.5
    t = np.clip(1 - yy / max(horizon, 1e-3), 0, 1)  # 1 at the top edge, 0 at the sky's lowest row

    rec = sp.get("recover", 40) / 100 * amt
    grad = sp.get("grad", 30) / 100 * amt
    newL = L - rec * 0.35 * E.smoothstep(0.55, 1.0, L) * (L - 0.45)
    newL = newL * (1 - grad * 0.22 * t)
    blue = sp.get("blue", 0) / 100 * amt
    if blue and sky_chroma < 12:  # white / overcast sky only; a blue sky already has its colour
        core = 1 - E.smoothstep(0.88, 0.98, L)  # leave bright cloud cores white
        neutral = 1 - E.smoothstep(6, 16, chroma)
        k = blue * neutral * core * (0.45 + 0.55 * t)
        A = A + k * (-3.0 - A * 0.5)
        B = B + k * (-16.0 - B * 0.5)
    sat = sp.get("sat", 15) / 100 * amt
    if sat and sky_chroma >= 12:
        A, B = A * (1 + sat), B * (1 + sat)
    lab2 = np.stack([np.clip(newL, 0, 1) * 100, A, B], -1).astype(np.float32)
    res = np.clip(cv2.cvtColor(lab2, cv2.COLOR_LAB2RGB), 0, 1)
    dh = sp.get("dehaze", 10) * amt
    if dh:
        res = E.dehaze(res, dh)
    out = img * (1 - m[..., None]) + res * m[..., None]
    return np.clip(out, 0, 1).astype(np.float32)
