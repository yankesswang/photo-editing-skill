"""Album organisation: sort a whole folder into events + content categories, and flag the frames
worth deleting (missed focus, closed eyes, shake, hopeless exposure, near-duplicates, accidental shots).

Pipeline (reuses cull.scan for time / thumbnails / sharpness / exposure):
  1. faces    — Haar boxes -> MediaPipe FaceLandmarker on each crop: false positives drop out, and the
                eyeBlink blendshapes say whether the eyes are closed. Sharpness is measured ON each face.
  2. classify — CLIP zero-shot on the cached thumbnail -> one content category per frame
  3. events   — time gaps (default 3 h) split the shoot into events / days
  4. dupes    — cull.group + cull.assess: inside a burst only the best frame is kept, the rest are
                "duplicate" suggestions
  5. verdict  — every frame gets keep / review / delete with human-readable reasons

Nothing here deletes or moves an original. The output is a report (JSON + text lists + contact sheets
+ an offline HTML page) and, on request, a folder tree of symlinks/copies.

Models (downloaded once):
  face_landmarker.task  MediaPipe (Apache-2.0), ~3.7MB  -> ~/.cache/photo-edit/models
  openai/clip-vit-base-patch32 via transformers, ~600MB -> HuggingFace cache
"""
from __future__ import annotations

import json
import os
import urllib.request
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

MODEL_DIR = Path(os.environ.get("PHOTO_EDIT_MODELS", Path.home() / ".cache" / "photo-edit" / "models"))
MODEL_URLS = {
    "face_landmarker.task": "https://storage.googleapis.com/mediapipe-models/face_landmarker/"
                            "face_landmarker/float16/1/face_landmarker.task",
    "blaze_face_short_range.tflite": "https://storage.googleapis.com/mediapipe-models/face_detector/"
                                     "blaze_face_short_range/float16/1/blaze_face_short_range.tflite",
}
CLIP_ID = "openai/clip-vit-base-patch32"
FACE_V = 2  # bump to invalidate cached face scans

# key, 中文名稱, CLIP prompts (averaged). Order = tie priority. Edit freely or pass --categories file.json
CATEGORIES = [
    ("portrait", "人像", ["a portrait photo of one person", "a close-up photo of a person's face",
                         "a photo of a person posing for the camera"]),
    ("group", "合照", ["a group photo of several people posing together", "a photo of friends or family together"]),
    ("selfie", "自拍", ["a selfie taken with a phone at arm's length"]),
    ("landscape", "風景", ["a landscape photo of mountains", "a photo of a lake or the sea",
                          "a scenic nature photo", "a photo of a forest or countryside"]),
    ("city", "城市街景", ["a photo of a city street", "a photo of an old town with houses",
                        "a cityscape skyline photo"]),
    ("architecture", "建築地標", ["a photo of a famous landmark", "a photo of a church or cathedral",
                               "a photo of a castle or palace", "a photo of a building's architecture"]),
    ("interior", "室內", ["a photo of a building interior", "a photo of a room inside a building"]),
    ("museum", "展覽藝術", ["a photo of paintings or sculptures in a museum", "a photo of a museum exhibit"]),
    ("food", "美食", ["a photo of food on a plate", "a photo of a meal at a restaurant", "a photo of drinks or coffee"]),
    ("animal", "動物", ["a photo of an animal", "a photo of a pet", "a photo of animals at a zoo", "a photo of a bird"]),
    ("plants", "花草", ["a close-up photo of flowers", "a photo of plants in a garden"]),
    ("transport", "交通", ["a photo of a train or train station", "a photo of an airplane or airport",
                         "a photo of a car or bus", "a photo of a boat"]),
    ("night", "夜景", ["a photo of a city at night", "a night photo with lights"]),
    ("document", "記錄翻拍", ["a photo of a sign or information board", "a photo of a document or text",
                          "a photo of a ticket, receipt or menu", "a screenshot", "a photo of a map"]),
]
PEOPLE_CATS = {"portrait", "group", "selfie"}
LABEL_ZH = {k: zh for k, zh, _ in CATEGORIES}

