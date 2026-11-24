"""Remove bystanders: person instance segmentation (YOLOv8-seg) + effect-aware inpainting.

Two-step, so the AI assistant (or the user) decides who stays:
  1. people(img)  -> numbered list of every detected person (area, box, face, role guess)
  2. erase(img, ids) -> those people (and their shadows/reflections) removed, region by region

Fill backends (see removers.py): osor (default on CUDA, removes shadows too), objectclear
(non-commercial licence), lama (fast fallback, used on Mac/CPU).

Fidelity rules (see SKILL.md): this *invents* background where people stood. Only remove
people who are not the subject; never touch the subject; check every result at 100%;
skip removal when a bystander overlaps the subject or covers a large/complex area.

Models (downloaded once to ~/.cache/photo-edit/models, ~230MB total):
  yolov8s-seg.pt  https://github.com/ultralytics/assets/releases/download/v8.1.0/yolov8s-seg.pt
  big-lama.pt     https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt
"""
from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import cv2
import numpy as np


MODEL_DIR = Path(os.environ.get("PHOTO_EDIT_MODELS", Path.home() / ".cache" / "photo-edit" / "models"))
URLS = {
    "yolov8s-seg.pt": "https://github.com/ultralytics/assets/releases/download/v8.1.0/yolov8s-seg.pt",
    "big-lama.pt": "https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt",
}
_YOLO = None
_LAMA = None


def model_path(name):
    p = MODEL_DIR / name
    if not p.exists() or p.stat().st_size < 1_000_000:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        print(f"downloading {name} -> {p} (one-time)", flush=True)
        tmp = p.with_suffix(".part")
        urllib.request.urlretrieve(URLS[name], tmp)
        tmp.rename(p)
    return p


def _device():
    import torch
    if torch.cuda.is_available():
        return "cuda"
    return "mps" if torch.backends.mps.is_available() else "cpu"


