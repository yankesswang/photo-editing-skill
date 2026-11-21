# photo-edit-skill

Claude Code skill：把照片修成攝影師在 Lightroom 調過風格的成品。
說明與流程見 [SKILL.md](SKILL.md)，判斷準則見 [references/editing-guide.md](references/editing-guide.md)。

```bash
ln -sfn "$PWD" ~/.claude/skills/photo-edit     # 安裝
python3 scripts/pe.py styles                    # 列出風格
python3 scripts/pe.py board photo.jpg -o board.jpg
python3 scripts/pe.py develop /path/to/folder --style film-portra
python3 scripts/pe.py xmp film-portra           # 匯出 Lightroom 預設檔
```

依賴：numpy、opencv-python-headless、pillow（`uv run scripts/pe.py ...` 會自動安裝）。
