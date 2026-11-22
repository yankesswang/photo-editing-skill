"""Batch consistency: frames of the same scene get the same white balance and tone corrections.

Per-photo Auto corrects each frame on its own, so a set shot in one place drifts: one frame a bit
warmer, the next with more shadow lift. Next to each other in an album that reads as careless.
A photographer syncs settings across the set and only nudges exposure. This does the same:

  1. analyse every frame (autotone, cached), read its capture time
  2. split into scene groups: new group when the time gap > `gap` seconds, the light changes
     (night vs day), or the overall colour/brightness of the frame jumps (mean Lab distance)
  3. per group, the median of each synced correction replaces the per-frame value;
     exposure (and local masks like the backlit-subject lift) stay per frame, since the camera's
     own exposure varied between frames

Returns per-file overrides for build_recipe(corr_override=...).
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

import autotone
import engine as E

SYNCED = ("temp", "tint", "highlights", "shadows", "whites", "blacks", "contrast", "dehaze")


def _when(info, path):
    v = (info.get("exif") or {}).get("DateTimeOriginal")
    try:
        return datetime.strptime(str(v)[:19], "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:
        return os.path.getmtime(path)


def _lab_mean(img):
    small = cv2.resize(img, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32)
    return cv2.cvtColor(small, cv2.COLOR_RGB2LAB).reshape(-1, 3).mean(0)


def groups(paths, gap=600, scene_de=18.0):
    """-> list of groups, each a list of dicts {path, t, info, lab} in capture order."""
    items = []
    for p in paths:
        img = E.load_image(str(p), 900)
        info = autotone.analyze(str(p), img)
        items.append({"path": str(p), "t": _when(info, p), "info": info, "lab": _lab_mean(img)})
    items.sort(key=lambda it: it["t"])
    out = []
    for it in items:
        if out:
            prev = out[-1][-1]
            night = ("night/low-light" in it["info"]["tags"]) != ("night/low-light" in prev["info"]["tags"])
            # compare with the group's running mean, so a slow drift (sunset) still splits eventually
            ref = np.mean([x["lab"] for x in out[-1]], 0)
            if it["t"] - prev["t"] <= gap and not night and float(np.linalg.norm(it["lab"] - ref)) <= scene_de:
                out[-1].append(it)
                continue
        out.append([it])
    return out


def overrides(grps):
    """{path: {synced corrections}} with the group's median for every synced key."""
    res = {}
    for gi, g in enumerate(grps):
        corrs = [it["info"]["auto_correction"] for it in g]
        common = {}
        for k in SYNCED:
            vals = [c.get(k, 0) for c in corrs]
            common[k] = round(float(np.median(vals)))
        for it in g:
            res[it["path"]] = {**common, "_group": gi}
    return res


def report(grps):
    lines = []
    for gi, g in enumerate(grps):
        t0 = datetime.fromtimestamp(g[0]["t"]).strftime("%m-%d %H:%M")
        t1 = datetime.fromtimestamp(g[-1]["t"]).strftime("%H:%M")
        temps = [it["info"]["auto_correction"].get("temp", 0) for it in g]
        lines.append(f"group {gi}: {len(g):3d} photos  {t0}-{t1}  "
                     f"temp per-frame {min(temps)}..{max(temps)} -> {round(float(np.median(temps)))}  "
                     f"[{Path(g[0]['path']).name} .. {Path(g[-1]['path']).name}]")
    return "\n".join(lines)
