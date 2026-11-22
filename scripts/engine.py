"""Lightroom-style raster develop engine (numpy + OpenCV).

All slider names and ranges follow Lightroom Classic (Process Version 2012+)
so a recipe can be read by a photographer and exported to an .xmp preset.
This is an *approximation* of Lightroom's math, tuned by eye on JPGs.

Recipe keys (all optional; missing = neutral):
  temp, tint                -100..100  (relative, like LR on JPG)
  exposure                  -5..5 EV
  contrast, highlights, shadows, whites, blacks   -100..100
  texture, clarity, dehaze  -100..100
  vibrance, saturation      -100..100
  tone_curve   {"master"|"red"|"green"|"blue": [[x,y],...] in 0..255}
  hsl          {"red"|"orange"|"yellow"|"green"|"aqua"|"blue"|"purple"|"magenta":
                  {"hue":.., "sat":.., "lum":..}}   each -100..100
  color_grade  {"shadows"|"midtones"|"highlights"|"global": {"hue":0..360,"sat":0..100,"lum":-100..100},
                "balance":-100..100, "blending":0..100}
  bw           {"enabled":true, "mix":{band: -100..100}}
  sharpen      {"amount":0..150, "radius":0.5..3, "masking":0..100}
  noise        {"luminance":0..100, "color":0..100, "method": "bilateral"|"nlm"}
  vignette     {"amount":-100..100, "midpoint":0..100, "feather":0..100}
  grain        {"amount":0..100, "size":0..100, "roughness":0..100}
  straighten   degrees (+ = clockwise)
  crop         [left, top, right, bottom] fractions 0..1
  sky          {"amount", "recover", "grad", "blue", "sat", "dehaze"}  restore a washed-out sky (masks.sky_enhance)
"""
from __future__ import annotations

import copy
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

cv2.setNumThreads(0)

BANDS = ["red", "orange", "yellow", "green", "aqua", "blue", "purple", "magenta"]
BAND_HUES = np.array([0, 30, 60, 120, 180, 225, 270, 315], dtype=np.float32)
# Keys that are *added* when stacking a style with per-photo corrections.
ADDITIVE = {"temp", "tint", "exposure", "contrast", "highlights", "shadows", "whites",
            "blacks", "texture", "clarity", "dehaze", "vibrance", "saturation", "straighten"}
LUM_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


# ----------------------------------------------------------------------------- io
RAW_EXT = {".arw", ".dng", ".cr2", ".cr3", ".nef", ".nrw", ".raf", ".orf", ".rw2", ".pef", ".srw", ".3fr", ".iiq"}
HEIF_EXT = {".heic", ".heif", ".hif"}
try:  # iPhone HEIC straight into PIL (pip install pillow-heif); otherwise convert with sips first
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass


def is_raw(path):
    return str(path).lower().endswith(tuple(RAW_EXT))


def raw_base_curve(lin, gain=1.8):
    """Camera-like baseline for a linear RAW render: lifts the mids with a soft highlight shoulder
    (f(1) = 1), so the default looks like the camera JPG while the highlights the RAW holds stay
    recoverable by the highlights / whites sliders."""
    return lin * gain / (1 + (gain - 1) * lin)


def raw_preview(path):
    """The camera's own JPG embedded in a RAW (bytes) or None. Carries the EXIF (ISO, lens, date)
    that PIL can't read from most RAW containers, and the camera's brightness judgement."""
    try:
        import rawpy
        with rawpy.imread(str(path)) as raw:
            th = raw.extract_thumb()
        return th.data if th.format == rawpy.ThumbFormat.JPEG else None
    except Exception:
        return None


def _raw_gain(path, lin):
    """Base-curve gain that puts the RAW render's median where the camera's JPG has it
    (cameras meter differently; their embedded JPG is the reference for 'normal')."""
    import io
    data = raw_preview(path)
    m = float(np.median(lin @ LUM_W))
    if not data or m <= 1e-4:
        return 1.8
    jp = np.asarray(Image.open(io.BytesIO(data)).convert("RGB").reduce(4), dtype=np.float32) / 255
    mt = float(np.median(srgb_to_linear(jp) @ LUM_W))
    g = mt * (1 - m) / max(m * (1 - mt), 1e-6)  # solve raw_base_curve(m, g) = mt
    return float(np.clip(g, 1.0, 6.0))