REASON_ZH = {
    "eyes closed": "閉眼", "someone's eyes closed": "有人閉眼", "missed focus": "失焦（焦點在背景）",
    "blurry face": "臉糊", "blurry": "整張模糊／手震", "too dark": "太暗救不回", "blown out": "大面積過曝",
    "duplicate": "連拍重複（組內有更好的）", "accidental": "誤拍", "face cut off": "臉被裁切",
    "eyes half closed": "眼睛半閉／往下看", "similar": "相似照（同場景另一個角度）",
}


def model_path(name="face_landmarker.task"):
    p = MODEL_DIR / name
    if not p.exists() or p.stat().st_size < 100_000:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        print(f"downloading {name} -> {p} (one-time)", flush=True)
        tmp = p.with_suffix(".part")
        urllib.request.urlretrieve(MODEL_URLS[name], tmp)
        tmp.rename(p)
    return p


# ----------------------------------------------------------------------------- faces / eyes
_CASC = None
_LM = None


def _landmarker():
    global _LM
    if _LM is None:
        import sys
        # mediapipe optionally imports tensorflow (docs only); a TF built for numpy 1.x crashes the
        # import under numpy 2 with a non-ImportError. Hide TF unless the caller already loaded it.
        sys.modules.setdefault("tensorflow", None)  # type: ignore[arg-type]
        os.environ.setdefault("GLOG_minloglevel", "2")
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision
        opts = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path())),
            running_mode=vision.RunningMode.IMAGE, num_faces=1, output_face_blendshapes=True,
            output_facial_transformation_matrixes=True, min_face_detection_confidence=0.4,
            min_face_presence_confidence=0.4)
        det = vision.FaceDetector.create_from_options(vision.FaceDetectorOptions(
            base_options=BaseOptions(model_asset_path=str(model_path("blaze_face_short_range.tflite"))),
            running_mode=vision.RunningMode.IMAGE, min_detection_confidence=0.5))
        _LM = (mp, vision.FaceLandmarker.create_from_options(opts), det)
    return _LM


