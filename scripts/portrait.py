"""Portrait retouch driven by MediaPipe's 478-point face mesh: under-eye, dodge & burn contour, slim face, bigger eyes.

recipe key:
  "portrait": {"undereye": 0..1,   lift dark circles / bags toward the cheek below (keeps lash line)
               "contour":  0..1,   dodge forehead / nose bridge / cheekbones / chin, burn jaw edge & nose sides
               "slim":     0..1,   pull the lower jaw contour toward the face centre (1 = ~6% of face width)
               "eyes":     0..1}   bulge the eyes (1 = ~14% bigger at the centre)

Tone work (undereye, contour) runs in Lab on the developed image, limited to skin (iPhone skin matte when present).
Warps run last and only inside each face's neighbourhood, so background lines barely move.
Faces smaller than ~6% of the long side are left alone: retouching a face you can't see only adds risk.
"""
from __future__ import annotations

import os
import sys

import cv2
import numpy as np

import engine as E

# MediaPipe face-mesh indices
L_LOWER = [33, 7, 163, 144, 145, 153, 154, 155, 133]
R_LOWER = [263, 249, 390, 373, 374, 380, 381, 382, 362]
L_EYE = [33, 246, 161, 160, 159, 158, 157, 173, 133, 155, 154, 153, 145, 144, 163, 7]
R_EYE = [263, 466, 388, 387, 386, 385, 384, 398, 362, 382, 381, 380, 374, 373, 390, 249]
JAW = [234, 93, 132, 58, 172, 136, 150, 149, 176, 148, 152, 377, 400, 378, 379, 365, 397, 288, 361, 323, 454]
SLIM = [132, 58, 172, 136, 150, 379, 365, 397, 288, 361]  # cheek -> jaw angle; chin points stay (else a pointy 'V' chin)
NOSE_TIP, FOREHEAD, CHIN = 4, 151, 175
BRIDGE = [168, 6, 197, 195, 5]
CHEEKBONE = [50, 280]
NOSE_SIDES = [[193, 122, 196, 3, 51], [417, 351, 419, 248, 281]]

_LM = None


def _landmarker():
    global _LM
    if _LM is None:
        sys.modules.setdefault("tensorflow", None)  # mediapipe's optional TF import crashes under numpy 2
        os.environ.setdefault("GLOG_minloglevel", "2")
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision
        from organize import model_path
        opts = vision.FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path())),
            running_mode=vision.RunningMode.IMAGE, num_faces=8,
            min_face_detection_confidence=0.5, min_face_presence_confidence=0.5)
        _LM = (mp, vision.FaceLandmarker.create_from_options(opts))
    return _LM


def detect(img, max_side=1600, min_frac=0.06):
    """-> list of (478,2) float32 pixel coords in img space, faces at least min_frac of the long side wide."""
    h, w = img.shape[:2]
    s = min(1.0, max_side / max(h, w))
    small = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA) if s < 1 else img
    mp, lm = _landmarker()
    u8 = np.ascontiguousarray((np.clip(small, 0, 1) * 255).astype(np.uint8))
    res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=u8))
    faces = []
    for pts in res.face_landmarks or []:
        p = np.array([[q.x * w, q.y * h] for q in pts], np.float32)
        fw = p[JAW, 0].max() - p[JAW, 0].min()
        if fw >= min_frac * max(h, w):
            faces.append(p)
    return faces


def _poly_mask(h, w, poly, blur):
    m = np.zeros((h, w), np.float32)
    cv2.fillPoly(m, [np.round(poly).astype(np.int32)], 1.0)
    return E.gblur(m, blur) if blur > 0 else m


def _blob(h, w, c, r, roi):
    """Soft round falloff centred at c with radius r, computed inside roi=(x0,y0,x1,y1)."""
    x0, y0, x1, y1 = roi
    m = np.zeros((h, w), np.float32)
    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
    m[y0:y1, x0:x1] = np.exp(-((xx - c[0]) ** 2 + (yy - c[1]) ** 2) / (2 * r * r))
    return m


def _line(h, w, pts, thick, blur):
    m = np.zeros((h, w), np.float32)
    cv2.polylines(m, [np.round(pts).astype(np.int32)], False, 1.0, max(1, int(thick)), cv2.LINE_AA)
    return E.gblur(m, blur)


def _roi(p, h, w, pad):
    x0, y0 = np.maximum(p.min(0) - pad, 0).astype(int)
    x1, y1 = np.minimum(p.max(0) + pad, [w, h]).astype(int)
    return x0, y0, x1, y1


