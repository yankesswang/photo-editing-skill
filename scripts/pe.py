#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "opencv-python-headless", "pillow"]
# ///
"""photo-edit CLI — Lightroom-style photo editing driven by recipes.

  pe.py styles                                  list looks
  pe.py analyze IMG...                          stats + scene tags + auto correction (JSON)
  pe.py board IMG [--styles a,b] -o board.jpg   one photo rendered in many looks (to choose)
  pe.py develop IMG... --style NAME [...]       render full-res edits (+ recipe sidecar)
  pe.py compare BEFORE AFTER -o cmp.jpg         labelled side-by-side
  pe.py review ORIG_DIR EDIT_DIR                before|after pair sheets for a whole output folder
  pe.py contact IMG... -o sheet.jpg             contact sheet
  pe.py match IMG --ref REF.jpg [-o recipe.json] derive a recipe that moves IMG toward REF's look
  pe.py xmp STYLE|RECIPE.json [-o out.xmp]      export as Lightroom preset
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
STYLES_DIR = HERE.parent / "styles"
DEFAULT_OUT = Path.home() / "Pictures" / "PhotoEdit"
IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic"}

import numpy as np  # noqa: E402

import autotone  # noqa: E402
import engine as E  # noqa: E402


# ----------------------------------------------------------------------------- helpers
def expand(paths):
    out = []
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            out += sorted(x for x in pp.iterdir() if x.suffix.lower() in IMG_EXT and not x.name.startswith("._"))
        elif any(c in p for c in "*?["):
            out += sorted(Path(x) for x in glob.glob(p) if not Path(x).name.startswith("._"))
        else:
            out.append(pp)
    return out


def load_style(name):
    if not name or name == "none":
        return {"name": "none"}
    p = Path(name)
    if not p.exists():
        p = STYLES_DIR / f"{name}.json"
    if not p.exists():
        sys.exit(f"style not found: {name}  (see: pe.py styles)")
    return json.loads(p.read_text())


def load_json_arg(s):
    if not s:
        return {}
    p = Path(s)
    return json.loads(p.read_text()) if p.exists() else json.loads(s)


def all_style_names():
    return sorted(p.stem for p in STYLES_DIR.glob("*.json"))


def label(img, text, size=None):
    from PIL import Image, ImageDraw, ImageFont
    im = Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    fs = size or max(14, im.width // 28)
    font = None
    for f in ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/Helvetica.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(f, fs)
            break
        except Exception:
            pass
    font = font or ImageFont.load_default()
    pad = fs // 3
    bbox = d.textbbox((0, 0), text, font=font)
    d.rectangle([0, 0, bbox[2] + 2 * pad, bbox[3] + 2 * pad], fill=(0, 0, 0))
    d.text((pad, pad), text, fill=(255, 255, 255), font=font)
    return np.asarray(im, dtype=np.float32) / 255


def grid(tiles, cols, gap=6):
    h = max(t.shape[0] for t in tiles); w = max(t.shape[1] for t in tiles)
    rows = (len(tiles) + cols - 1) // cols
    out = np.ones((rows * h + (rows - 1) * gap, cols * w + (cols - 1) * gap, 3), np.float32)
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        out[r * (h + gap): r * (h + gap) + t.shape[0], c * (w + gap): c * (w + gap) + t.shape[1]] = t
    return out


def build_recipe(path, style, strength=1.0, auto=True, adjust=None, img_small=None):
    info = autotone.analyze(path, img_small)
    corr = info["auto_correction"] if auto else {}
    if any(t.startswith("people") for t in info["tags"]):
        corr = {**corr, "skin_protect": 0.8}
    look = E.scale_recipe({k: v for k, v in style.items() if k not in E.META_KEYS}, strength)
    # style noise/sharpen only if stronger than the auto (per-ISO) values
    recipe = E.merge_recipes(corr, look)
    for k in ("noise", "sharpen"):
        if k in corr and k in look:
            recipe[k] = {kk: max(corr[k].get(kk, 0), look[k].get(kk, 0)) for kk in set(corr[k]) | set(look[k])}
    return E.merge_recipes(recipe, adjust or {})


def default_jobs():
    try:
        mem = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    except Exception:
        mem = 8
    return max(1, min((os.cpu_count() or 2) // 2, int(mem // 5)))


# ----------------------------------------------------------------------------- commands
def cmd_styles(a):
    for n in all_style_names():
        s = load_style(n)
        print(f"{n:24s} {s.get('description','')}")
        print(f"{'':24s} use for: {', '.join(s.get('use_for', []))}" +
              (f" | avoid: {', '.join(s['avoid_for'])}" if s.get("avoid_for") else ""))


def cmd_analyze(a):
    res = [autotone.analyze(str(p)) for p in expand(a.images)]
    txt = json.dumps(res if len(res) > 1 else res[0], ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(txt)
    print(txt)


def _worker_init(parent_pid):
    """Pool workers exit on their own if the parent dies (e.g. killed batch) instead of
    lingering as orphans that hog RAM/CPU."""
    import threading

    def watch():
        while True:
            time.sleep(2)
            if os.getppid() != parent_pid:
                os._exit(1)
    threading.Thread(target=watch, daemon=True).start()


def _develop_one(job):
    path, style, strength, auto, adjust, outdir, preview, suffix, quality = job
    t = time.time()
    small = E.load_image(path, 900)
    recipe = build_recipe(path, style, strength, auto, adjust, small)
    img = E.load_image(path, preview) if preview else E.load_image(path)
    out = E.develop(img, recipe)
    name = Path(path).stem + (f"_{suffix}" if suffix else "") + ".jpg"
    dst = Path(outdir) / name
    E.save_image(out, dst, path, quality)
    side = {"source": str(path), "style": style.get("name"), "strength": strength, "auto": auto,
            "adjust": adjust, "recipe": recipe}
    dst.with_suffix(".recipe.json").write_text(json.dumps(side, ensure_ascii=False, indent=1))
    return f"{dst}  ({time.time() - t:.1f}s)"


def cmd_develop(a):
    files = expand(a.images)
    if not files:
        sys.exit("no images")
    style = load_style(a.style)
    adjust = load_json_arg(a.adjust)
    outdir = Path(a.out or (DEFAULT_OUT / (files[0].parent.name + "_" + style.get("name", "edit"))))
    outdir.mkdir(parents=True, exist_ok=True)
    per_file = load_json_arg(a.per_file) if a.per_file else {}
    suffix = a.suffix if a.suffix is not None else style.get("name", "edit")
    jobs = []
    for f in files:
        adj = E.merge_recipes(adjust, per_file.get(f.name) or per_file.get(f.stem) or {})
        jobs.append((str(f), style, a.strength, not a.no_auto, adj, str(outdir), a.preview, suffix, a.quality))
    workers = max(1, min(a.jobs, len(jobs)))
    if workers == 1:
        for j in jobs:
            print(_develop_one(j), flush=True)
    else:
        with ProcessPoolExecutor(workers, initializer=_worker_init, initargs=(os.getpid(),)) as ex:
            for r in ex.map(_develop_one, jobs):
                print(r, flush=True)
    print(f"done -> {outdir}")


def cmd_board(a):
    path = expand([a.image])[0]
    names = a.styles.split(",") if a.styles else all_style_names()
    small = E.load_image(path, 900)
    base = E.load_image(path, a.size)
    tiles = [label(base, "original")]
    if not a.no_auto:
        corr = autotone.analyze(str(path), small)["auto_correction"]
        tiles.append(label(E.develop(base, corr), "auto-correct only"))
    for n in names:
        st = load_style(n)
        r = build_recipe(str(path), st, a.strength, not a.no_auto, None, small)
        tiles.append(label(E.develop(base, r), n))
    out = grid(tiles, a.cols)
    dst = a.out or str(DEFAULT_OUT / f"{path.stem}_board.jpg")
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    E.save_image(out, dst, quality=88)
    print(dst)


def cmd_compare(a):
    size = a.size
    ims = []
    for p, lab in zip(a.images, a.labels.split(",") if a.labels else [None] * len(a.images)):
        im = E.load_image(p, size)
        ims.append(label(im, lab or Path(p).stem))
    h = min(i.shape[0] for i in ims)
    import cv2
    ims = [cv2.resize(i, (int(i.shape[1] * h / i.shape[0]), h)) if i.shape[0] != h else i for i in ims]
    out = grid(ims, a.cols or len(ims))
    dst = a.out or str(DEFAULT_OUT / "compare.jpg")
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    E.save_image(out, dst, quality=88)
    print(dst)


def cmd_contact(a):
    files = expand(a.images)
    tiles = [label(E.load_image(str(f), a.size), f.name, size=max(12, a.size // 22)) for f in files]
    import cv2
    th = a.size * 2 // 3
    tiles = [cv2.resize(t, (int(t.shape[1] * th / t.shape[0]), th)) for t in tiles]
    tw = max(t.shape[1] for t in tiles)
    tiles = [np.pad(t, ((0, 0), (0, tw - t.shape[1]), (0, 0)), constant_values=1) for t in tiles]
    out = grid(tiles, a.cols)
    dst = a.out or str(DEFAULT_OUT / "contact.jpg")
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    E.save_image(out, dst, quality=85)
    print(dst)


def cmd_review(a):
    """before|after pairs for every edited file in a folder (matched to originals by stem)."""
    import cv2
    src = {f.stem: f for f in expand([a.originals])}
    edits = sorted(Path(a.edited).glob("*.jpg"))
    tiles = []
    for e in edits:
        stem = e.stem.split("_")[0]
        if stem not in src:
            continue
        b = E.load_image(str(src[stem]), a.size)
        af = E.load_image(str(e), a.size)
        if b.shape != af.shape:
            af = cv2.resize(af, (b.shape[1], b.shape[0]))
        th = a.size * 2 // 3
        pair = np.concatenate([label(b, stem, size=max(12, a.size // 20)), np.ones((b.shape[0], 4, 3), np.float32),
                               label(af, "after", size=max(12, a.size // 20))], 1)
        tiles.append(cv2.resize(pair, (int(pair.shape[1] * th / pair.shape[0]), th)))
    if not tiles:
        sys.exit("no matching edited files")
    tw = max(t.shape[1] for t in tiles)
    tiles = [np.pad(t, ((0, 0), (0, tw - t.shape[1]), (0, 0)), constant_values=1) for t in tiles]
    per = a.per_sheet
    outs = []
    for i in range(0, len(tiles), per):
        dst = a.out or str(Path(a.edited) / "_review.jpg")
        if len(tiles) > per:
            dst = dst.replace(".jpg", f"_{i // per + 1}.jpg")
        E.save_image(grid(tiles[i:i + per], a.cols, gap=10), dst, quality=85)
        outs.append(dst)
    print("\n".join(outs))


def cmd_match(a):
    import match
    rec = match.match_recipe(str(a.image), [str(p) for p in expand(a.ref)], a.strength)
    txt = json.dumps(rec, ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(txt)
    print(txt)


def cmd_xmp(a):
    import xmp
    src = load_style(a.recipe)
    if "recipe" in src and isinstance(src["recipe"], dict):  # a .recipe.json sidecar
        src = {"name": Path(a.recipe).stem.replace(".recipe", ""), **src["recipe"]}
    name = a.name or src.get("name", "photo-edit")
    dst = a.out or str(DEFAULT_OUT / "lightroom-presets" / f"{name}.xmp")
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text(xmp.recipe_to_xmp(src, name, a.group))
    print(dst)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    sp.add_parser("styles").set_defaults(fn=cmd_styles)

    p = sp.add_parser("analyze"); p.add_argument("images", nargs="+"); p.add_argument("-o", "--out")
    p.set_defaults(fn=cmd_analyze)

    p = sp.add_parser("develop")
    p.add_argument("images", nargs="+", help="files, dirs or globs")
    p.add_argument("--style", default="clean-natural", help="style name, path to .json, or 'none'")
    p.add_argument("--strength", type=float, default=1.0, help="style amount 0..1.5")
    p.add_argument("--adjust", help="JSON string/file of extra slider tweaks applied to every image")
    p.add_argument("--per-file", help="JSON {filename: {tweaks}} for per-photo fixes")
    p.add_argument("--no-auto", action="store_true", help="skip per-photo auto correction")
    p.add_argument("--preview", type=int, help="render at this long side instead of full res")
    p.add_argument("-o", "--out", help=f"output dir (default {DEFAULT_OUT}/<folder>_<style>)")
    p.add_argument("--suffix", help="filename suffix (default style name; '' for none)")
    p.add_argument("--quality", type=int, default=92)
    p.add_argument("-j", "--jobs", type=int, default=default_jobs(),
                   help="parallel workers (default from RAM: ~1 per 5GB, 24MP needs ~3GB)")
    p.set_defaults(fn=cmd_develop)

    p = sp.add_parser("board"); p.add_argument("image")
    p.add_argument("--styles"); p.add_argument("--strength", type=float, default=1.0)
    p.add_argument("--no-auto", action="store_true"); p.add_argument("--size", type=int, default=900)
    p.add_argument("--cols", type=int, default=4); p.add_argument("-o", "--out")
    p.set_defaults(fn=cmd_board)

    p = sp.add_parser("compare"); p.add_argument("images", nargs="+"); p.add_argument("--labels")
    p.add_argument("--size", type=int, default=1200); p.add_argument("--cols", type=int)
    p.add_argument("-o", "--out"); p.set_defaults(fn=cmd_compare)

    p = sp.add_parser("contact"); p.add_argument("images", nargs="+")
    p.add_argument("--size", type=int, default=360); p.add_argument("--cols", type=int, default=6)
    p.add_argument("-o", "--out"); p.set_defaults(fn=cmd_contact)

    p = sp.add_parser("review", help="before|after pairs sheet for an output folder")
    p.add_argument("originals"); p.add_argument("edited")
    p.add_argument("--size", type=int, default=520); p.add_argument("--cols", type=int, default=2)
    p.add_argument("--per-sheet", type=int, default=8); p.add_argument("-o", "--out")
    p.set_defaults(fn=cmd_review)

    p = sp.add_parser("match"); p.add_argument("image"); p.add_argument("--ref", nargs="+", required=True)
    p.add_argument("--strength", type=float, default=0.8); p.add_argument("-o", "--out")
    p.set_defaults(fn=cmd_match)

    p = sp.add_parser("xmp"); p.add_argument("recipe", help="style name, style .json or .recipe.json")
    p.add_argument("--name"); p.add_argument("--group", default="Photo Edit Skill"); p.add_argument("-o", "--out")
    p.set_defaults(fn=cmd_xmp)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