def _haar(gray):
    """Haar boxes in pixel coords of `gray`; equalised pass then CLAHE pass (dark/backlit faces)."""
    global _CASC
    if _CASC is None:
        _CASC = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    h, w = gray.shape
    s = 1000.0 / max(h, w)
    g = cv2.resize(gray, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    found = []
    for prep, neigh in ((cv2.equalizeHist(g), 5),
                        (cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(g), 4)):
        for (x, y, fw, fh) in _CASC.detectMultiScale(prep, scaleFactor=1.1, minNeighbors=neigh, minSize=(22, 22)):
            b = [x / s, y / s, fw / s, fh / s]
            if all(_iou(b, o) < 0.3 for o in found):
                found.append(b)
    return found


def _iou(a, b):
    ax0, ay0, ax1, ay1 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx0, by0, bx1, by1 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    iw, ih = max(0, min(ax1, bx1) - max(ax0, bx0)), max(0, min(ay1, by1) - max(ay0, by0))
    inter = iw * ih
    return inter / (a[2] * a[3] + b[2] * b[3] - inter + 1e-9)


def sharpness(gray):
    """Scale-tolerant focus measure on a uint8/float gray patch: Tenengrad energy at the native scale
    relative to the same patch blurred (σ=1.5). In-focus detail loses most of its gradient energy when
    blurred (ratio high); an already-soft patch barely changes (ratio → 1). Contrast-normalised, so a
    pale face is not 'blurrier' than a dark one."""
    g = gray.astype(np.float32)
    g = (g - g.mean()) / (g.std() + 1e-3)
    def ten(x):
        gx = cv2.Sobel(x, cv2.CV_32F, 1, 0, ksize=3); gy = cv2.Sobel(x, cv2.CV_32F, 0, 1, ksize=3)
        return float(np.percentile(gx * gx + gy * gy, 90))
    return ten(g) / (ten(cv2.GaussianBlur(g, (0, 0), 1.5)) + 1e-6)


def _lapnorm(gray):
    """Laplacian variance / intensity variance on the patch resampled to 128px wide."""
    g = gray.astype(np.float32)
    g = cv2.resize(g, (128, max(8, int(128 * g.shape[0] / g.shape[1]))), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(g, cv2.CV_32F).var() / (g.var() + 1e-3))


def _pitch_yaw(mat):
    r = np.asarray(mat)[:3, :3]
    pitch = float(np.degrees(np.arctan2(-r[2, 1], r[2, 2])))
    yaw = float(np.degrees(np.arcsin(np.clip(r[2, 0], -1, 1))))
    return pitch, yaw


def faces_and_eyes(arr):
    """arr uint8 RGB (≈2000px long side). Returns faces sorted by size (desc), each:
    box (normalised x,y,w,h), px (face width in pixels at this scale), blink_l/blink_r (0..1, 1 = closed),
    pitch/yaw (deg), sharp (sharpness() on the face), crop (uint8 220px, for sheets)."""
    mp, lm, det = _landmarker()
    H, W = arr.shape[:2]
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    out = []
    for (x, y, bw, bh) in _haar(gray):
        cx, cy, side = x + bw / 2, y + bh / 2, max(bw, bh) * 1.9
        x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
        x1, y1 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
        crop = arr[y0:y1, x0:x1]
        if crop.shape[0] < 20 or crop.shape[1] < 20:
            continue
        big = cv2.resize(crop, (384, 384), interpolation=cv2.INTER_CUBIC if crop.shape[0] < 384 else cv2.INTER_AREA)
        mimg = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(big))
        # BlazeFace must agree there is a face in the crop: the landmarker alone happily fits a face
        # mesh onto lamp posts, screens and lions (Haar false positives)
        dets = det.detect(mimg).detections
        conf = max((d.categories[0].score for d in dets), default=0.0)
        if conf < 0.6:
            continue
        res = lm.detect(mimg)
        if not res.face_landmarks:
            continue
        bs = {c.category_name: c.score for c in res.face_blendshapes[0]}
        pitch, yaw = _pitch_yaw(res.facial_transformation_matrixes[0]) if res.facial_transformation_matrixes else (0, 0)
        # sharpness on the inner face (Haar box, no hair/background), native pixels, ≤160px
        fx0, fy0 = int(x + bw * 0.15), int(y + bh * 0.15)
        inner = gray[fy0:int(y + bh * 0.9), fx0:int(x + bw * 0.85)]
        if inner.shape[1] > 160:
            inner = cv2.resize(inner, (160, int(160 * inner.shape[0] / inner.shape[1])), interpolation=cv2.INTER_AREA)
        lms = res.face_landmarks[0]
        xs = [p.x for p in lms]; ys = [p.y for p in lms]
        cut = (x0 == 0 and min(xs) < 0.02) or (y0 == 0 and min(ys) < 0.02) or \
              (x1 == W and max(xs) > 0.98) or (y1 == H and max(ys) > 0.98)
        out.append({"box": [x / W, y / H, bw / W, bh / H], "px": int(bw), "conf": round(conf, 3),
                    "blink_l": round(bs.get("eyeBlinkLeft", 0), 3), "blink_r": round(bs.get("eyeBlinkRight", 0), 3),
                    "squint": round((bs.get("eyeSquintLeft", 0) + bs.get("eyeSquintRight", 0)) / 2, 3),
                    "look_down": round((bs.get("eyeLookDownLeft", 0) + bs.get("eyeLookDownRight", 0)) / 2, 3),
                    "pitch": round(pitch, 1), "yaw": round(yaw, 1),
                    "sharp": round(sharpness(inner), 3) if inner.size else None,
                    "lapn": round(_lapnorm(inner), 4) if inner.size else None, "cut": bool(cut),
                    "crop": cv2.resize(crop, (220, 220), interpolation=cv2.INTER_AREA)})
    out.sort(key=lambda f: -f["px"])
    return out