# ----------------------------------------------------------------------------- detection
def people(img, conf=0.2, imgsz=1920):
    """Detect every person. img float sRGB HxWx3. Returns list of dicts sorted by area (desc):
    id, box [x,y,w,h] (normalised), area (frac of frame), conf, poly (pixel coords), role guess."""
    global _YOLO
    import contextlib
    import io
    import warnings
    # ultralytics imports pandas; with a NumPy-2 / old-pyarrow mix that prints long, harmless
    # tracebacks. Keep them out of the user's output (real import failures still raise).
    with contextlib.redirect_stderr(io.StringIO()), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from ultralytics import YOLO
    if _YOLO is None:
        # ultralytics 8.1 checkpoints pickle model classes; torch>=2.6 defaults to weights_only.
        # The file is the official ultralytics GitHub release (URLS above), so allow it for this load only.
        prev = os.environ.get("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD")
        os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                _YOLO = YOLO(str(model_path("yolov8s-seg.pt")))
        finally:
            if prev is None:
                os.environ.pop("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", None)
            else:
                os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = prev
    h, w = img.shape[:2]
    # detect on a fixed 1920px copy so ids/outlines are identical for previews and full-res renders
    # (small bystanders are missed at lower resolutions)
    s = 1920.0 / max(h, w)
    det = cv2.resize(img, (int(round(w * s)), int(round(h * s))), interpolation=cv2.INTER_AREA)
    bgr = cv2.cvtColor((np.clip(det, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2BGR)
    res = _YOLO.predict(bgr, imgsz=imgsz, conf=conf, classes=[0], verbose=False, retina_masks=False)[0]
    out = []
    if res.masks is None:
        return out
    for poly, box, c in zip(res.masks.xy, res.boxes.xyxy.cpu().numpy(), res.boxes.conf.cpu().numpy()):
        if len(poly) < 3:
            continue
        poly = poly / s
        x0, y0, x1, y1 = box / s
        m = np.zeros((h, w), np.uint8)
        cv2.fillPoly(m, [poly.astype(np.int32)], 1)
        out.append({"box": [float(x0 / w), float(y0 / h), float((x1 - x0) / w), float((y1 - y0) / h)],
                    "area": float(m.mean()), "conf": float(c), "poly": poly.astype(np.float32)})
    out.sort(key=lambda p: -p["area"])
    for i, p in enumerate(out):
        p["id"] = i
    _guess_roles(out)
    return out


def _guess_roles(ps):
    """Subject = the big, central people (relative to the largest person); the rest = bystanders.
    A frame where everyone is small (landscape with tourists) has no subject."""
    if not ps:
        return
    big = ps[0]["area"]
    for p in ps:
        x, y, w, h = p["box"]
        cx = x + w / 2
        central = 0.15 < cx < 0.85
        if big >= 0.02 and p["area"] >= 0.35 * big and (central or p["area"] >= 0.6 * big):
            p["role"] = "subject"
        else:
            p["role"] = "bystander"


def select(ps, spec):
    """spec: 'bystanders' | 'all' | list of ids to remove | {'keep': [ids]}."""
    if spec in (None, "bystanders", "auto"):
        return [p for p in ps if p["role"] == "bystander"]
    if spec == "all":
        return list(ps)
    if isinstance(spec, dict) and "keep" in spec:
        keep = set(spec["keep"])
        return [p for p in ps if p["id"] not in keep]
    ids = set(spec if isinstance(spec, (list, tuple)) else [spec])
    return [p for p in ps if p["id"] in ids]


def person_mask(shape, chosen, grow=0.10):
    """Union of chosen people, each dilated by `grow` x its height (+ feet/shadow margin)."""
    h, w = shape[:2]
    m = np.zeros((h, w), np.uint8)
    for p in chosen:
        one = np.zeros((h, w), np.uint8)
        cv2.fillPoly(one, [p["poly"].astype(np.int32)], 255)
        ph = p["box"][3] * h
        # proportional to the person, but capped in absolute terms: a person filling half the frame
        # must not swallow the umbrellas and walls around them
        r = int(min(max(3, grow * ph), max(6, 0.012 * max(h, w))))
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        one = cv2.dilate(one, k)
        # extend a little below the feet (contact shadow)
        x0, y0, bw, bh = [int(v) for v in (p["box"][0] * w, p["box"][1] * h, p["box"][2] * w, p["box"][3] * h)]
        y1 = min(h, y0 + bh + int(0.04 * bh))
        one[max(0, y0 + bh - r):y1, max(0, x0 - r // 2):min(w, x0 + bw + r // 2)] = 255
        m = np.maximum(m, one)
    return m


# ----------------------------------------------------------------------------- inpainting
def _lama():
    global _LAMA
    if _LAMA is None:
        import torch
        dev = _device()
        model = torch.jit.load(str(model_path("big-lama.pt")), map_location="cpu")
        model.eval()
        try:
            model = model.to(dev)
        except Exception:
            dev = "cpu"
        _LAMA = (model, dev)
    return _LAMA


def _lama_run(rgb_u8, mask_u8):
    import torch
    model, dev = _lama()
    h, w = mask_u8.shape
    ph, pw = (8 - h % 8) % 8, (8 - w % 8) % 8
    img = np.pad(rgb_u8, ((0, ph), (0, pw), (0, 0)), mode="reflect")
    msk = np.pad(mask_u8, ((0, ph), (0, pw)), mode="reflect")
    t_img = torch.from_numpy(img).permute(2, 0, 1)[None].float().div(255)
    t_msk = (torch.from_numpy(msk)[None, None].float() > 127).float()
    with torch.inference_mode():
        try:
            out = model(t_img.to(dev), t_msk.to(dev))
        except Exception:  # some ops are unsupported on MPS -> CPU
            model_cpu = model.to("cpu")
            out = model_cpu(t_img, t_msk)
    out = out[0].permute(1, 2, 0).clamp(0, 1).cpu().numpy()[:h, :w]
    return out


def inpaint(img, mask, work=2400):
    """Fill mask (uint8 HxW, 255 = remove) region by region. Each region is cropped with
    generous context, run through LaMa at <= `work` px, and pasted back with a feathered edge."""
    h, w = mask.shape
    out = img.copy()
    n, lab, stats, _ = cv2.connectedComponentsWithStats((mask > 127).astype(np.uint8))
    # merge nearby components into crops
    boxes = []
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if area < 16:
            continue
        # context: enough for LaMa to read the surroundings, small enough to keep resolution
        pad = int(min(max(bw, bh) * 1.2, max(bw, bh) * 0.35 + 160) + 48)
        boxes.append([max(0, x - pad), max(0, y - pad), min(w, x + bw + pad), min(h, y + bh + pad)])
    merged = []
    for b in sorted(boxes):
        for m in merged:
            if b[0] < m[2] and b[2] > m[0] and b[1] < m[3] and b[3] > m[1]:
                m[:] = [min(m[0], b[0]), min(m[1], b[1]), max(m[2], b[2]), max(m[3], b[3])]
                break
        else:
            merged.append(list(b))
    for x0, y0, x1, y1 in merged:
        crop = out[y0:y1, x0:x1]
        cm = mask[y0:y1, x0:x1]
        ch, cw = cm.shape
        s = min(1.0, work / max(ch, cw))
        sw, sh = max(8, int(cw * s)), max(8, int(ch * s))
        small = cv2.resize((crop * 255).astype(np.uint8), (sw, sh), interpolation=cv2.INTER_AREA)
        smask = cv2.resize(cm, (sw, sh), interpolation=cv2.INTER_NEAREST)
        filled = _lama_run(small, smask)
        filled = cv2.resize(filled.astype(np.float32), (cw, ch), interpolation=cv2.INTER_CUBIC)
        # restore some fine texture lost by the downscale: add grain-matched noise from the crop
        if s < 1:
            hf = crop - cv2.GaussianBlur(crop, (0, 0), 1.0 / s)
            sd = float(np.median(np.abs(hf[cm < 128]))) * 1.4 if (cm < 128).any() else 0.0
            rng = np.random.default_rng(0)
            noise = cv2.GaussianBlur(rng.standard_normal((ch, cw)).astype(np.float32), (0, 0), 0.6)
            filled = filled + (noise / max(noise.std(), 1e-6) * min(sd, 0.02) * 0.5)[..., None]
        soft = cv2.GaussianBlur((cm > 127).astype(np.float32), (0, 0), 2.5)[..., None]
        out[y0:y1, x0:x1] = crop * (1 - soft) + np.clip(filled, 0, 1) * soft
    return np.clip(out, 0, 1)


def box_mask(shape, boxes, feather=0.15):
    """Extra regions to remove, normalised [x, y, w, h] (e.g. feet showing below a bench)."""
    h, w = shape[:2]
    m = np.zeros((h, w), np.uint8)
    for x, y, bw, bh in boxes or []:
        x0, y0 = int(x * w), int(y * h)
        x1, y1 = int((x + bw) * w), int((y + bh) * h)
        m[max(0, y0):min(h, y1), max(0, x0):min(w, x1)] = 255
    return m


def fill(img, mask, backend="lama", protect=None):
    """Fill `mask` with the chosen backend ('lama' | 'osor' | 'objectclear', already resolved)."""
    if backend == "lama":
        return inpaint(img, mask)
    import removers
    return removers.remove(img, mask, backend, protect)


def erase(img, spec="bystanders", grow=0.10, ps=None, extra=None, second_pass=True, backend="auto"):
    """Remove the selected people (+ `extra` boxes). A second detection pass on the result
    catches half-erased ghosts and people missed the first time inside the touched area.
    backend: 'auto' (osor on CUDA, else lama) | 'osor' | 'objectclear' | 'lama'."""
    import removers
    backend = removers.resolve(backend)
    ps = ps if ps is not None else people(img)
    chosen = select(ps, spec)
    if not chosen and not extra:
        return img, ps, []
    chosen_ids = {p["id"] for p in chosen}
    # protect kept subjects from the soft paste edge, but where a kept polygon overlaps a removed
    # person (YOLO polygons often wrap a neighbour) the explicit removal wins
    subj = person_mask(img.shape, [p for p in ps if p["id"] not in chosen_ids], 0.0)
    subj[person_mask(img.shape, chosen, 0.0) > 0] = 0
    k = max(3, int(0.006 * max(img.shape[:2]))) | 1  # drop slivers left by the subtraction
    subj = cv2.morphologyEx(subj, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    # diffusion backends get a near-tight mask: they find the shadow / reflection themselves and
    # report it, and a fat dilated mask only gives them less real context to copy from
    g = grow if backend == "lama" else min(grow, 0.03)
    m = np.maximum(person_mask(img.shape, chosen, g), box_mask(img.shape, extra))
    out = fill(img, m, backend, protect=subj)
    if second_pass and chosen:
        h, w = m.shape
        near = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (max(3, w // 60),) * 2))
        left = []
        for q in people(out):
            one = np.zeros((h, w), np.uint8)
            cv2.fillPoly(one, [q["poly"].astype(np.int32)], 1)
            overlap_touched = (one & (near > 0)).sum() / max(one.sum(), 1)
            overlap_subject = (one & (subj > 0)).sum() / max(one.sum(), 1)
            if overlap_touched > 0.3 and overlap_subject < 0.2:
                left.append(q)
        if left:
            m2 = person_mask(img.shape, left, g * 1.5)
            out = fill(out, m2, backend, protect=subj)
    return out, ps, [p["id"] for p in chosen]


def overlay(img, ps):
    """Numbered outlines: green = subject (kept by default), red = bystander (removed by default)."""
    vis = (np.clip(img, 0, 1) * 255).astype(np.uint8).copy()
    h, w = vis.shape[:2]
    lw = max(2, w // 500)
    for p in ps:
        col = (40, 210, 60) if p["role"] == "subject" else (230, 40, 40)
        cv2.polylines(vis, [p["poly"].astype(np.int32)], True, col, lw)
        x, y = int(p["box"][0] * w), int(p["box"][1] * h)
        fs = max(0.6, w / 1600)
        cv2.putText(vis, str(p["id"]), (x, max(20, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 0, 0), lw + 3)
        cv2.putText(vis, str(p["id"]), (x, max(20, y - 6)), cv2.FONT_HERSHEY_SIMPLEX, fs, col, lw)
    return vis.astype(np.float32) / 255
