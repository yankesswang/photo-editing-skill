"""Send finished photos to Apple Photos (and so to iCloud Photos, if the library syncs).

macOS only. Drives Photos.app through AppleScript (`osascript`), standard library only.

  - Every directory under the given paths that directly holds images becomes one album, named after
    the directory (`--album` overrides it when there is a single group). Sidecars (.recipe.json, .xmp)
    are never imported.
  - Imports in small batches (default 25): one 177-photo import call died with Photos error -609
    (connection invalid) while 25-photo batches went through cleanly. A batch that fails is retried
    after Photos has had a moment to settle.
  - Idempotent: file names already in the target album are skipped, so a re-run after a failure only
    sends what is missing and never makes duplicates.
  - Verifies at the end: the album's item count is compared with what should be in it.

Whether the photos then reach iCloud depends on Photos > Settings > iCloud > iCloud Photos; this
module only puts them in the local library.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff",
             ".dng", ".arw", ".cr2", ".cr3", ".nef", ".raf", ".orf", ".rw2"}

_ENSURE_ALBUM = r'''
on run argv
  set albumName to item 1 of argv
  tell application "Photos"
    if not (exists album albumName) then make new album named albumName
  end tell
  return "ok"
end run
'''

_ALBUM_NAMES = r'''
on run argv
  set albumName to item 1 of argv
  tell application "Photos"
    if not (exists album albumName) then return ""
    set names to filename of media items of album albumName
  end tell
  set AppleScript's text item delimiters to linefeed
  return names as text
end run
'''

_IMPORT = r'''
on run argv
  set listFile to item 1 of argv
  set albumName to item 2 of argv
  set fileList to paragraphs of (do shell script "cat " & quoted form of listFile)
  set aliasList to {}
  repeat with p in fileList
    if (p as text) is not "" then set end of aliasList to (POSIX file (p as text)) as alias
  end repeat
  with timeout of 1800 seconds
    tell application "Photos"
      set imported to import aliasList into album albumName skip check duplicates true
    end tell
  end timeout
  return (count of imported) as text
end run
'''


def _osa(script: str, *args: str, timeout: int = 1900) -> str:
    r = subprocess.run(["osascript", "-", *args], input=script, capture_output=True, text=True,
                       timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"osascript exit {r.returncode}")
    return r.stdout.strip()


def collect(paths: list[str], recursive: bool = True) -> dict[str, list[Path]]:
    """{album name: [image paths]} — one album per directory that directly contains images.
    Loose files given on the command line are grouped under their parent directory's name."""
    groups: dict[Path, list[Path]] = {}
    for p in map(Path, paths):
        p = p.expanduser().resolve()
        if p.is_file():
            if p.suffix.lower() in IMAGE_EXT:
                groups.setdefault(p.parent, []).append(p)
            continue
        if not p.is_dir():
            print(f"[photos] skip (not found): {p}", file=sys.stderr)
            continue
        walker = os.walk(p) if recursive else [(str(p), [], os.listdir(p))]
        for root, dirs, files in walker:
            dirs[:] = sorted(d for d in dirs if not d.startswith((".", "_")))
            imgs = sorted(Path(root) / f for f in files
                          if not f.startswith(".") and Path(f).suffix.lower() in IMAGE_EXT)
            if imgs:
                groups.setdefault(Path(root), []).extend(imgs)
    out: dict[str, list[Path]] = {}
    for d, imgs in groups.items():
        name = d.name
        if name in out:                                   # two dirs with the same name
            name = f"{d.parent.name}_{d.name}"
        out[name] = sorted(set(imgs))
    return out


def import_album(album: str, files: list[Path], batch: int = 25, retries: int = 2,
                 log=print) -> dict:
    _osa(_ENSURE_ALBUM, album)
    existing = set(filter(None, _osa(_ALBUM_NAMES, album).splitlines()))
    todo = [f for f in files if f.name not in existing]
    skipped = len(files) - len(todo)
    log(f"[photos] {album}: {len(files)} images, {skipped} already in album, {len(todo)} to import")

    imported, failed = 0, []
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("\n".join(str(f) for f in chunk))
            list_file = fh.name
        try:
            for attempt in range(retries + 1):
                try:
                    n = int(_osa(_IMPORT, list_file, album) or 0)
                    imported += n
                    log(f"[photos]   batch {i // batch + 1}: +{n}")
                    break
                except (RuntimeError, subprocess.TimeoutExpired, ValueError) as e:
                    if attempt == retries:
                        log(f"[photos]   batch {i // batch + 1} FAILED: {e}")
                        failed.extend(chunk)
                    else:
                        log(f"[photos]   batch {i // batch + 1} error ({e}); retrying")
                        time.sleep(5 * (attempt + 1))
        finally:
            os.unlink(list_file)

    final = set(filter(None, _osa(_ALBUM_NAMES, album).splitlines()))
    missing = [f.name for f in files if f.name not in final]
    return {"album": album, "total": len(files), "skipped_existing": skipped, "imported": imported,
            "failed": [str(f) for f in failed], "missing_after": missing,
            "album_count": len(final), "ok": not missing}


def run(paths: list[str], album: str | None = None, batch: int = 25, recursive: bool = True,
        dry_run: bool = False, log=print) -> list[dict]:
    if sys.platform != "darwin":
        raise SystemExit("photos: macOS only (needs Photos.app)")
    groups = collect(paths, recursive=recursive)
    if not groups:
        raise SystemExit("photos: no images found")
    if album:
        if len(groups) == 1:
            groups = {album: next(iter(groups.values()))}
        else:                                             # one named album for everything
            groups = {album: sorted({f for v in groups.values() for f in v})}
    if dry_run:
        for name, files in groups.items():
            log(f"[photos] would import {len(files):4d} -> album '{name}'")
        return [{"album": n, "total": len(f), "dry_run": True} for n, f in groups.items()]
    return [import_album(name, files, batch=batch, log=log) for name, files in groups.items()]
