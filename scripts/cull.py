"""Culling for big shoots (an SD card of 1000+ frames) BEFORE editing.

Pipeline (no ML models, runs on a laptop):
  1. scan    — per frame: capture time, perceptual hash, sharpness, exposure, faces, cached thumbnail
  2. group   — consecutive frames that are close in time AND look alike = one burst/scene group
  3. veto    — hard technical rejects: blurry, hopelessly dark, blown out
  4. rank    — technical score inside each group (sharpness + exposure); keep top candidates
  5. sheets  — contact sheets per group so Claude can judge the *moment* (expression, gesture,
               composition) visually and override the technical pick

The technical score only decides among near-identical frames. Choosing *which moments* are worth
keeping is Claude's visual judgement (see SKILL.md, step "選片").
"""
from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

THUMB = 420


def _exif_time(im):
    try:
        ex = im.getexif()
        sub = ex.get_ifd(0x8769)
        s = sub.get(36867) or ex.get(306)  # DateTimeOriginal / DateTime
        sub_s = sub.get(37521) or "0"     # SubsecTimeOriginal
        t = datetime.strptime(str(s).strip("\x00 "), "%Y:%m:%d %H:%M:%S").timestamp()
        return t + float("0." + str(sub_s).strip("\x00 ")[:3] or 0)
    except Exception:
        return None