def _load_raw(path, max_side=None):
    import rawpy  # pip install rawpy
    with rawpy.imread(str(path)) as raw:
        # half_size skips demosaicing (4x faster) and is plenty for analysis / previews
        half = bool(max_side) and max_side <= min(raw.sizes.width, raw.sizes.height) // 2
        rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=16, gamma=(1, 1),
                              half_size=half, highlight_mode=rawpy.HighlightMode.Blend,
                              output_color=rawpy.ColorSpace.sRGB, user_flip=None)
    lin = rgb.astype(np.float32) / 65535.0
    if max_side and max(lin.shape[:2]) > max_side:
        s = max_side / max(lin.shape[:2])
        lin = cv2.resize(lin, (int(lin.shape[1] * s), int(lin.shape[0] * s)), interpolation=cv2.INTER_AREA)
    small = lin if max(lin.shape[:2]) <= 1200 else cv2.resize(lin, None, fx=1200 / max(lin.shape[:2]),
                                                              fy=1200 / max(lin.shape[:2]), interpolation=cv2.INTER_AREA)
    out = linear_to_srgb(np.clip(raw_base_curve(lin, _raw_gain(path, small)), 0, 1))
    return raw_profile(out)


# like Lightroom's 'Adobe Color' on top of a flat RAW render: a gentle S-curve and some colour
RAW_PROFILE_CURVE = [[0, 0], [64, 57], [128, 128], [192, 199], [255, 255]]


def raw_profile(rgb, sat=1.15):
    rgb = tone_curves(rgb.astype(np.float32), {"master": RAW_PROFILE_CURVE})
    y = (rgb @ LUM_W)[..., None]
    return np.clip(y + (rgb - y) * sat, 0, 1).astype(np.float32)


def exif_image(path):
    """A PIL image whose getexif() describes `path` (the embedded JPG for RAW files)."""
    import io
    if is_raw(path):
        data = raw_preview(path)
        return Image.open(io.BytesIO(data)) if data else None
    return Image.open(path)


def _load_heif_sips(path, max_side=None):
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        dst = Path(d) / (Path(path).stem + ".jpg")
        subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "best", str(path), "--out", str(dst)],
                       check=True, capture_output=True)
        return load_image(dst, max_side)


def load_image(path, max_side=None):
    if is_raw(path):
        return _load_raw(path, max_side)
    if str(path).lower().endswith(tuple(HEIF_EXT)) and "pillow_heif" not in globals():
        return _load_heif_sips(path, max_side)
    im = Image.open(path)
    if max_side:
        im.draft("RGB", (max_side, max_side))
    im = ImageOps.exif_transpose(im).convert("RGB")
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    return np.asarray(im, dtype=np.float32) / 255.0


def save_image(arr, path, src_path=None, quality=92):
    out = Image.fromarray((np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8))
    kw = {"quality": quality, "subsampling": 0 if quality >= 90 else 2}
    if src_path:
        try:
            src = exif_image(src_path)
            exif = src.getexif()
            exif[0x0112] = 1  # pixels are already upright
            exif[0x0131] = "photo-edit-skill"
            kw["exif"] = exif.tobytes()
            if src.info.get("icc_profile"):
                kw["icc_profile"] = src.info["icc_profile"]
        except Exception:
            pass
    out.save(path, "JPEG", **kw)


# ----------------------------------------------------------------------------- color helpers
def srgb_to_linear(x):
    return np.where(x <= 0.04045, x / 12.92, ((np.maximum(x, 0) + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(x):
    x = np.maximum(x, 0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1 / 2.4) - 0.055).astype(np.float32)


def luminance(rgb):
    return rgb @ LUM_W


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def gblur(x, sigma):
    if sigma <= 0.3:
        return x
    return cv2.GaussianBlur(x, (0, 0), sigmaX=float(sigma), borderType=cv2.BORDER_REFLECT)


def fast_blur(x, sigma):
    """Large-sigma blur through a downscaled copy (fast on 24MP)."""
    h, w = x.shape[:2]
    f = max(1.0, sigma / 6.0)
    if f <= 1.5:
        return gblur(x, sigma)
    sw, sh = max(8, int(w / f)), max(8, int(h / f))
    small = cv2.resize(x, (sw, sh), interpolation=cv2.INTER_AREA)
    small = gblur(small, sigma / f)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)


def edge_aware_base(L, frac=0.012):
    """Locally-smoothed luminance used as mask for highlights/shadows (limits halos)."""
    h, w = L.shape
    scale = 640.0 / max(h, w)
    small = cv2.resize(L, (max(8, int(w * scale)), max(8, int(h * scale))), interpolation=cv2.INTER_AREA)
    sig = max(2.0, frac * 640)
    b = cv2.bilateralFilter(small, d=0, sigmaColor=0.12, sigmaSpace=sig)
    b = cv2.bilateralFilter(b, d=0, sigmaColor=0.12, sigmaSpace=sig)
    return cv2.resize(b, (w, h), interpolation=cv2.INTER_LINEAR)


def pchip_lut(points, n=4096):
    """Monotone cubic (Fritsch-Carlson) LUT from [[x,y],...] in 0..255."""
    pts = sorted((float(a) / 255.0, float(b) / 255.0) for a, b in points)
    if pts[0][0] > 0:
        pts.insert(0, (0.0, pts[0][1]))
    if pts[-1][0] < 1:
        pts.append((1.0, pts[-1][1]))
    xs = np.array([p[0] for p in pts]); ys = np.array([p[1] for p in pts])
    h = np.diff(xs); d = np.diff(ys) / np.maximum(h, 1e-9)
    m = np.zeros_like(xs)
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, len(xs) - 1):
        if d[i - 1] * d[i] <= 0:
            m[i] = 0
        else:
            w1, w2 = 2 * h[i] + h[i - 1], h[i] + 2 * h[i - 1]
            m[i] = (w1 + w2) / (w1 / d[i - 1] + w2 / d[i])
    xq = np.linspace(0, 1, n)
    idx = np.clip(np.searchsorted(xs, xq) - 1, 0, len(xs) - 2)
    x0, x1 = xs[idx], xs[idx + 1]; y0, y1 = ys[idx], ys[idx + 1]
    hh = x1 - x0; t = (xq - x0) / np.maximum(hh, 1e-9)
    h00 = 2 * t**3 - 3 * t**2 + 1; h10 = t**3 - 2 * t**2 + t
    h01 = -2 * t**3 + 3 * t**2; h11 = t**3 - t**2
    yq = h00 * y0 + h10 * hh * m[idx] + h01 * y1 + h11 * hh * m[idx + 1]
    return np.clip(yq, 0, 1).astype(np.float32)