def _face_one(args):
    path, thumb_dir = args
    try:
        im = Image.open(path)
        im.draft("RGB", (2000, 2000))
        im = ImageOps.exif_transpose(im).convert("RGB")
        if max(im.size) > 2000:
            im.thumbnail((2000, 2000), Image.LANCZOS)
        arr = np.asarray(im, dtype=np.uint8)
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        # frame focus: best 3x4 tile at ~1000px (the subject may be small; bokeh elsewhere is fine)
        s = 1000.0 / max(gray.shape)
        g = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        th, tw = g.shape[0] // 3, g.shape[1] // 4
        tiles = [g[i * th:(i + 1) * th, j * tw:(j + 1) * tw] for i in range(3) for j in range(4)]
        tsharp = sorted((sharpness(t) for t in tiles if t.std() > 4), reverse=True)
        frame_sharp = float(np.mean(tsharp[:2])) if tsharp else 1.0
        detail = float(g.std())
        faces = faces_and_eyes(arr)
        for k, f in enumerate(faces):
            fp = Path(thumb_dir) / f"{Path(path).stem}_f{k}.jpg"
            Image.fromarray(f.pop("crop")).save(fp, quality=85)
            f["thumb"] = str(fp)
        return {"file": str(path), "frame_sharp": round(frame_sharp, 3), "detail": round(detail, 2),
                "people": faces, "v": FACE_V}
    except Exception as e:
        return {"file": str(path), "error": f"faces: {e}", "v": FACE_V}


def scan_faces(files, out_dir, jobs=3):
    out_dir = Path(out_dir)
    thumb_dir = out_dir / "thumbs"; thumb_dir.mkdir(parents=True, exist_ok=True)
    cache_p = out_dir / "faces.json"
    known = {}
    if cache_p.exists():
        known = {r["file"]: r for r in json.loads(cache_p.read_text(encoding="utf-8-sig")) if r.get("v") == FACE_V and "error" not in r}
    todo = [str(f) for f in files if str(f) not in known]
    if todo:
        with ProcessPoolExecutor(jobs) as ex:
            for i, r in enumerate(ex.map(_face_one, [(f, str(thumb_dir)) for f in todo], chunksize=4)):
                known[r["file"]] = r
                if (i + 1) % 100 == 0:
                    print(f"  faces {i + 1}/{len(todo)}", flush=True)
                    cache_p.write_text(json.dumps(list(known.values())), encoding="utf-8")
        cache_p.write_text(json.dumps(list(known.values())), encoding="utf-8")
    return {str(f): known.get(str(f), {}) for f in files}


# ----------------------------------------------------------------------------- categories (CLIP)
def load_categories(path=None):
    if not path:
        return CATEGORIES
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return [(c["key"], c.get("label", c["key"]), c["prompts"]) for c in data]


