---
name: photo-edit
description: 把照片修成攝影師在 Lightroom 調過風格的成品。逐張分析（曝光／白平衡／高光陰影／雜訊）先校正，再套上攝影師風格（電影感、底片 Portra、暗調秋景、黑白、夜景城市、日系明亮…），或從使用者給的參考照片學風格；Claude 會實際看前後對照圖修正，輸出全解析度 JPG＋可匯入 Lightroom 的 .xmp 預設檔。Use when the user wants photos edited / color graded / retouched "like a photographer", Lightroom-style presets, batch editing a folder or SD card, matching a reference photo's look, or says 修圖、調色、套濾鏡、修成攝影師風格、Lightroom 預設.
---

# photo-edit — 攝影師風格修圖

用 numpy/OpenCV 實作的 Lightroom 面板（基本、色調曲線、HSL、顏色分級、細節、效果、黑白）。
參數名稱和範圍都跟 Lightroom 一致，所以每張成品都有一份看得懂的 recipe，也能匯出成 Lightroom 預設檔。

**修圖的核心是 Claude 自己用眼睛看。** 腳本負責算，Claude 負責判斷：每一輪都要 `Read` 渲染出來的對照圖，
按檢查清單挑問題、改參數、再看一次。不要沒看圖就交件。

`SKILL_DIR` = 這份檔案所在的資料夾（通常是 `~/.claude/skills/photo-edit`）。
所有指令：`python3 $SKILL_DIR/scripts/pe.py <cmd>`（缺套件就改用 `uv run $SKILL_DIR/scripts/pe.py <cmd>`，依賴寫在 PEP 723 header）。

## 原則

1. **原檔絕不覆寫。** SD 卡、相簿都只讀。輸出預設在 `~/Pictures/PhotoEdit/<資料夾>_<風格>/`，每張旁邊有 `.recipe.json`。
2. **先校正、後風格**（攝影師的順序）：逐張的 auto correction（曝光、白平衡、高光/陰影救回、ISO 對應降噪）＋ 整批共用的風格 ＋ 個別微調。
   疊加規則：基本面板滑桿（exposure/temp/contrast/highlights…）是**相加**；HSL、曲線、分級等巢狀設定是**覆蓋**。
3. **一批照片一個風格**，才像同一個攝影師修的；夜景／室內這類場景差太多的，可以拆成第二個風格。
4. 寧可克制。專業的成品很少一眼就看出「套了濾鏡」。膚色、天空、白色物件是最容易露餡的地方。

## 流程

### 1. 了解這批照片
```bash
pe.py contact "/Volumes/NO NAME/DCIM/103MSDCF" -o /tmp/.../contact.jpg --cols 8   # 大量照片先抽樣：目錄可換成 glob 或挑選的檔案
pe.py analyze IMG1 IMG2 ...      # 曝光統計、場景標籤（night / backlit / foggy / people / foliage…）、auto_correction
```
`Read` 縮圖總覽。數百張照片時，從 `ls` 每隔 N 張挑 20–30 張做總覽，不要全部讀。
搞清楚：拍什麼（旅行／人像／街拍／風景／夜景）、光線（陰天、逆光、鎢絲燈）、常見問題（灰、暗、歪、雜訊）。

### 2. 選風格
- 使用者指定了風格、攝影師或參考圖 → 照做（參考圖見第 5 步）。
- 否則挑 2–3 張有代表性的照片做風格板：
  ```bash
  pe.py board IMG --size 700 -o /tmp/.../board.jpg            # 全部風格
  pe.py board IMG --styles moody-autumn,film-portra,cinematic-teal-orange
  ```
  `Read` 風格板，依 `pe.py styles`（每個風格的說明與適用場景）和 `references/editing-guide.md` 的「場景 → 風格建議」挑一個；拿不準時把風格板給使用者選（AskUserQuestion，附 2–3 個推薦）。
- 風格強度：`--strength 0.6`（淡）～`1.2`（重），對應 Lightroom 預設檔的 Amount。