def _dhash(gray, size=8):
    g = cv2.resize(gray, (size + 1, size), interpolation=cv2.INTER_AREA)
    bits = (g[:, 1:] > g[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def hamming(a, b):
    return bin(a ^ b).count("1")


def _scan_one(args):
    path, thumb_dir = args
    try:
        im = Image.open(path)
        t = _exif_time(im)
        im.draft("RGB", (1600, 1600))  # JPEG DCT scaling: fast 1/2–1/4 decode
        im = ImageOps.exif_transpose(im).convert("RGB")
        arr = np.asarray(im, dtype=np.uint8)
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        h, w = gray.shape
        # sharpness: Laplacian variance per tile, 90th percentile (subject may be small; bokeh is fine)
        g = cv2.resize(gray, (1200, int(1200 * h / w))) if w >= h else cv2.resize(gray, (int(1200 * w / h), 1200))
        lap = cv2.Laplacian(g.astype(np.float32), cv2.CV_32F)
        th, tw = g.shape[0] // 4, g.shape[1] // 6
        tiles = [lap[i * th:(i + 1) * th, j * tw:(j + 1) * tw].var() for i in range(4) for j in range(6)]
        sharp = float(np.percentile(tiles, 90))
        f = gray.astype(np.float32) / 255
        lum = float(f.mean())
        clip_hi = float((arr.max(-1) >= 250).mean())
        clip_lo = float((gray <= 6).mean())
        thumb = Image.fromarray(arr)
        thumb.thumbnail((THUMB, THUMB))
        tp = Path(thumb_dir) / (Path(path).stem + ".jpg")
        thumb.save(tp, quality=82)
        faces = 0
        try:
            casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
            s = 640 / max(h, w)
            gs = cv2.equalizeHist(cv2.resize(gray, (int(w * s), int(h * s))))
            faces = int(len(casc.detectMultiScale(gs, 1.15, 6, minSize=(24, 24))))
        except Exception:
            pass
        return {"file": str(path), "time": t, "hash": _dhash(gray), "sharp": round(sharp, 2),
                "lum": round(lum, 3), "clip_hi": round(clip_hi, 4), "clip_lo": round(clip_lo, 4),
                "faces": faces, "thumb": str(tp), "size": [int(w), int(h)]}
    except Exception as e:
        return {"file": str(path), "error": str(e)}


def scan(files, out_dir, jobs=4, cache=True):
    out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir = out_dir / "thumbs"; thumb_dir.mkdir(exist_ok=True)
    cache_p = out_dir / "scan.json"
    known = {}
    if cache and cache_p.exists():
        known = {r["file"]: r for r in json.loads(cache_p.read_text()) if "error" not in r}
    todo = [str(f) for f in files if str(f) not in known]
    if todo:
        with ProcessPoolExecutor(jobs) as ex:
            for i, r in enumerate(ex.map(_scan_one, [(f, str(thumb_dir)) for f in todo], chunksize=8)):
                known[r["file"]] = r
                if (i + 1) % 100 == 0:
                    print(f"  scanned {i + 1}/{len(todo)}", flush=True)
    rows = [known[str(f)] for f in files if str(f) in known]
    cache_p.write_text(json.dumps(list(known.values())))
    return rows


def _sim_features(thumb_path):
    """Shift-tolerant look-alike features from the cached thumbnail:
    blurred 32x32 grey (structure, zero-mean/unit-var) + hue/sat histogram (palette)."""
    im = cv2.imread(thumb_path)
    if im is None:
        return None
    g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
    s = cv2.GaussianBlur(cv2.resize(g, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32), (0, 0), 1.2)
    s = ((s - s.mean()) / (s.std() + 1e-6)).ravel()
    hsv = cv2.cvtColor(im, cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0, 1], None, [18, 4], [0, 180, 0, 256])
    cv2.normalize(h, h)
    return s, h


def similar(fa, fb, ncc_hi=0.6, ncc_lo=0.4, hist_min=0.9):
    if fa is None or fb is None:
        return False
    ncc = float((fa[0] * fb[0]).mean())
    if ncc >= ncc_hi:
        return True
    return ncc >= ncc_lo and cv2.compareHist(fa[1], fb[1], cv2.HISTCMP_CORREL) >= hist_min


def group(rows, gap_s=30.0, strict=0.0):
    """Burst/scene groups: frames shot within gap_s of the previous one that look like one of the
    group's last 3 frames (tolerates re-framing back and forth). Calibrated on a 1.6k-frame travel card:
    same-spot frames have structure-NCC ~0.8-0.95 / palette-corr ~0.97; unrelated frames ~0.25 / ~0.6.
    `strict` (0..0.3) raises the similarity bar."""
    ok = [r for r in rows if "error" not in r]
    ok.sort(key=lambda r: (r["time"] or 0, r["file"]))
    feats = {r["file"]: _sim_features(r["thumb"]) for r in ok}
    groups, cur = [], []
    for r in ok:
        if cur:
            prev = cur[-1]
            dt = (r["time"] - prev["time"]) if r["time"] and prev["time"] else 1e9
            if dt <= gap_s and any(similar(feats[r["file"]], feats[c["file"]], 0.6 + strict, 0.4 + strict,
                                           0.9 + strict / 3) for c in cur[-3:]):
                cur.append(r)
                continue
            groups.append(cur)
        cur = [r]
    if cur:
        groups.append(cur)
    return groups


def assess(groups):
    all_sharp = np.array([r["sharp"] for g in groups for r in g]) if groups else np.array([1.0])
    med = float(np.median(all_sharp)) or 1.0
    for gi, g in enumerate(groups):
        gmax = max(r["sharp"] for r in g)
        for r in g:
            veto = []
            if r["sharp"] < 0.25 * med and r["sharp"] < 0.5 * gmax:
                veto.append("blurry")
            if r["lum"] < 0.05 and r["clip_lo"] > 0.6:
                veto.append("too dark")
            if r["clip_hi"] > 0.45:
                veto.append("blown out")
            sharp_rel = min(r["sharp"] / max(gmax, 1e-6), 1.0)        # vs best frame of the burst
            sharp_abs = min(r["sharp"] / (med * 1.5), 1.0)             # vs the whole shoot
            expo = 1.0 - min(1.0, r["clip_hi"] * 4 + max(0.0, r["clip_lo"] - 0.15) * 2)
            r["tech"] = round(0.45 * sharp_rel + 0.25 * sharp_abs + 0.30 * expo - 0.5 * bool(veto), 3)
            r["veto"] = veto
            r["group"] = gi
        ranked = sorted(g, key=lambda r: -r["tech"])
        for k, r in enumerate(ranked):
            r["rank"] = k + 1
    return groups


def summarize(groups, keep_per_group=1):
    picks, rejected = [], 0
    for g in groups:
        ranked = sorted(g, key=lambda r: r["rank"])
        good = [r for r in ranked if not r["veto"]]
        picks += [r["file"] for r in good[:keep_per_group]]
        rejected += sum(1 for r in g if r["veto"])
    return picks, rejected