def classify(rows, out_dir, categories=CATEGORIES, batch=32):
    """Zero-shot CLIP over cached thumbnails. Returns {file: {key: prob}} (cached per category set)."""
    sig = json.dumps(categories, ensure_ascii=False)
    cache_p = Path(out_dir) / "clip.json"
    cache = {}
    if cache_p.exists():
        c = json.loads(cache_p.read_text(encoding="utf-8-sig"))
        if c.get("sig") == sig:
            cache = c["probs"]
    todo = [r for r in rows if r["file"] not in cache and r.get("thumb")]
    if todo:
        import sys
        os.environ.setdefault("USE_TF", "0")  # a numpy-1 TensorFlow install would crash the import
        os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
        sys.modules.setdefault("tensorflow", None)  # type: ignore[arg-type]
        import torch
        from transformers import CLIPModel, CLIPProcessor
        print(f"  CLIP: {len(todo)} frames ({CLIP_ID}; first run downloads ~600MB)", flush=True)
        model = CLIPModel.from_pretrained(CLIP_ID).eval()
        proc = CLIPProcessor.from_pretrained(CLIP_ID)
        dev = "mps" if torch.backends.mps.is_available() else "cpu"
        model = model.to(dev)
        prompts = [p for _, _, ps in categories for p in ps]
        owner = [i for i, (_, _, ps) in enumerate(categories) for _ in ps]
        with torch.no_grad():
            t = proc(text=prompts, return_tensors="pt", padding=True).to(dev)
            tf = model.get_text_features(**t)
            tf = tf / tf.norm(dim=-1, keepdim=True)
            # one mean embedding per category
            ct = torch.stack([tf[[j for j, o in enumerate(owner) if o == i]].mean(0) for i in range(len(categories))])
            ct = ct / ct.norm(dim=-1, keepdim=True)
            for i in range(0, len(todo), batch):
                chunk = todo[i:i + batch]
                ims = [Image.open(r["thumb"]).convert("RGB") for r in chunk]
                px = proc(images=ims, return_tensors="pt")["pixel_values"].to(dev)
                f = model.get_image_features(pixel_values=px)
                f = f / f.norm(dim=-1, keepdim=True)
                p = (100.0 * f @ ct.T).softmax(-1).cpu().numpy()
                for r, row in zip(chunk, p):
                    cache[r["file"]] = {categories[k][0]: round(float(v), 4) for k, v in enumerate(row)}
                if (i // batch) % 10 == 9:
                    print(f"  CLIP {i + len(chunk)}/{len(todo)}", flush=True)
        cache_p.write_text(json.dumps({"sig": sig, "probs": cache}), encoding="utf-8")
    return cache


# ----------------------------------------------------------------------------- events
def events(rows, gap_h=3.0):
    """Split the time-sorted shoot wherever two frames are > gap_h hours apart (and at each new
    calendar day). Returns {file: event_name}; event_name = 'YYYY-MM-DD' or 'YYYY-MM-DD_2'..."""
    ok = sorted([r for r in rows if r.get("time")], key=lambda r: r["time"])
    out, prev, day, n = {}, None, None, 0
    for r in ok:
        d = datetime.fromtimestamp(r["time"]).strftime("%Y-%m-%d")
        if prev is None or d != day:
            day, n = d, 1
        elif r["time"] - prev > gap_h * 3600:
            n += 1
        out[r["file"]] = d if n == 1 else f"{d}_{n}"
        prev = r["time"]
    for r in rows:
        out.setdefault(r["file"], "no-date")
    return out


# ----------------------------------------------------------------------------- verdict
def judge(rows, faces, probs, groups, th=None):
    """Attach category / people / reasons / verdict ('delete' | 'review' | 'keep') to every row."""
    th = {"blink": 0.5, "blink_soft": 0.38, "dup_sim": 0.85, "dup_dt": 20, "face_px": 60, "face_soft": 1.0, "focus_gap": 0.9,
          "face_conf": 0.75, "cat_min": 0.3, "frame_blur": 2.2, "frame_soft": 3.0, **(th or {})}
    by_file = {}
    for g in groups:
        for r in g:
            by_file[r["file"]] = (g, r)
    out = []
    for r in rows:
        if "error" in r:
            continue
        fz = faces.get(r["file"], {}) or {}
        # BlazeFace < 0.75 on a 1.6k-frame card was almost always a statue, ornament or crystal
        ppl = [f for f in fz.get("people", []) if f.get("conf", 1) >= th["face_conf"]]
        g, rr = by_file.get(r["file"], ([r], r))
        r = {**r, **rr}
        pr = probs.get(r["file"], {})
        cat = max(pr, key=pr.get) if pr else "other"
        if pr and pr[cat] < th["cat_min"]:
            cat = "other"  # CLIP unsure (softmax over ~14 classes): better 其他 than a wrong album
        # a real (landmark-confirmed) face that fills a decent part of the frame wins over CLIP's scene guess
        main = [f for f in ppl if f["px"] >= max(th["face_px"], 0.5 * ppl[0]["px"])] if ppl else []
        if main and cat not in PEOPLE_CATS and main[0]["box"][2] > 0.07:
            cat = "group" if len(main) >= 3 else "portrait"
        reasons, soft = [], []
        # --- people checks (only landmark-confirmed faces big enough to judge)
        judged = [f for f in main if f["px"] >= th["face_px"]]
        def blink(f):
            return min(f["blink_l"], f["blink_r"])
        closed = [f for f in judged if blink(f) >= th["blink"]]
        half = [f for f in judged if th["blink_soft"] <= blink(f) < th["blink"]]
        if closed and (closed[0] is judged[0] or len(judged) <= 2):
            reasons.append("eyes closed")
        elif closed:
            soft.append("someone's eyes closed")
        elif half:
            soft.append("eyes half closed")
        if judged:
            f0 = judged[0]
            fs, frm = f0["sharp"] or 0, fz.get("frame_sharp", 0)
            if fs < th["face_soft"] and frm - fs > th["focus_gap"]:
                reasons.append("missed focus")
            elif fs < th["face_soft"] * 0.85:
                reasons.append("blurry face")
            if f0["cut"] and f0["box"][2] > 0.12:
                soft.append("face cut off")
        # --- frame checks
        # whole-frame blur: cull's shoot/burst-relative veto alone fires on fog, dusk and silhouettes
        # (low detail, not blur). It counts only when the scale-relative frame focus agrees:
        # calibrated fog/overcast lakes 5-10, visibly soft/shaken frames 1.5-2.5.
        fsh = fz.get("frame_sharp", 9)
        for v in r.get("veto", []):
            if v in ("too dark", "blown out"):
                reasons.append(v)
            elif v == "blurry" and fsh < th["frame_blur"] and not judged:
                reasons.append("blurry")
            elif v == "blurry" and fsh < th["frame_soft"]:
                soft.append("blurry")
            elif v == "blurry face" and "missed focus" not in reasons and "blurry face" not in reasons:
                soft.append(v)
        if fz.get("detail", 99) < 6 and r.get("lum", 1) < 0.12:
            reasons.append("accidental")
        # --- duplicates: not the best of a multi-frame burst whose best frame is itself fine
        if len(g) > 1 and r.get("rank", 1) > 1:
            reasons.append("duplicate")
        verdict = "delete" if reasons else ("review" if soft else "keep")
        out.append({"file": r["file"], "thumb": r.get("thumb"), "time": r.get("time"), "category": cat,
                    "cat_prob": round(pr.get(cat, 0), 3), "faces": len(ppl), "people": ppl,
                    "frame_sharp": fz.get("frame_sharp"), "group": r.get("group"), "group_size": len(g),
                    "rank": r.get("rank", 1), "tech": r.get("tech"), "reasons": reasons, "soft": soft,
                    "verdict": verdict, "_th": th})
    return out


def rebalance_duplicates(items):
    """A burst whose top technical pick is itself flagged (closed eyes / missed focus) should keep its
    best *clean* frame instead: move the keep to the highest-ranked frame without hard reasons.
    Only near-identical frames shot close to the keeper are 'duplicate' (delete); a re-framed or
    later shot of the same scene is just 'similar' (review) — that is a taste call, not a defect.
    Calibrated on a 1.6k-frame card: ~1/3 of non-keeper burst frames reach structure-NCC ≥0.85 within 20 s."""
    from cull import _sim_features
    feats = {}

    def sim(a, b):
        fa = feats.setdefault(a["file"], _sim_features(a["thumb"]))
        fb = feats.setdefault(b["file"], _sim_features(b["thumb"]))
        return float((fa[0] * fb[0]).mean()) if fa is not None and fb is not None else 0.0

    groups = {}
    for it in items:
        if it["group_size"] > 1:
            groups.setdefault(it["group"], []).append(it)
    for g in groups.values():
        g.sort(key=lambda it: it["rank"])
        clean = [it for it in g if not [x for x in it["reasons"] if x != "duplicate"]]
        keeper = clean[0] if clean else g[0]
        for it in g:
            rs = [x for x in it["reasons"] if x != "duplicate"]
            sf = [x for x in it["soft"] if x != "similar"]
            if it is not keeper:
                th = it.get("_th", {})
                dt = abs((it["time"] or 0) - (keeper["time"] or 0))
                if sim(it, keeper) >= th.get("dup_sim", 0.85) and dt <= th.get("dup_dt", 20):
                    rs.append("duplicate")
                else:
                    sf.append("similar")
            it["reasons"], it["soft"] = rs, sf
            it["verdict"] = "delete" if rs else ("review" if it["soft"] else "keep")
            it["burst_keeper"] = keeper["file"]
    for it in items:
        it.pop("_th", None)
    return items


# ----------------------------------------------------------------------------- HTML report
def write_html(items, summary, dst, reason_zh=REASON_ZH):
    """Offline single-page report next to the thumbnails: delete suggestions (pre-checked, export the
    checked list), review, and the album by event x category. Thumbnails are referenced relatively."""
    dst = Path(dst)
    base = dst.parent

    def rel(p):
        return os.path.relpath(p, base) if p else ""

    data = []
    for it in items:
        ppl = it.get("people") or []
        data.append({"f": it["file"], "n": Path(it["file"]).name, "t": rel(it.get("thumb")),
                     "face": rel(ppl[0]["thumb"]) if ppl else "", "v": it["verdict"],
                     "r": [reason_zh.get(x, x) for x in it["reasons"]], "s": [reason_zh.get(x, x) for x in it["soft"]],
                     "c": it["category_zh"], "e": it["event"]})
    html = _HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False)) \
                .replace("__SUMMARY__", json.dumps(summary, ensure_ascii=False))
    dst.write_text(html, encoding="utf-8")
    return dst