### 3. 預覽＋逐張檢查（最重要）
```bash
pe.py develop IMG... --style STYLE --preview 1200 -o /tmp/.../preview     # 1200px 一張 <1 秒
pe.py review ORIGINAL_DIR /tmp/.../preview --per-sheet 6                  # 整批「原圖｜成品」成對排版（每張 sheet 6 對）
pe.py compare ORIG v1.jpg v2.jpg --labels orig,v1,v2 -o /tmp/.../cmp.jpg  # 單張多版本比較
```
`Read` 每張 review sheet，逐張逐項檢查 `references/editing-guide.md` 的清單：
主體曝光、膚色、高光死白／天空、陰影死黑、色偏、過飽和、地平線歪斜、光暈／色階斷層、高 ISO 雜訊。

實測學到的（Sony a5100 JPG 旅行照）：
- 人臉偵測會**漏掉暗臉／戴帽子的臉**，這時沒有膚色保護、曝光也不會以臉為準 → 看到人就手動加 `"skin_protect": 0.8` 並自己判斷曝光。
- 逆光／藍調時刻的人像：臉要亮到看得清楚，常需要 `exposure +0.6~1.0`、`shadows +40`、`highlights -30`、`blacks +10~15`，風格的 contrast／blacks 往回抵消。
- 鎢絲燈餐廳：自動白平衡只修三成，保留暖意；仍太黃就再 `temp -5~-10`、`hsl.yellow.sat -15`。
- 風格把某個顏色壓過頭（例如 moody 的藍天變灰）→ 在個別微調檔覆寫那一色的 `hsl`，不必換風格。

有問題就寫個別微調檔（key 用檔名或去副檔名的 stem）：
```json
{"DSC07480": {"exposure": 0.3, "shadows": 15, "temp": -8},
 "DSC07059": {"straighten": -1.5, "crop": [0.0, 0.05, 1.0, 1.0]},
 "DSC06913": {"temp": -12, "hsl": {"orange": {"sat": -15}}}}
```
`pe.py develop ... --per-file fixes.json` 重新渲染，再看一次。一張最多改 2–3 輪；改不好就降低 `--strength` 或換風格。
整批都同一個問題（例如都太暖）→ 用 `--adjust '{"temp": -8}'` 一次修全部。

### 4. 全解析度輸出
```bash
pe.py develop IMG... --style STYLE --per-file fixes.json            # 不加 --preview = 全解析度
pe.py xmp STYLE                                                      # 匯出 Lightroom 預設檔（~/Pictures/PhotoEdit/lightroom-presets/）
pe.py xmp OUT/DSC07761_moody-autumn.recipe.json --name "Hallstatt fog"   # 單張完整參數也能匯出
```
24MP 一張約 15 秒（8GB 機器）；`-j` 平行數會依記憶體自動決定，不要開太多會 swap。
上百張請用 `run_in_background`。EXIF 會保留，方向轉正後 Orientation 改成 1。

交件時附上：輸出資料夾、用了哪個風格與理由、哪幾張有個別修正（各修了什麼）、一張前後對照總覽圖。

### 5. 從參考照片學風格（使用者給了喜歡的攝影師作品）
```bash
pe.py match TARGET.jpg --ref REF1.jpg REF2.jpg -o my-look.json --strength 0.8
pe.py develop IMGS... --style my-look.json --preview 1400
```
轉移的是調色，不是內容：以中位數為軸的對比形狀、黑白點（褪色感）、近中性色像素的亮暗部色偏、整體飽和度。
參考圖給 2–5 張同一系列的效果最好。產出的 json 就是普通 recipe，可以再手動調 HSL（例如綠色往黃移）、
存進 `styles/` 變成新的固定風格（補上 `description`/`use_for`），或匯出 xmp。

## 自訂／新增風格
`styles/*.json` 一檔一風格，格式見 `references/editing-guide.md` 的「recipe 欄位」。
新風格建議先在 3 種場景（人像、風景、夜景）做 board 確認沒有翻車再存。

## 限制（老實說）
- 這是 Lightroom 演算法的近似，不是 Adobe 的引擎；同一組數字在 Lightroom 裡會接近但不會像素級一致。
- 輸入是 JPG（相機已經套過曲線）：高光救回的空間比 RAW 小得多，已經死白的天空救不回細節。
- 沒有局部遮罩（筆刷、天空/主體 AI 遮罩）；局部問題只能靠 highlights/shadows、HSL 和裁切處理。