def retouch_tone(rgb, faces, cfg, skin=None):
    """Under-eye + dodge/burn contour in Lab. skin: optional 0..1 matte limiting where tone may change."""
    ue, ct = cfg.get("undereye", 0), cfg.get("contour", 0)
    if not faces or not (ue or ct):
        return rgb
    h, w = rgb.shape[:2]
    lab = cv2.cvtColor(np.clip(rgb, 0, 1).astype(np.float32), cv2.COLOR_RGB2LAB)
    L = lab[..., 0]
    for p in faces:
        fw = float(p[JAW, 0].max() - p[JAW, 0].min())
        roi = _roi(p, h, w, fw * 0.3)
        guard = skin if skin is not None and skin.max() > 0.05 else _poly_mask(h, w, p[JAW + [10]], fw * 0.02)
        if ue:
            for lower, eye in ((L_LOWER, L_EYE), (R_LOWER, R_EYE)):
                lid = p[lower]
                ew = float(np.linalg.norm(lid[0] - lid[-1]))
                top = lid + [0, ew * 0.10]                 # start just below the lash line
                bot = lid[::-1] + [0, ew * 0.55]          # down into the tear trough
                m = _poly_mask(h, w, np.vstack([top, bot]), ew * 0.12)
                m *= 1 - _poly_mask(h, w, p[eye], ew * 0.05)  # never touch the eye itself
                m *= guard
                # reference: the cheek just below the dark-circle band
                ref = _poly_mask(h, w, np.vstack([lid + [0, ew * 0.75], lid[::-1] + [0, ew * 1.05]]), 0) * guard
                if ref.sum() < 20 or m.max() < 0.05:
                    continue
                refL = float(np.median(L[ref > 0.5])) if (ref > 0.5).any() else float(np.median(L[ref > 0]))
                refab = [float(np.median(lab[..., c][ref > 0.3])) for c in (1, 2)]
                Lb = E.gblur(L, ew * 0.08)
                gap = np.clip(refL - Lb, 0, None)          # only lift what is darker than the cheek
                L += gap * m * ue * 0.85
                for c, v in zip((1, 2), refab):            # purple/brown cast -> cheek colour
                    lab[..., c] += (v - lab[..., c]) * m * ue * 0.6
        if ct:
            x0, y0, x1, y1 = roi
            d = np.zeros((h, w), np.float32)
            d += _blob(h, w, p[FOREHEAD], fw * 0.12, roi) * 0.8
            d += _line(h, w, p[BRIDGE], fw * 0.035, fw * 0.025) * 1.0
            for i in CHEEKBONE:
                d += _blob(h, w, p[i], fw * 0.09, roi) * 0.7
            d += _blob(h, w, p[CHIN], fw * 0.06, roi) * 0.6
            b = np.zeros((h, w), np.float32)
            jaw = p[JAW]
            inner = jaw + (p[NOSE_TIP] - jaw) * 0.10       # band just inside the jaw edge
            b += _line(h, w, inner, fw * 0.08, fw * 0.05) * 1.0
            for side in NOSE_SIDES:
                b += _line(h, w, p[side], fw * 0.03, fw * 0.02) * 0.7
            g = guard[y0:y1, x0:x1]
            L[y0:y1, x0:x1] += (np.clip(d[y0:y1, x0:x1], 0, 1) * 4.0 - np.clip(b[y0:y1, x0:x1], 0, 1) * 4.5) * ct * g
    lab[..., 0] = np.clip(L, 0, 100)
    return np.clip(cv2.cvtColor(lab, cv2.COLOR_LAB2RGB), 0, 1)


def retouch_warp(rgb, faces, cfg):
    """Slim jaw + bigger eyes with one inverse-mapped displacement field per face (local remap)."""
    sl, ey = cfg.get("slim", 0), cfg.get("eyes", 0)
    if not faces or not (sl or ey):
        return rgb
    h, w = rgb.shape[:2]
    out = rgb.copy()
    for p in faces:
        fw = float(p[JAW, 0].max() - p[JAW, 0].min())
        x0, y0, x1, y1 = _roi(p, h, w, fw * 0.45)
        yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        mx, my = xx.copy(), yy.copy()
        if sl:
            c = p[NOSE_TIP]
            R = fw * 0.13
            for i in SLIM:
                q = p[i]
                v = np.array([c[0] - q[0], 0.0], np.float32)       # horizontal only: no chin/collar drag
                v = v / (np.linalg.norm(v) + 1e-6) * fw * 0.06 * sl
                t = q + v                                          # where the contour should end up
                wgt = np.exp(-((xx - t[0]) ** 2 + (yy - t[1]) ** 2) / (2 * R * R))
                mx -= v[0] * wgt
                my -= v[1] * wgt
        if ey:
            for eye in (L_EYE, R_EYE):
                e = p[eye]
                c = e.mean(0)
                r = float(np.linalg.norm(e[0] - e[8])) * 1.1
                dx, dy = mx - c[0], my - c[1]
                d = np.sqrt(dx * dx + dy * dy) / r
                k = np.where(d < 1, 0.14 * ey * (1 - d * d) ** 2, 0).astype(np.float32)
                mx, my = c[0] + dx * (1 - k), c[1] + dy * (1 - k)
        out[y0:y1, x0:x1] = cv2.remap(rgb, mx, my, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
        rgb = out  # faces may overlap: later faces warp the already-warped image
    return np.clip(out, 0, 1)