def apply_lut(x, lut):
    n = len(lut) - 1
    return lut[np.clip((x * n + 0.5).astype(np.int32), 0, n)]


_EXT = np.append(BAND_HUES, 360.0).astype(np.float32)


def band_interp(hue_deg, values):
    """Per-pixel value of an 8-band slider set, cosine-interpolated between
    neighbouring band centres (no HxWx8 array -> low memory)."""
    v = np.asarray(values, dtype=np.float32)
    if not v.any():
        return np.zeros(hue_deg.shape, np.float32)
    pos = np.interp(hue_deg % 360, _EXT, np.arange(9, dtype=np.float32)).astype(np.float32)
    i = np.minimum(pos.astype(np.int32), 7)
    f = pos - i
    f = 0.5 - 0.5 * np.cos(np.pi * f)
    return (v[i] * (1 - f) + v[(i + 1) % 8] * f).astype(np.float32)


def rgb_to_hsv(rgb):
    hsv = cv2.cvtColor(np.clip(rgb, 0, 1).astype(np.float32), cv2.COLOR_RGB2HSV)
    return hsv  # H 0..360, S 0..1, V 0..1


def hsv_to_rgb(hsv):
    return cv2.cvtColor(hsv.astype(np.float32), cv2.COLOR_HSV2RGB)


def hue_vec(hue_deg):
    """Zero-luminance RGB offset pointing to a hue (unit-ish chroma)."""
    c = hsv_to_rgb(np.array([[[hue_deg % 360, 1.0, 1.0]]], dtype=np.float32))[0, 0]
    v = c - luminance(c)
    return v / max(np.abs(v).max(), 1e-6)


# ----------------------------------------------------------------------------- recipe merge
META_KEYS = {"name", "description", "use_for", "avoid_for", "notes"}