_HTML = r"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>相簿整理報告</title>
<style>
:root{--bg:#f6f5f2;--fg:#1d1d1b;--mut:#6b6a66;--card:#fff;--line:#e2e0da;--del:#c8372d;--rev:#c98a12;--ok:#2f7d4f}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe7;--mut:#9c9a94;--card:#222220;--line:#34332f;--del:#ef6a5f;--rev:#e3a93a;--ok:#5fb884}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"PingFang TC",sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px}
h1{font-size:18px;margin:0 0 6px}.stats{color:var(--mut);font-size:13px}
nav{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}
nav button,.act{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:6px;padding:5px 10px;cursor:pointer;font:inherit}
nav button.on{background:var(--fg);color:var(--bg)}
main{padding:8px 16px 60px}h2{font-size:15px;margin:22px 0 8px}h3{font-size:13px;color:var(--mut);margin:14px 0 6px;font-weight:600}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:8px}
.c{background:var(--card);border:1px solid var(--line);border-radius:6px;overflow:hidden;position:relative}
.c img.t{width:100%;aspect-ratio:3/2;object-fit:contain;background:#111;display:block;cursor:zoom-in}
.c img.fc{position:absolute;right:4px;top:calc((100% - 46px) - 58px);width:58px;height:58px;border:2px solid #fff;border-radius:4px}
.c .m{padding:5px 7px;font-size:12px;display:flex;gap:6px;align-items:flex-start}
.c .m span{flex:1;min-width:0;overflow-wrap:anywhere}.tag{color:var(--del)}.tag.s{color:var(--rev)}
.c.off{opacity:.45}.bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}
#zoom{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;align-items:center;justify-content:center;z-index:9}
#zoom img{max-width:94vw;max-height:90vh}
</style></head><body>
<header><h1>相簿整理報告</h1><div class="stats" id="stats"></div><nav id="nav"></nav></header>
<main id="main"></main><div id="zoom" onclick="this.style.display='none'"><img></div>
<script>
const D=__DATA__, S=__SUMMARY__;
const keep=new Set(); // files un-checked from the delete list
try{JSON.parse(localStorage.getItem('org-keep')||'[]').forEach(f=>keep.add(f))}catch(e){}
const save=()=>{try{localStorage.setItem('org-keep',JSON.stringify([...keep]))}catch(e){}};
document.getElementById('stats').textContent=`${S.frames} 張 · 保留 ${S.keep} · 待確認 ${S.review} · 建議刪除 ${S.delete} · ${Object.keys(S.events).length} 個活動`;
const tabs={del:'建議刪除',rev:'待確認',album:'分類相簿'};let cur='del';
const nav=document.getElementById('nav');
for(const k in tabs){const b=document.createElement('button');b.textContent=tabs[k];b.onclick=()=>{cur=k;render()};b.dataset.k=k;nav.appendChild(b)}
function card(d,check){
  const c=document.createElement('div');c.className='c'+(check&&keep.has(d.f)?' off':'');
  const im=document.createElement('img');im.className='t';im.loading='lazy';im.src=d.t;im.onclick=()=>{const z=document.getElementById('zoom');z.querySelector('img').src=d.t;z.style.display='flex'};c.appendChild(im);
  if(d.face&&(d.r.concat(d.s).some(x=>/閉眼|半閉|焦|臉/.test(x)))){const f=document.createElement('img');f.className='fc';f.src=d.face;c.appendChild(f)}
  const m=document.createElement('div');m.className='m';
  if(check){const cb=document.createElement('input');cb.type='checkbox';cb.checked=!keep.has(d.f);cb.onchange=()=>{cb.checked?keep.delete(d.f):keep.add(d.f);c.classList.toggle('off',!cb.checked);save();count()};m.appendChild(cb)}
  const s=document.createElement('span');s.innerHTML=`${d.n}<br>`+d.r.map(x=>`<b class=tag>${x}</b>`).join(' ')+' '+d.s.map(x=>`<b class="tag s">${x}</b>`).join(' ');m.appendChild(s);c.appendChild(m);return c}
function section(title,list,check){const h=document.createElement('h2');h.textContent=`${title}（${list.length}）`;const g=document.createElement('div');g.className='grid';list.forEach(d=>g.appendChild(card(d,check)));return [h,g]}
function count(){const n=D.filter(d=>d.v==='delete'&&!keep.has(d.f)).length;const el=document.getElementById('cnt');if(el)el.textContent=`已勾選 ${n} 張`}
function render(){
  [...nav.children].forEach(b=>b.classList.toggle('on',b.dataset.k===cur));
  const M=document.getElementById('main');M.innerHTML='';
  if(cur==='del'){
    const bar=document.createElement('div');bar.className='bar';
    bar.innerHTML='<span id=cnt></span><button class=act id=exp>匯出勾選清單 (.txt)</button><span class=stats>取消勾選＝要留下。原檔不會被動到，清單交給 AI 助理 或自己在 檔案管理員／相簿刪除。</span>';
    M.appendChild(bar);
    bar.querySelector('#exp').onclick=()=>{const t=D.filter(d=>d.v==='delete'&&!keep.has(d.f)).map(d=>d.f).join('\n')+'\n';const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([t],{type:'text/plain'}));a.download='delete_confirmed.txt';a.click()};
    const by={};D.filter(d=>d.v==='delete').forEach(d=>{(by[d.r[0]]=by[d.r[0]]||[]).push(d)});
    const P=['閉眼','失焦','臉糊','整張模糊','誤拍','太暗','大面積過曝','連拍重複'],pi=k=>{const i=P.findIndex(p=>k.startsWith(p));return i<0?99:i};
    Object.entries(by).sort((a,b)=>pi(a[0])-pi(b[0])).forEach(([k,l])=>section(k,l,true).forEach(e=>M.appendChild(e)));count();
  }else if(cur==='rev'){
    const by={};D.filter(d=>d.v==='review').forEach(d=>{(by[d.s[0]]=by[d.s[0]]||[]).push(d)});
    Object.entries(by).forEach(([k,l])=>section(k,l,false).forEach(e=>M.appendChild(e)));
  }else{
    const ev={};D.filter(d=>d.v!=='delete').forEach(d=>{((ev[d.e]=ev[d.e]||{})[d.c]=ev[d.e][d.c]||[]).push(d)});
    for(const [e,cs] of Object.entries(ev)){const h=document.createElement('h2');h.textContent=e;M.appendChild(h);
      for(const [c,l] of Object.entries(cs).sort((a,b)=>b[1].length-a[1].length)){const h3=document.createElement('h3');h3.textContent=`${c}（${l.length}）`;M.appendChild(h3);const g=document.createElement('div');g.className='grid';l.forEach(d=>g.appendChild(card(d,false)));M.appendChild(g)}}
  }
}
render();
</script></body></html>
"""
