#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["numpy", "opencv-python-headless", "pillow"]
# ///
"""photo-edit CLI — Lightroom-style photo editing driven by recipes.

  pe.py styles                                  list looks
  pe.py analyze IMG...                          stats + scene tags + auto correction (JSON)
  pe.py board IMG [--styles a,b] -o board.jpg   one photo rendered in many looks (to choose)
  pe.py develop IMG... --style NAME [...]       render full-res edits (+ recipe sidecar); --consistent syncs a set
  pe.py compare BEFORE AFTER -o cmp.jpg         labelled side-by-side
  pe.py review ORIG_DIR EDIT_DIR                before|after pair sheets for a whole output folder
  pe.py contact IMG... -o sheet.jpg             contact sheet
  pe.py cull DIR                                bursts -> groups, veto blur/exposure, picks.txt + sheets
  pe.py organize DIR [--apply link]             album: events x categories, delete suggestions + HTML report
  pe.py masks IMG --mask face --mask sky        red overlay of local-adjustment masks
  pe.py mattes IPHONE.HEIC... [-o DIR]          extract iPhone skin/hair/teeth/person/sky mattes -> <stem>.mattes/
  pe.py people IMG -o people.jpg                 numbered people, subject vs bystander guess
  pe.py erase IMG --remove bystanders|2,5        remove people (inpainting) into new files
  pe.py fill IMG --source OTHER.JPG [--keep 0]   remove people with REAL pixels from another frame of the scene
  pe.py stack F1 F2 F3... -o merged.jpg          align + merge a handheld burst: multi-frame noise reduction
  pe.py match IMG --ref REF.jpg [-o recipe.json] derive a recipe that moves IMG toward REF's look
  pe.py xmp STYLE|RECIPE.json [-o out.xmp]      export as Lightroom preset
  pe.py photos DIR... [--album NAME]            import finished JPGs into Apple Photos (-> iCloud)
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
IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic", ".heif"} | {
    ".arw", ".dng", ".cr2", ".cr3", ".nef", ".nrw", ".raf", ".orf", ".rw2", ".pef", ".srw"}

import numpy as np  # noqa: E402

import autotone  # noqa: E402
import engine as E  # noqa: E402


# ----------------------------------------------------------------------------- helpers
def expand(paths):
    out = []
    for p in paths:
        if p.startswith("@"):  # @list.txt: one path per line (e.g. cull picks.txt)
            out += expand([ln.strip() for ln in Path(p[1:]).read_text().splitlines()
                           if ln.strip() and not ln.startswith("#")])
            continue
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
    for f in ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
              "/System/Library/Fonts/Helvetica.ttc",
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


def build_recipe(path, style, strength=1.0, auto=True, adjust=None, img_small=None, corr_override=None):
    info = autotone.analyze(path, img_small)
    corr = info["auto_correction"] if auto else {}
    if auto and corr_override:  # batch consistency: the scene group's shared WB / tone
        corr = {**corr, **{k: v for k, v in corr_override.items() if not k.startswith("_")}}
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
    path, style, strength, auto, adjust, outdir, preview, suffix, quality, sync = job
    t = time.time()
    small = E.load_image(path, 900)
    recipe = build_recipe(path, style, strength, auto, adjust, small, sync)
    side_mattes = Path(path).with_suffix(".mattes")  # iPhone semantic mattes (pe.py mattes) for 'matte' masks
    if side_mattes.is_dir():
        recipe["_mattes"] = str(side_mattes)
    img = E.load_image(path, preview) if preview else E.load_image(path)
    out = E.develop(img, recipe)
    name = Path(path).stem + (f"_{suffix}" if suffix else "") + ".jpg"
    dst = Path(outdir) / name
    E.save_image(out, dst, path, quality)
    side = {"source": str(path), "style": style.get("name"), "strength": strength, "auto": auto,
            "adjust": adjust, "recipe": recipe, **({"group": sync["_group"]} if sync else {})}
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
    sync = {}
    if a.consistent and not a.no_auto and len(files) > 1:
        import consistency as C
        grps = C.groups(files, gap=a.group_gap)
        print(C.report(grps), flush=True)
        sync = C.overrides(grps)
    jobs = []
    for f in files:
        adj = E.merge_recipes(adjust, per_file.get(f.name) or per_file.get(f.stem) or {})
        jobs.append((str(f), style, a.strength, not a.no_auto, adj, str(outdir), a.preview, suffix, a.quality,
                     sync.get(str(f))))
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
    labels = (a.labels.split(a.sep) if a.labels else [])
    labels += [None] * (len(a.images) - len(labels))  # never drop images when labels run short
    for p, lab in zip(a.images, labels):
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
        # edited files are "<orig stem>_<style>"; orig stems may contain "_" too → longest prefix match
        cands = [s for s in src if e.stem == s or e.stem.startswith(s + "_")]
        if not cands:
            continue
        stem = max(cands, key=len)
        b = E.load_image(str(src[stem]), a.size)
        af = E.load_image(str(e), a.size)
        if b.shape != af.shape:  # rotated/cropped edit: match height, keep its own aspect
            af = cv2.resize(af, (max(1, int(af.shape[1] * b.shape[0] / af.shape[0])), b.shape[0]))
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


def apple_mattes_bin():
    """Compile (once) the ImageIO matte extractor; needs macOS + swiftc (Xcode command line tools)."""
    import subprocess
    src = HERE / "apple_mattes.swift"
    out = Path.home() / ".cache/photo-edit/apple_mattes"
    if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["swiftc", "-O", str(src), "-o", str(out)], check=True, capture_output=True)
    return out


def cmd_mattes(a):
    """iPhone HEIC/JPG -> <stem>.mattes/{portrait,skin,hair,teeth,glasses,sky}.png + meta.json (orientation)."""
    import subprocess
    exe = apple_mattes_bin()
    for f in expand(a.images):
        dst = (Path(a.out) if a.out else f.parent) / (f.stem + ".mattes")
        r = subprocess.run([str(exe), str(f), str(dst)], capture_output=True, text=True)
        print(f"{f.name}: {r.stdout.strip() or 'no mattes'}")


def cmd_masks(a):
    """Show every mask of a recipe as a red overlay (verify coverage before trusting a local edit)."""
    import masks as M
    path = expand([a.image])[0]
    img = E.load_image(str(path), a.size)
    entries = []
    if a.auto:
        entries += autotone.analyze(str(path)).get("auto_correction", {}).get("masks", [])
    for src in (a.recipe, a.adjust):
        if src:
            r = load_json_arg(src)
            r = r.get("recipe", r)
            r = r.get(path.name) or r.get(path.stem) or r  # per-file json
            entries += r.get("masks", []) if isinstance(r, dict) else []
    for spec in a.mask or []:
        entries.append({"name": spec, "mask": load_json_arg(spec) if spec.startswith("{") else {"type": spec}})
    if not entries:
        sys.exit("no masks: pass --auto, --recipe, --adjust or --mask face|subject|sky|'{json}'")
    side_mattes = path.with_suffix(".mattes")
    M.set_mattes(str(side_mattes) if side_mattes.is_dir() else None, img.shape[:2], {})
    faces = M.detect_faces(img)
    tiles = [label(img, f"{path.stem}  faces={len(faces)}")]
    for ov, name in M.overlay(img, entries, faces):
        tiles.append(label(ov, str(name)))
    dst = a.out or str(DEFAULT_OUT / f"{path.stem}_masks.jpg")
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    E.save_image(grid(tiles, min(a.cols, len(tiles))), dst, quality=85)
    print(dst)


def _thumb_tile(r, size, picked):
    from PIL import Image
    im = Image.open(r["thumb"]).convert("RGB")
    bh = size * 2 // 3  # fixed 3:2 cell so portrait and landscape frames tile together
    im.thumbnail((size, bh))
    t = np.asarray(im, dtype=np.float32) / 255
    pad = np.ones((bh, size, 3), np.float32) * 0.12
    y0, x0 = (bh - t.shape[0]) // 2, (size - t.shape[1]) // 2
    pad[y0:y0 + t.shape[0], x0:x0 + t.shape[1]] = t
    if r.get("face_thumb") and Path(r["face_thumb"]).exists():
        # face inset (bottom-right) so expressions can be judged without zooming
        f = Image.open(r["face_thumb"]).convert("RGB")
        fs = bh * 11 // 20
        f = np.asarray(f.resize((fs, fs)), dtype=np.float32) / 255
        pad[bh - fs - 2:bh - 2, size - fs - 2:size - 2] = f
    col = (0.1, 0.85, 0.2) if picked else ((0.9, 0.15, 0.15) if r["veto"] else None)
    if col:
        b = max(3, size // 60)
        pad[:b], pad[-b:], pad[:, :b], pad[:, -b:] = col, col, col, col
    txt = f"{Path(r['file']).stem[-5:]} #{r['rank']} t{r['tech']:.2f}" + (f" {','.join(r['veto'])}" if r["veto"] else "")
    return label(pad, txt, size=max(11, size // 20))


def cmd_cull(a):
    import cull as C
    files = expand(a.images)
    if not files:
        sys.exit("no images")
    out = Path(a.out or (DEFAULT_OUT / (files[0].parent.name + "_cull")))
    t0 = time.time()
    rows = C.scan(files, out, jobs=a.jobs)
    groups = C.assess(C.group(rows, a.gap, a.strict))
    picks, rejected = C.summarize(groups, a.keep)
    errors = [r for r in rows if "error" in r]
    multi = [g for g in groups if len(g) > 1]
    (out / "cull.json").write_text(json.dumps(
        {"source": str(files[0].parent), "frames": len(rows), "groups": len(groups), "burst_groups": len(multi),
         "vetoed": rejected, "picks": len(picks), "errors": errors,
         "params": {"gap_s": a.gap, "strict": a.strict, "keep_per_group": a.keep},
         "group_list": [[{k: r[k] for k in ("file", "rank", "tech", "sharp", "lum", "clip_hi", "faces", "veto")}
                         for r in sorted(g, key=lambda r: r["rank"])] for g in groups]},
        ensure_ascii=False, indent=1))
    (out / "picks.txt").write_text("\n".join(picks) + "\n")
    pickset = set(picks)
    # burst sheets: one row per multi-frame group (green = technical pick, red = vetoed)
    sz = a.thumb
    rows_img, sheets = [], []
    for gi, g in enumerate(multi):
        g = sorted(g, key=lambda r: r["file"])[: a.max_per_row]
        tiles = [_thumb_tile(r, sz, r["file"] in pickset) for r in g]
        row = np.concatenate([np.pad(t, ((0, 0), (0, 4), (0, 0)), constant_values=1) for t in tiles], 1)
        rows_img.append(row)
    for i in range(0, len(rows_img), a.rows_per_sheet):
        chunk = rows_img[i:i + a.rows_per_sheet]
        wmax = max(r.shape[1] for r in chunk)
        chunk = [np.pad(r, ((0, 6), (0, wmax - r.shape[1]), (0, 0)), constant_values=1) for r in chunk]
        dst = out / f"bursts_{i // a.rows_per_sheet + 1:02d}.jpg"
        E.save_image(np.concatenate(chunk, 0), dst, quality=82)
        sheets.append(str(dst))
    # picks sheets: every kept frame, numbered, for the visual "moment" pass
    prow = [r for g in groups for r in g if r["file"] in pickset]
    per = a.cols * a.rows_per_sheet
    for i in range(0, len(prow), per):
        tiles = [_thumb_tile(r, sz, False) for r in prow[i:i + per]]
        dst = out / f"picks_{i // per + 1:02d}.jpg"
        E.save_image(grid(tiles, a.cols, gap=6), dst, quality=82)
        sheets.append(str(dst))
    print(f"{len(rows)} frames -> {len(groups)} groups ({len(multi)} bursts), vetoed {rejected}, "
          f"technical picks {len(picks)}  [{time.time() - t0:.0f}s]")
    print(f"out: {out}/  cull.json picks.txt  sheets: {len(sheets)}")
    for s_ in sheets:
        print("  " + s_)


def _org_tile(it, size, col, text, face=None):
    """Thumbnail cell (3:2) + optional face inset + coloured border + caption."""
    from PIL import Image
    im = Image.open(it["thumb"]).convert("RGB")
    bh = size * 2 // 3
    im.thumbnail((size, bh))
    t = np.asarray(im, dtype=np.float32) / 255
    pad = np.ones((bh, size, 3), np.float32) * 0.12
    y0, x0 = (bh - t.shape[0]) // 2, (size - t.shape[1]) // 2
    pad[y0:y0 + t.shape[0], x0:x0 + t.shape[1]] = t
    if face and Path(face).exists():
        fs = bh * 11 // 20
        f = np.asarray(Image.open(face).convert("RGB").resize((fs, fs)), dtype=np.float32) / 255
        pad[bh - fs - 2:bh - 2, size - fs - 2:size - 2] = f
    if col:
        b = max(3, size // 60)
        pad[:b], pad[-b:], pad[:, :b], pad[:, -b:] = col, col, col, col
    return label(pad, text, size=max(11, size // 20))


def cmd_organize(a):
    """Album organisation: events x categories + keep / review / delete suggestions. Never touches originals."""
    import shutil
    import cull as C
    import organize as O
    files = expand(a.images)
    if not files:
        sys.exit("no images")
    out = Path(a.out or (DEFAULT_OUT / (files[0].parent.name + "_organize")))
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    print(f"[1/4] scan {len(files)} frames", flush=True)
    rows = C.scan(files, out, jobs=a.jobs)
    print(f"[2/4] faces + eyes  [{time.time() - t0:.0f}s]", flush=True)
    faces = O.scan_faces(files, out, jobs=min(a.jobs, 3))
    print(f"[3/4] categories  [{time.time() - t0:.0f}s]", flush=True)
    cats = O.load_categories(a.categories)
    probs = O.classify([r for r in rows if "error" not in r], out, cats)
    print(f"[4/4] bursts + verdicts  [{time.time() - t0:.0f}s]", flush=True)
    groups = C.assess(C.group(rows, a.gap, a.strict))
    th = json.loads(a.thresholds) if a.thresholds else None
    items = O.rebalance_duplicates(O.judge(rows, faces, probs, groups, th))
    ev = O.events(rows, a.event_gap)
    zh = {k: v for k, v, _ in cats}
    zh.update({"other": "其他"})
    for it in items:
        it["event"] = ev.get(it["file"], "no-date")
        it["category_zh"] = zh.get(it["category"], it["category"])
    items.sort(key=lambda it: (it["time"] or 0, it["file"]))

    by_v = {v: [it for it in items if it["verdict"] == v] for v in ("keep", "review", "delete")}
    for v, lst in by_v.items():
        (out / f"{v}.txt").write_text("".join(it["file"] + "\n" for it in lst))
    reasons = {}
    for it in items:
        for r_ in it["reasons"] + it["soft"]:
            reasons.setdefault(r_, []).append(it)
    summary = {"source": str(files[0].parent), "frames": len(items),
               "keep": len(by_v["keep"]), "review": len(by_v["review"]), "delete": len(by_v["delete"]),
               "reasons": {k: len(v) for k, v in sorted(reasons.items(), key=lambda kv: -len(kv[1]))},
               "events": {}, "categories": {}}
    for it in items:
        e = summary["events"].setdefault(it["event"], {})
        e[it["category_zh"]] = e.get(it["category_zh"], 0) + 1
        summary["categories"][it["category_zh"]] = summary["categories"].get(it["category_zh"], 0) + 1
    (out / "organize.json").write_text(json.dumps({"summary": summary, "items": items}, ensure_ascii=False, indent=1))

    # ---- contact sheets (for Claude to verify): one series per delete/review reason, one per category
    sz, per = a.thumb, a.cols * a.rows_per_sheet
    for old in list(out.glob("del_*.jpg")) + list(out.glob("cat_*.jpg")):
        old.unlink()
    sheets = []

    def face_for(it, reason):
        ppl = it.get("people") or []
        if reason in ("eyes closed", "someone's eyes closed", "eyes half closed") and ppl:
            ppl = sorted(ppl, key=lambda f: -min(f["blink_l"], f["blink_r"]))
        return ppl[0]["thumb"] if ppl else None

    for reason, lst in reasons.items():
        tiles = []
        for it in lst:
            hard = reason in it["reasons"]
            info = ""
            ppl = it.get("people") or []
            if ppl and reason in ("eyes closed", "someone's eyes closed", "eyes half closed"):
                f = max(ppl, key=lambda f: min(f["blink_l"], f["blink_r"]))
                info = f" eyes{min(f['blink_l'], f['blink_r']):.2f}"
            elif ppl and reason in ("missed focus", "blurry face"):
                info = f" face{ppl[0]['sharp']:.2f}/bg{it['frame_sharp']:.2f}"
            elif reason == "duplicate":
                info = f" keep{Path(it.get('burst_keeper', '')).stem[-4:]}"
            tiles.append(_org_tile(it, sz, (0.9, 0.15, 0.15) if hard else (0.95, 0.65, 0.1),
                                   f"{Path(it['file']).stem[-5:]}{info}", face_for(it, reason)))
        slug = reason.replace("'", "").replace(" ", "-")
        for i in range(0, len(tiles), per):
            dst = out / f"del_{slug}_{i // per + 1:02d}.jpg"
            E.save_image(grid(tiles[i:i + per], a.cols), dst, quality=80)
            sheets.append(str(dst))
    if a.category_sheets:
        for key in [k for k, _, _ in cats] + ["other"]:
            lst = [it for it in items if it["category"] == key and it["verdict"] != "delete"]
            tiles = [_org_tile(it, sz, None, f"{Path(it['file']).stem[-5:]} {it['cat_prob']:.2f}") for it in lst]
            for i in range(0, len(tiles), per):
                dst = out / f"cat_{key}_{i // per + 1:02d}.jpg"
                E.save_image(grid(tiles[i:i + per], a.cols), dst, quality=80)
                sheets.append(str(dst))
    O.write_html(items, summary, out / "index.html", reason_zh=O.REASON_ZH)

    # ---- optional folder tree (links/copies; originals stay where they are)
    if a.apply != "none":
        root = out / "album"
        if root.exists():
            shutil.rmtree(root)
        for it in items:
            src = Path(it["file"])
            if it["verdict"] == "delete":
                d = root / "_建議刪除" / O.REASON_ZH.get(it["reasons"][0], it["reasons"][0])
            elif it["verdict"] == "review" and a.review_folder:
                d = root / "_待確認" / O.REASON_ZH.get(it["soft"][0], it["soft"][0])
            else:
                d = root / it["event"] / it["category_zh"]
            d.mkdir(parents=True, exist_ok=True)
            if a.apply == "link":
                (d / src.name).symlink_to(src)
            else:
                shutil.copy2(src, d / src.name)

    print(f"{len(items)} frames  keep {len(by_v['keep'])}  review {len(by_v['review'])}  "
          f"delete {len(by_v['delete'])}  [{time.time() - t0:.0f}s]")
    for k, v in summary["reasons"].items():
        print(f"  {O.REASON_ZH.get(k, k):<16} {v}")
    print("categories: " + "  ".join(f"{k} {v}" for k, v in sorted(summary["categories"].items(), key=lambda kv: -kv[1])))
    print(f"events: {len(summary['events'])}")
    print(f"out: {out}/  organize.json index.html keep/review/delete.txt" + ("  album/" if a.apply != "none" else ""))
    for s_ in sheets:
        print("  " + s_)


def cmd_people(a):
    """List detected people with ids and roles, plus a numbered overlay (green=subject, red=bystander)."""
    import erase as R
    rows = []
    for path in expand(a.images):
        img = E.load_image(str(path), a.size)
        ps = R.people(img)
        print(f"{path.name}: {len(ps)} people")
        for p in ps:
            x, y, w, h = p["box"]
            print(f"  #{p['id']:<2} {p['role']:<9} area {p['area'] * 100:5.2f}%  conf {p['conf']:.2f}  "
                  f"box x{x:.2f} y{y:.2f} w{w:.2f} h{h:.2f}")
        rows.append(label(R.overlay(img, ps), f"{path.stem}  green=subject red=bystander"))
    if a.out:
        E.save_image(grid(rows, min(a.cols, len(rows))), a.out, quality=85)
        print(a.out)


def cmd_erase(a):
    """Write copies with people removed (originals untouched); EXIF kept. Then develop those copies."""
    import erase as R
    files = expand(a.images)
    outdir = Path(a.out or (DEFAULT_OUT / (files[0].parent.name + "_erased")))
    outdir.mkdir(parents=True, exist_ok=True)
    if a.keep:
        spec = {"keep": [int(x) for x in a.keep.split(",")]}
    elif a.remove in ("bystanders", "all"):
        spec = a.remove
    else:
        spec = [int(x) for x in a.remove.split(",")]
    for f in files:
        t = time.time()
        img = E.load_image(str(f), a.preview) if a.preview else E.load_image(str(f))
        extra = json.loads(a.extra) if a.extra else None
        out, ps, removed = R.erase(img, spec, a.grow, extra=extra)
        dst = outdir / f.name
        E.save_image(out, dst, str(f), 95)
        print(f"{dst}  removed {removed} of {len(ps)} people  ({time.time() - t:.1f}s)")


def cmd_fill(a):
    """Remove people using real background from other frames of the same scene; LaMa only for leftovers."""
    import fill as F
    if a.keep:
        spec = {"keep": [int(x) for x in a.keep.split(",")]}
    elif a.remove in ("bystanders", "all"):
        spec = a.remove
    else:
        spec = [int(x) for x in a.remove.split(",")]
    t = time.time()
    tgt = E.load_image(str(a.image))
    srcs = [E.load_image(str(s)) for s in expand(a.source)]
    out, rep = F.fill(tgt, srcs, spec, a.grow)
    dst = Path(a.out) if a.out else DEFAULT_OUT / (Path(a.image).parent.name + "_erased") / Path(a.image).name
    if dst.suffix == "" or dst.is_dir():
        dst = dst / Path(a.image).name
    dst.parent.mkdir(parents=True, exist_ok=True)
    E.save_image(out, dst, str(a.image), 95)
    for st in rep["sources"]:
        print(f"  source {st['source']}: fit {st.get('fit')}  inliers {st.get('inliers', 0)}  "
              f"filled {st.get('filled_px', 0)} px  ring_diff {st.get('ring_diff', '-')}")
    print(f"{dst}  removed {rep['removed']} of {rep['people']} people, "
          f"{rep.get('real_pct', 100)}% real pixels  ({time.time() - t:.1f}s)")
    for w in rep.get("warnings", []):
        print(f"  WARNING: {w}  -> check 100% crop; prefer erase or keep the person")


def cmd_stack(a):
    """Merge a burst into one low-noise frame (then develop it like any photo)."""
    import stack as K
    files = expand(a.images)
    if len(files) < 2:
        sys.exit("stack needs at least 2 frames")
    t = time.time()
    frames = [E.load_image(str(f), a.preview) if a.preview else E.load_image(str(f)) for f in files]
    ref = None
    if a.ref:
        names = [f.name for f in files]
        ref = names.index(Path(a.ref).name) if Path(a.ref).name in names else None
    out, rep = K.merge(frames, ref, a.ghost)
    dst = Path(a.out) if a.out else DEFAULT_OUT / (files[0].parent.name + "_stacked") / (files[rep["ref"]].stem + "_stack.jpg")
    if dst.suffix == "" or dst.is_dir():
        dst = dst / (files[rep["ref"]].stem + "_stack.jpg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    E.save_image(out, dst, str(files[rep["ref"]]), 95)
    print(f"reference {files[rep['ref']].name}  noise sigma {rep['noise_sigma']} -> {rep['noise_after']}")
    for st in rep["frames"]:
        name = files[st["frame"]].name
        if st.get("used"):
            print(f"  {name}: aligned ({st['inliers']} inliers, ECC {st.get('ecc')}), static {st['static_pct']}%")
        else:
            print(f"  {name}: SKIPPED ({st.get('reason')})")
    print(f"{dst}  merged {rep['used']} of {len(files)} frames  ({time.time() - t:.1f}s)")


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


def cmd_photos(a):
    import photos as P
    res = P.run(a.paths, album=a.album, batch=a.batch, recursive=not a.no_recursive, dry_run=a.dry_run)
    if a.dry_run:
        return
    bad = False
    for r in res:
        line = (f"{r['album']}: {r['album_count']} in album "
                f"(+{r['imported']} new, {r['skipped_existing']} already there)")
        if not r["ok"]:
            bad = True
            line += f"  MISSING {len(r['missing_after'])}: " + ", ".join(r["missing_after"][:10])
        print(line)
    if bad:
        sys.exit(1)


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
    p.add_argument("--consistent", action="store_true",
                   help="sync WB / tone corrections within scene groups (time gap + scene change); exposure stays per photo")
    p.add_argument("--group-gap", type=int, default=600, help="seconds between frames that start a new scene group")
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
    p.add_argument("--sep", default=",", help="label separator (use '|' when labels contain commas)")
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

    p = sp.add_parser("cull", help="group bursts, veto technical failures, rank, contact sheets")
    p.add_argument("images", nargs="+"); p.add_argument("-o", "--out")
    p.add_argument("--gap", type=float, default=30.0, help="max seconds between frames of one group")
    p.add_argument("--strict", type=float, default=0.0, help="0..0.3: raise similarity bar (smaller groups)")
    p.add_argument("--keep", type=int, default=1, help="technical picks per group")
    p.add_argument("--thumb", type=int, default=300); p.add_argument("--cols", type=int, default=6)
    p.add_argument("--rows-per-sheet", type=int, default=6); p.add_argument("--max-per-row", type=int, default=8)
    p.add_argument("-j", "--jobs", type=int, default=4)
    p.set_defaults(fn=cmd_cull)

    p = sp.add_parser("organize", help="album: events x categories + delete suggestions (closed eyes, focus, dupes)")
    p.add_argument("images", nargs="+"); p.add_argument("-o", "--out")
    p.add_argument("--gap", type=float, default=30.0, help="burst grouping: max seconds between frames")
    p.add_argument("--strict", type=float, default=0.0, help="0..0.3: raise burst similarity bar")
    p.add_argument("--event-gap", type=float, default=3.0, help="hours of silence that start a new event")
    p.add_argument("--categories", help="JSON list of {key,label,prompts} replacing the built-in categories")
    p.add_argument("--thresholds", help='JSON overrides, e.g. \'{"blink": 0.6, "face_soft": 1.1}\'')
    p.add_argument("--apply", choices=["none", "link", "copy"], default="none",
                   help="also build album/<event>/<category>/ (+ _建議刪除/<reason>/) as symlinks or copies")
    p.add_argument("--review-folder", action="store_true", help="with --apply: put 'review' frames in _待確認/")
    p.add_argument("--category-sheets", action="store_true", help="also write one contact sheet series per category")
    p.add_argument("--thumb", type=int, default=300); p.add_argument("--cols", type=int, default=6)
    p.add_argument("--rows-per-sheet", type=int, default=6)
    p.add_argument("-j", "--jobs", type=int, default=4)
    p.set_defaults(fn=cmd_organize)

    p = sp.add_parser("masks", help="red-overlay preview of local-adjustment masks")
    p.add_argument("image"); p.add_argument("--auto", action="store_true", help="include auto-suggested masks")
    p.add_argument("--recipe", help="recipe / .recipe.json / per-file json containing masks")
    p.add_argument("--adjust", help="JSON string with a masks list")
    p.add_argument("--mask", action="append", help="mask type or JSON spec (repeatable)")
    p.add_argument("--size", type=int, default=700); p.add_argument("--cols", type=int, default=3)
    p.add_argument("-o", "--out"); p.set_defaults(fn=cmd_masks)

    p = sp.add_parser("people", help="detect people: ids, subject/bystander guess, overlay")
    p.add_argument("images", nargs="+"); p.add_argument("--size", type=int, default=1600)
    p.add_argument("--cols", type=int, default=2); p.add_argument("-o", "--out")
    p.set_defaults(fn=cmd_people)

    p = sp.add_parser("erase", help="remove bystanders (YOLOv8-seg + LaMa inpainting) into new files")
    p.add_argument("images", nargs="+")
    p.add_argument("--remove", default="bystanders", help="'bystanders' (default), 'all', or ids '2,3,5'")
    p.add_argument("--keep", help="ids to keep; everyone else is removed")
    p.add_argument("--grow", type=float, default=0.10, help="mask dilation as fraction of person height")
    p.add_argument("--extra", help="JSON list of extra normalised boxes [[x,y,w,h],...] to remove (leftover feet etc.)")
    p.add_argument("--preview", type=int, help="work at this long side (fast check)")
    p.add_argument("-o", "--out"); p.set_defaults(fn=cmd_erase)

    p = sp.add_parser("fill", help="remove people with real pixels borrowed from another frame (burst / re-shot)")
    p.add_argument("image"); p.add_argument("--source", nargs="+", required=True, help="other frame(s) of the same scene")
    p.add_argument("--remove", default="bystanders", help="'bystanders' (default), 'all', or ids '2,3,5'")
    p.add_argument("--keep", help="ids to keep; everyone else is removed")
    p.add_argument("--grow", type=float, default=0.08, help="mask dilation as fraction of person height")
    p.add_argument("-o", "--out", help="output file or folder"); p.set_defaults(fn=cmd_fill)

    p = sp.add_parser("stack", help="align + merge a handheld burst into one low-noise frame (night / high ISO)")
    p.add_argument("images", nargs="+"); p.add_argument("--ref", help="reference frame (default: the sharpest)")
    p.add_argument("--ghost", type=float, default=2.5, help="motion tolerance, x the pair's median difference (lower = stricter)")
    p.add_argument("--preview", type=int, help="work at this long side (fast check)")
    p.add_argument("-o", "--out", help="output file or folder"); p.set_defaults(fn=cmd_stack)

    p = sp.add_parser("mattes", help="extract the iPhone's own segmentation mattes (skin/hair/teeth/person/sky)")
    p.add_argument("images", nargs="+"); p.add_argument("-o", "--out", help="dir for <stem>.mattes/ (default: next to image)")
    p.set_defaults(fn=cmd_mattes)

    p = sp.add_parser("match"); p.add_argument("image"); p.add_argument("--ref", nargs="+", required=True)
    p.add_argument("--strength", type=float, default=0.8); p.add_argument("-o", "--out")
    p.set_defaults(fn=cmd_match)

    p = sp.add_parser("xmp"); p.add_argument("recipe", help="style name, style .json or .recipe.json")
    p.add_argument("--name"); p.add_argument("--group", default="Photo Edit Skill"); p.add_argument("-o", "--out")
    p.set_defaults(fn=cmd_xmp)

    p = sp.add_parser("photos", help="import finished photos into Apple Photos albums (syncs to iCloud if enabled)")
    p.add_argument("paths", nargs="+", help="output folder(s) or image files; each folder with images = one album")
    p.add_argument("--album", help="album name (default: the folder name)")
    p.add_argument("--batch", type=int, default=25, help="images per import call (big calls hit Photos error -609)")
    p.add_argument("--no-recursive", action="store_true", help="only the given folder, not its subfolders")
    p.add_argument("--dry-run", action="store_true", help="show which albums / how many photos, import nothing")
    p.set_defaults(fn=cmd_photos)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
