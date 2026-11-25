"""Semantic mattes for any camera (Sony / Canon / Android ... no iPhone auxiliary images needed).

Writes the same sidecar layout as `pe.py mattes`, so `{"type": "matte", "name": ...}` masks and
`skin_anchor` work unchanged:

  <stem>.mattes/{portrait,skin,eyes,teeth,lips}.png + meta.json

- portrait: YOLOv8-seg person polygons of the subject(s), edge-refined with a guided filter.
  If YOLO finds nobody but a face is visible, a face-anchored head+shoulders ellipse is used.
- skin / eyes / teeth / lips: MediaPipe FaceLandmarker (478 points). Faces are detected on an
  upscaled crop of each subject, so small faces in wide travel shots are still found.
  Skin is the face oval only (minus eyes, brows, lips; hair over the face is excluded by brightness);
  neck / hands are deliberately left out because colour-based body skin leaks into coats and hair.
"""
import json
from pathlib import Path

import cv2
import numpy as np

import engine as E
import masks as M

OVAL = [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377, 152, 148, 176,
        149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109]
BROW_L = [70, 63, 105, 66, 107, 55, 65, 52, 53, 46]
BROW_R = [300, 293, 334, 296, 336, 285, 295, 282, 283, 276]
LIPS = [61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 409, 270, 269, 267, 0, 37, 39, 40, 185]
MOUTH_IN = [78, 191, 80, 81, 82, 13, 312, 311, 310, 415, 308, 324, 318, 402, 317, 14, 87, 178, 88, 95]
WORK = 2000   # matte resolution (long side); develop rescales mattes to the image


def _poly(h, w, pts, grow=0):
    m = np.zeros((h, w), np.float32)
    cv2.fillPoly(m, [pts.astype(np.int32)], 1.0)
    if grow:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1))
        m = cv2.dilate(m, k)
    return m


def build(path, out_dir=None, keep=None):
    """Write <stem>.mattes/ for one image. keep = person ids from `pe.py people` (default: role == subject)."""
    import erase as R
    import portrait as P
    img = E.load_image(str(path), WORK)
    h, w = img.shape[:2]
    ps = R.people(img)
    subj = [p for p in ps if (p["id"] in keep if keep is not None else p["role"] == "subject")]
    port = np.zeros((h, w), np.float32)
    for p in subj:
        port = np.maximum(port, _poly(h, w, p["poly"]))
    if port.max() > 0:
        port = np.clip(M.guided_refine(port, img), 0, 1)

    faces = []
    for bx, by, bw, bh in [p["box"] for p in subj] or [[0, 0, 1, 1]]:
        x0, y0 = int(max(0, bx - bw * 0.2) * w), int(max(0, by - bh * 0.05) * h)
        x1, y1 = int(min(1, bx + bw * 1.2) * w), int(min(1, by + bh * 0.6) * h)
        for f in P.detect(img[y0:y1, x0:x1], max_side=1600, min_frac=0.05):
            f = f + np.array([x0, y0], np.float32)
            if not any(np.abs(f[1] - g[1]).sum() < 10 for g in faces):
                faces.append(f)
    if not subj and faces:
        fb = []
        for f in faces:
            fx, fy = f[:, 0].min(), f[:, 1].min()
            fb.append([fx / w, fy / h, (f[:, 0].max() - fx) / w, (f[:, 1].max() - fy) / h])
        port = np.clip(M.guided_refine(M.build_mask({"type": "subject"}, img, faces=fb), img), 0, 1)

    hsv = E.rgb_to_hsv(img)
    V = hsv[..., 2]
    skin_col = E.gblur(E.skin_mask(hsv), 2)
    skin = np.zeros((h, w), np.float32); eyes = skin.copy(); teeth = skin.copy(); lips = skin.copy()
    for f in faces:
        g = max(1, int((f[P.JAW, 0].max() - f[P.JAW, 0].min()) * 0.015))
        eye = np.maximum(_poly(h, w, f[P.L_EYE], g), _poly(h, w, f[P.R_EYE], g))
        holes = np.maximum.reduce([eye, _poly(h, w, f[BROW_L], g), _poly(h, w, f[BROW_R], g), _poly(h, w, f[LIPS], g)])
        inner = _poly(h, w, f[OVAL]) * (1 - holes)
        ref = np.median(V[inner > 0.5]) if (inner > 0.5).any() else 0.5
        not_hair = E.smoothstep(ref * 0.45, ref * 0.7, V)      # hair over the face is much darker than skin
        skin = np.maximum(skin, inner * not_hair * np.clip(0.55 + skin_col, 0, 1))
        eyes = np.maximum(eyes, eye)
        mi = _poly(h, w, f[MOUTH_IN])
        teeth = np.maximum(teeth, mi * ((V > 0.45) & (hsv[..., 1] < 0.4)).astype(np.float32))
        lips = np.maximum(lips, _poly(h, w, f[LIPS]) * (1 - mi))
    skin = E.gblur(skin, max(1, w * 0.0015))

    path = Path(path)
    d = (Path(out_dir) if out_dir else path.parent) / (path.stem + ".mattes")
    d.mkdir(parents=True, exist_ok=True)
    for n, m in dict(portrait=port, skin=skin, eyes=E.gblur(eyes, 1), teeth=E.gblur(teeth, 1),
                     lips=E.gblur(lips, 1)).items():
        cv2.imwrite(str(d / f"{n}.png"), (np.clip(m, 0, 1) * 255).astype(np.uint8))
    (d / "meta.json").write_text(json.dumps({"orientation": 1, "source": "yolo+mediapipe"}))
    return dict(people=len(ps), subject=[p["id"] for p in subj], faces=len(faces),
                portrait=round(float(port.mean()), 3), skin=round(float(skin.mean()), 4))