def _override(a, b):
    out = copy.deepcopy(a)
    for k, v in b.items():
        out[k] = _override(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def merge_recipes(base, *overlays):
    """Stack recipes: top-level basic sliders ADD (style + correction),
    everything nested (hsl, curves, grading, ...) is overridden key by key."""
    out = {k: copy.deepcopy(v) for k, v in (base or {}).items() if k not in META_KEYS and not k.startswith("_")}
    for ov in overlays:
        for k, v in (ov or {}).items():
            if k in META_KEYS or k.startswith("_"):
                continue
            if k in ADDITIVE and isinstance(v, (int, float)) and isinstance(out.get(k), (int, float)):
                out[k] = out[k] + v
            elif k == "masks" and isinstance(v, list):
                # local adjustments stack; an overlay entry with the same "name" replaces it
                names = {m.get("name") for m in v if m.get("name")}
                out[k] = [m for m in out.get(k, []) if m.get("name") not in names] + copy.deepcopy(v)
            elif isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = _override(out[k], v)
            else:
                out[k] = copy.deepcopy(v)
    return out


def scale_recipe(r, s):
    """Preset 'Amount': scale a style toward neutral (0..1+)."""
    if s == 1:
        return copy.deepcopy(r)
    out = {}
    for k, v in r.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = v * s
        elif k == "tone_curve":
            out[k] = {ch: [[x, x + (y - x) * s] for x, y in pts] for ch, pts in v.items()}
        elif k == "hsl":
            out[k] = {b: {kk: vv * s for kk, vv in d.items()} for b, d in v.items()}
        elif k == "color_grade":
            cg = {}
            for kk, vv in v.items():
                if isinstance(vv, dict):
                    cg[kk] = {**vv, "sat": vv.get("sat", 0) * s, "lum": vv.get("lum", 0) * s}
                else:
                    cg[kk] = vv * s if kk == "balance" else vv
            out[k] = cg
        elif k in ("vignette", "grain", "sky"):
            out[k] = {**v, "amount": v.get("amount", 1.0 if k == "sky" else 0) * s}
        else:
            out[k] = copy.deepcopy(v)
    return out


# ----------------------------------------------------------------------------- pipeline stages
def white_balance(lin, temp, tint):
    if not temp and not tint:
        return lin
    t, g = temp / 100.0, tint / 100.0
    gains = np.array([math.exp(0.32 * t), math.exp(-0.22 * g), math.exp(-0.42 * t)], dtype=np.float32)
    gains /= gains @ LUM_W  # keep overall brightness
    return lin * gains


def denoise(rgb, nr, scale):
    lum_amt, col_amt = nr.get("luminance", 0), nr.get("color", 0)
    if not lum_amt and not col_amt:
        return rgb
    ycc = cv2.cvtColor(np.clip(rgb, 0, 1).astype(np.float32), cv2.COLOR_RGB2YCrCb)
    if col_amt:
        s = (1 + col_amt / 100 * 6) * scale
        ycc[..., 1] = gblur(ycc[..., 1], s)
        ycc[..., 2] = gblur(ycc[..., 2], s)
    if lum_amt and nr.get("method") == "nlm":
        # non-local means: averages similar patches across the frame. Much stronger on night / high-ISO
        # blotches than the bilateral default, keeps edges; slower (~10 s at 24MP), so opt-in
        y = ycc[..., 0]
        y8 = (np.clip(y, 0, 1) * 255 + 0.5).astype(np.uint8)
        den = cv2.fastNlMeansDenoising(y8, None, h=2 + lum_amt / 100 * 10, templateWindowSize=7,
                                       searchWindowSize=21).astype(np.float32) / 255
        ycc[..., 0] = y + (den - y) * min(1.0, lum_amt / 60)
    elif lum_amt:
        y = ycc[..., 0]
        d = max(3, int(round(5 * scale)))
        sc = 0.02 + lum_amt / 100 * 0.10
        den = cv2.bilateralFilter(y, d=d, sigmaColor=sc, sigmaSpace=2.5 * scale + 1)
        ycc[..., 0] = y + (den - y) * min(1.0, lum_amt / 60)
    return cv2.cvtColor(ycc, cv2.COLOR_YCrCb2RGB)


def tone_luminance(lin, r, long_side):
    hl, sh = r.get("highlights", 0) / 100, r.get("shadows", 0) / 100
    wh, bl = r.get("whites", 0) / 100, r.get("blacks", 0) / 100
    ct = r.get("contrast", 0) / 100
    Y = np.maximum(luminance(lin), 1e-5)
    Yt = Y.copy()
    if hl or sh:
        Lg = linear_to_srgb(np.minimum(Y, 4))
        base = 0.55 * edge_aware_base(np.clip(Lg, 0, 1)) + 0.45 * np.clip(Lg, 0, 1)
        stops = np.zeros_like(Y)
        if sh:
            m = 1 - smoothstep(0.02, 0.6, base)
            stops += sh * (1.25 if sh > 0 else 0.8) * m
        if hl:
            m = smoothstep(0.35, 0.95, base)
            stops += hl * (0.9 if hl < 0 else 0.6) * m
        Yt = Yt * np.exp2(stops)
    # map to perceptual space for whites / blacks / contrast
    Lg = linear_to_srgb(Yt)
    # highlight shoulder so >1 rolls off instead of hard clipping
    knee = 0.88
    over = Lg > knee
    Lg = np.where(over, knee + (1 - knee) * np.tanh((Lg - knee) / (1 - knee)), Lg)
    if wh:
        Lg = Lg + wh * 0.16 * smoothstep(0.45, 1.0, Lg) * (1.15 - Lg if wh > 0 else 1.0)
    if bl:
        Lg = Lg + bl * 0.10 * (1 - smoothstep(0.0, 0.4, Lg))
    if ct:
        pivot = 0.46
        a = 1 + ct * 0.75 if ct > 0 else 1 / (1 - ct * 0.75)
        x = np.clip(Lg, 0, 1)
        lo = pivot * (x / pivot) ** a
        hi = 1 - (1 - pivot) * ((1 - x) / (1 - pivot)) ** a
        Lg = np.where(x < pivot, lo, hi) + (Lg - x)
    Lg = np.clip(Lg, 0, 1.0)
    Ynew = srgb_to_linear(Lg)
    ratio = (Ynew / Y)[..., None]
    # near-black pixels (phone JPGs crush to ~0) get ratios of 100x+ when shadows/blacks lift them;
    # scaling RGB by that blows faint channel noise into saturated blue/red blotches. Luminance takes
    # the full ratio, chroma (deviation from grey) is capped.
    Y3 = Y[..., None]
    out = Ynew[..., None] + (lin - Y3) * np.minimum(ratio, 1.5)
    # channels pushed above 1: desaturate toward luminance instead of hue-shifting
    mx = out.max(-1, keepdims=True)
    yy = luminance(out)[..., None]
    k = np.where(mx > 1, (1 - yy) / np.maximum(mx - yy, 1e-6), 1.0)
    out = yy + (out - yy) * np.clip(k, 0, 1)
    return np.clip(out, 0, 1)


def dehaze(rgb, amount):
    if not amount:
        return rgb
    a = amount / 100
    h, w = rgb.shape[:2]
    s = 480.0 / max(h, w)
    small = cv2.resize(rgb, (max(8, int(w * s)), max(8, int(h * s))), interpolation=cv2.INTER_AREA)
    dark = cv2.erode(small.min(-1), np.ones((15, 15), np.uint8))
    flat = dark.ravel()
    idx = np.argsort(flat)[-max(10, flat.size // 1000):]
    A = small.reshape(-1, 3)[idx].mean(0)
    A = np.clip(A, 0.5, 1.0)
    if a > 0:
        t_small = 1 - 0.95 * a * cv2.erode((small / A).min(-1), np.ones((15, 15), np.uint8))
        t_small = gblur(t_small.astype(np.float32), 6)
        t = cv2.resize(t_small, (w, h), interpolation=cv2.INTER_LINEAR)
        t = np.clip(t, 0.25, 1)[..., None]
        out = (rgb - A) / t + A
        # dehaze also deepens a touch and adds saturation like LR
        return np.clip(out, 0, 1)
    return np.clip(rgb * (1 + 0.6 * a) - A * 0.6 * a, 0, 1)


def local_contrast(rgb, clarity, texture, long_side):
    if not clarity and not texture:
        return rgb
    L = luminance(rgb)
    delta = np.zeros_like(L)
    if clarity:
        base = fast_blur(L, long_side * 0.012)
        mid = 4 * L * (1 - L)
        delta += clarity / 100 * 0.9 * (L - base) * (0.35 + 0.65 * mid)
    if texture:
        base = gblur(L, max(1.0, long_side * 0.0012))
        delta += texture / 100 * 0.9 * (L - base)
    return np.clip(rgb + delta[..., None], 0, 1)


def tone_curves(rgb, curves):
    if not curves:
        return rgb
    out = rgb
    if curves.get("master"):
        lut = pchip_lut(curves["master"])
        # 40% per-channel (LR-like, adds a little saturation) + 60% on luminance (hue/sat preserving)
        per = apply_lut(out, lut)
        L = luminance(out)
        ratio = apply_lut(np.clip(L, 0, 1), lut) / np.maximum(L, 1e-4)
        out = 0.4 * per + 0.6 * np.clip(out * ratio[..., None], 0, 1)
        del per
    chans = [curves.get(c) for c in ("red", "green", "blue")]
    if any(chans):
        out = out.copy()
        for i, pts in enumerate(chans):
            if pts:
                out[..., i] = apply_lut(out[..., i], pchip_lut(pts))
    return out


def hsl_and_saturation(rgb, r):
    hsl = r.get("hsl") or {}
    vib, sat = r.get("vibrance", 0) / 100, r.get("saturation", 0) / 100
    if not hsl and not vib and not sat:
        return rgb
    hsv = rgb_to_hsv(rgb)
    H, S = hsv[..., 0], hsv[..., 1]
    H0 = H.copy()
    lum_gain = None
    if hsl:
        get = lambda key: [hsl.get(b, {}).get(key, 0) for b in BANDS]
        hue_s, sat_s, lum_s = get("hue"), get("sat"), get("lum")
        if any(hue_s):
            H = (H + band_interp(H0, [x * 0.3 for x in hue_s])) % 360  # +-100 => +-30 deg
        if any(sat_s):
            S *= np.clip(1 + band_interp(H0, [x / 100 for x in sat_s]), 0, 3)
        if any(lum_s):
            lum_gain = band_interp(H0, [x / 100 for x in lum_s])
    if vib:
        skin = smoothstep(5, 15, H0) * (1 - smoothstep(40, 55, H0))
        S *= 1 + vib * 1.1 * (1 - S) * (1 - 0.6 * skin)
    if sat:
        S *= 1 + sat
    np.clip(S, 0, 1, out=S)
    hsv[..., 0], hsv[..., 1] = H, S
    out = hsv_to_rgb(hsv)
    if lum_gain is not None:
        # luminance slider: brighten/darken coloured pixels, neutrals untouched
        g = 1 + lum_gain * 0.6 * np.clip(S * 1.4, 0, 1)
        out *= g[..., None]
    return np.clip(out, 0, 1)


def black_and_white(rgb, bw):
    if not bw or not bw.get("enabled"):
        return rgb
    mix = bw.get("mix") or {}
    L = luminance(rgb)
    if mix:
        hsv = rgb_to_hsv(rgb)
        m = [mix.get(b, 0) / 100 for b in BANDS]
        L = L * (1 + band_interp(hsv[..., 0], m) * 0.8 * np.clip(hsv[..., 1] * 1.3, 0, 1))
    L = np.clip(L, 0, 1)
    return np.repeat(L[..., None], 3, -1)


def color_grade(rgb, cg):
    if not cg:
        return rgb
    L = luminance(rgb)
    bal = cg.get("balance", 0) / 100
    Lb = np.clip(L, 0, 1) ** (2 ** (-bal * 1.2))
    blend = cg.get("blending", 50) / 100
    p = 1.5 + (1 - blend) * 2.5  # lower blending => crisper separation
    ws = (1 - Lb) ** p
    wh = Lb ** p
    wm = np.clip(1 - ws - wh, 0, 1)
    out = rgb.copy()
    for key, w in (("shadows", ws), ("midtones", wm), ("highlights", wh), ("global", np.ones_like(L))):
        z = cg.get(key)
        if not z:
            continue
        s, lum = z.get("sat", 0) / 100, z.get("lum", 0) / 100
        if s:
            out = out + (w * s * 0.22)[..., None] * hue_vec(z.get("hue", 0))
        if lum:
            out = out + (w * lum * 0.18)[..., None]
    return np.clip(out, 0, 1)


def sharpen(rgb, sh, scale):
    amt = sh.get("amount", 0) / 100
    if not amt:
        return rgb
    radius = max(0.4, sh.get("radius", 1.0) * scale)
    L = luminance(rgb)
    detail = L - gblur(L, radius)
    masking = sh.get("masking", 0) / 100
    if masking:
        gx = cv2.Sobel(gblur(L, 1.0 * scale + 0.5), cv2.CV_32F, 1, 0)
        gy = cv2.Sobel(gblur(L, 1.0 * scale + 0.5), cv2.CV_32F, 0, 1)
        mag = np.sqrt(gx * gx + gy * gy)
        thr = np.quantile(mag[:: 8, :: 8], 0.5 + 0.45 * masking)
        detail = detail * smoothstep(thr * 0.5, thr * 1.5, mag)
    return np.clip(rgb + (amt * 1.2 * detail)[..., None], 0, 1)


def vignette(rgb, vg):
    amt = vg.get("amount", 0) / 100
    if not amt:
        return rgb
    h, w = rgb.shape[:2]
    mid = vg.get("midpoint", 50) / 100
    feather = max(0.05, vg.get("feather", 50) / 100)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    nx, ny = (x / (w - 1) - 0.5) * 2, (y / (h - 1) - 0.5) * 2
    r = np.sqrt((nx * nx + ny * ny) / 2) * 1.414  # ~1 at edges, ~1.41 at corners
    start = 0.35 + 0.75 * mid
    m = smoothstep(start - feather * 0.6, start + feather * 0.6, r)
    if amt < 0:
        g = 1 + amt * 0.85 * m
        return rgb * g[..., None]
    return rgb + (1 - rgb) * (amt * 0.85 * m)[..., None]


def grain(rgb, gr, scale, seed=7):
    amt = gr.get("amount", 0) / 100
    if not amt:
        return rgb
    size = 0.6 + gr.get("size", 25) / 100 * 2.5
    rough = gr.get("roughness", 50) / 100
    h, w = rgb.shape[:2]
    gs = max(0.5, size * scale)
    rng = np.random.default_rng(seed)
    sh, sw = max(2, int(h / gs)), max(2, int(w / gs))
    n = rng.standard_normal((sh, sw)).astype(np.float32)
    n = cv2.resize(n, (w, h), interpolation=cv2.INTER_CUBIC if rough < 0.5 else cv2.INTER_LINEAR)
    n = gblur(n, 0.35 * gs * (1 - rough))
    n /= max(n.std(), 1e-6)
    L = luminance(rgb)
    weight = 0.35 + 0.65 * 4 * L * (1 - L)
    return np.clip(rgb + (n * weight * amt * 0.045)[..., None], 0, 1)


def geometry(rgb, r):
    rot = int(r.get("rotate", 0)) % 360  # 90/180/270 clockwise: frames whose EXIF lacks orientation
    if rot:
        rgb = np.ascontiguousarray(np.rot90(rgb, k=-(rot // 90)))
    ang = r.get("straighten", 0)
    if ang:
        h, w = rgb.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2, h / 2), -ang, 1.0)
        rgb = cv2.warpAffine(rgb, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
        a = math.radians(abs(ang))
        # largest axis-aligned rect inside rotated frame (same aspect)
        f = 1 / (math.cos(a) + math.sin(a) * max(w / h, h / w))
        cw, ch = int(w * f), int(h * f)
        x0, y0 = (w - cw) // 2, (h - ch) // 2
        rgb = rgb[y0:y0 + ch, x0:x0 + cw]
    crop = r.get("crop")
    if crop:
        h, w = rgb.shape[:2]
        l, t, rr, b = crop
        rgb = rgb[int(t * h):int(b * h), int(l * w):int(rr * w)]
    return rgb


def skin_mask(hsv):
    H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    hm = smoothstep(0, 8, H) * (1 - smoothstep(38, 52, H)) + (1 - smoothstep(345, 352, H)) * (H > 300)
    return np.clip(hm, 0, 1) * smoothstep(0.08, 0.18, S) * (1 - smoothstep(0.65, 0.8, S)) * smoothstep(0.12, 0.25, V)


def protect_skin(out, ref_hsv, amount):
    """Keep skin from going orange/red: cap saturation & pull hue back toward the original."""
    m = skin_mask(ref_hsv) * amount
    if m.max() < 0.05:
        return out
    hsv = rgb_to_hsv(out)
    dh = ((hsv[..., 0] - ref_hsv[..., 0] + 180) % 360) - 180
    hsv[..., 0] = (hsv[..., 0] - dh * m * 0.6) % 360
    cap = ref_hsv[..., 1] * 1.12 + 0.03
    hsv[..., 1] = hsv[..., 1] - np.maximum(hsv[..., 1] - cap, 0) * m
    return hsv_to_rgb(hsv)


def _guided_self(x, r, eps):
    """Edge-preserving smoothing (guided filter with itself as guide); x float32 2D."""
    k = (2 * r + 1, 2 * r + 1)
    mu = cv2.boxFilter(x, -1, k)
    var = cv2.boxFilter(x * x, -1, k) - mu * mu
    a = var / (var + eps)
    b = mu - a * mu
    return cv2.boxFilter(a, -1, k) * x + cv2.boxFilter(b, -1, k)


def beauty(lab, m, bt, long_side):
    """Portrait retouch inside the skin matte (frequency-separation style), lab = OpenCV float Lab.

    bt: {"smooth": 0..1  remove mid-frequency blemishes/unevenness, keep pore texture,
         "texture": 0..1 how much fine pore detail survives (0.6 = natural),
         "even": 0..1    even out blotchy redness / dark patches in colour,
         "glow": L*      brighten skin, "rosy": a*  add a little healthy pink}
    Radii scale with the skin area so a close selfie and a small face in a group shot get the same look.
    """
    area = float((m > 0.5).sum())
    if area < 400:
        return lab
    s = float(np.clip(np.sqrt(area), long_side * 0.03, long_side * 0.6))  # ~ face width in px
    L = lab[..., 0] / 100.0
    sm = bt.get("smooth", 0.5)
    if sm:
        fine = L - gblur(L, max(0.6, s * 0.004))                     # pores / fine texture
        r = max(2, int(s * 0.018))
        base = _guided_self(L, r, 0.0012)                            # blemishes & bumps flattened, edges kept
        Ln = base + fine * bt.get("texture", 0.6)
        L = L + (Ln - L) * (m * sm)
    ev = bt.get("even", 0.4)
    if ev:
        sig = s * 0.035
        wsum = gblur(m, sig) + 1e-4
        for c in (1, 2):                                             # skin-only blur of colour (no bleed from hair)
            avg = gblur(lab[..., c] * m, sig) / wsum
            lab[..., c] += (avg - lab[..., c]) * (m * ev)
    lab[..., 0] = np.clip(L * 100 + bt.get("glow", 0) * m, 0, 100)
    if bt.get("rosy"):
        lab[..., 1] += bt["rosy"] * m
    return lab


def skin_anchor(out, ref_lab, sa, h, w, long_side):
    """Pull skin (iPhone semantic skin matte) back to the camera's own skin rendering, in Lab.

    Phones already tone-map faces locally with these same mattes; global highlight/shadow/contrast/HSL moves then
    push faces darker/brighter/yellower than the scene around them, which reads as 'something is off'. Pros fix
    skin on a face-skin mask; this does that: L -> ref L + lum, chroma -> ref * sat, hue -> ref hue.
    sa: {"amount": 0..1, "lum": L* offset, "sat": chroma factor, "teeth": whiten 0..1}
    """
    import masks as M
    skin = M._matte("skin", h, w)
    teeth = M._matte("teeth", h, w) if sa.get("teeth", 0.5) else None
    if skin.max() < 0.05 and (teeth is None or teeth.max() < 0.05):
        return out
    lab = cv2.cvtColor(np.clip(out, 0, 1).astype(np.float32), cv2.COLOR_RGB2LAB)
    m = gblur(skin, long_side * 0.002) * sa.get("amount", 0.75)
    tgt_L = ref_lab[..., 0] + sa.get("lum", 0)
    lab[..., 0] += (tgt_L - lab[..., 0]) * m
    tgt_ab = ref_lab[..., 1:] * sa.get("sat", 1.0)
    lab[..., 1:] += (tgt_ab - lab[..., 1:]) * m[..., None]
    bt = sa.get("beauty")
    if bt and skin.max() >= 0.05:
        lab = beauty(lab, gblur(skin, long_side * 0.002), bt, long_side)
    if teeth is not None and teeth.max() >= 0.05:
        t = gblur(teeth, long_side * 0.001) * sa.get("teeth", 0.5)
        lab[..., 1:] *= (1 - 0.6 * t)[..., None]          # de-yellow
        lab[..., 0] += 4 * t * (lab[..., 0] < 90)          # a touch brighter
    return np.clip(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB), 0, 1)


# ----------------------------------------------------------------------------- main entry
def develop(img, recipe):
    """img: float32 sRGB HxWx3 in 0..1. Returns developed float32 sRGB."""
    r = recipe or {}
    if r.get("erase"):  # remove bystanders first, on the untouched pixels
        import erase as R
        spec = r["erase"]
        spec = spec if isinstance(spec, dict) else {"remove": spec}
        img, _, _ = R.erase(img, spec.get("remove", "bystanders") if "keep" not in spec else {"keep": spec["keep"]},
                            spec.get("grow", 0.10), extra=spec.get("extra"))
    pre_geom = img.shape[:2]
    img = geometry(img, r)
    h, w = img.shape[:2]
    long_side = max(h, w)
    scale = long_side / 6000.0  # pixel-radius params are defined at 24MP

    img = denoise(img, r.get("noise") or {}, max(scale, 0.25))
    skin = r.get("skin_protect", 0)
    ref_hsv = rgb_to_hsv(img) if skin and not (r.get("bw") or {}).get("enabled") else None
    sa = r.get("skin_anchor") if r.get("_mattes") and not (r.get("bw") or {}).get("enabled") else None
    if r.get("_mattes"):
        import masks as M
        M.set_mattes(r.get("_mattes"), pre_geom, r)
    ref_lab = cv2.cvtColor(img.astype(np.float32), cv2.COLOR_RGB2LAB) if sa else None
    pt = r.get("portrait") if not (r.get("bw") or {}).get("enabled") else None
    pfaces = None
    if pt and any(pt.values()):
        import portrait as P  # face-mesh retouch (MediaPipe); landmarks from the untouched pixels
        pfaces = P.detect(img)
    lin = srgb_to_linear(img)
    del img
    lin = white_balance(lin, r.get("temp", 0), r.get("tint", 0))
    if r.get("exposure"):
        lin = lin * (2.0 ** r["exposure"])
    lin = tone_luminance(lin, r, long_side)
    out = linear_to_srgb(lin)
    out = dehaze(out, r.get("dehaze", 0))
    out = local_contrast(out, r.get("clarity", 0), r.get("texture", 0), long_side)
    out = tone_curves(out, r.get("tone_curve"))
    if (r.get("bw") or {}).get("enabled"):
        out = black_and_white(out, r["bw"])
    else:
        out = hsl_and_saturation(out, r)
    out = color_grade(out, r.get("color_grade"))
    if sa:
        out = skin_anchor(out, ref_lab, sa, h, w, long_side)
        del ref_lab
    if r.get("sky") and not (r.get("bw") or {}).get("enabled"):
        import masks as M  # restore a washed-out sky (sky matte / heuristic mask)
        M.set_mattes(r.get("_mattes"), pre_geom, r)
        out = M.sky_enhance(out, r["sky"], long_side)
    if r.get("masks"):
        import masks as M  # local adjustments (lazy: masks imports engine)
        M.set_mattes(r.get("_mattes"), pre_geom, r)
        out = M.apply_masks(out, r["masks"])
    if ref_hsv is not None:
        out = protect_skin(out, ref_hsv, skin)
        del ref_hsv
    if pfaces:
        import masks as M
        sk = M._matte("skin", h, w) if r.get("_mattes") else None
        out = P.retouch_tone(out, pfaces, pt, sk)
        out = P.retouch_warp(out, pfaces, pt)
    out = sharpen(out, r.get("sharpen") or {}, max(scale, 0.25))
    out = vignette(out, r.get("vignette") or {})
    out = grain(out, r.get("grain") or {}, max(scale, 0.25))
    return np.clip(out, 0, 1).astype(np.float32)
