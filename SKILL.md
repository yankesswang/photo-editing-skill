---
name: photo-edit
description: 把照片修成攝影師在 Lightroom 調過風格的成品。大量照片先選片（連拍分組、模糊／曝光否決、挑最好的一張），逐張分析（曝光／白平衡／高光陰影／雜訊）先校正，再套上攝影師風格（電影感、底片 Portra、暗調秋景、黑白、夜景城市、日系明亮…）或從參考照片學風格，必要時用局部遮罩（主體提亮、天空、漸層、顏色範圍）；Claude 會實際看前後對照圖修正，輸出全解析度 JPG＋可匯入 Lightroom 的 .xmp 預設檔。Use when the user wants photos culled / selected, edited / color graded / retouched "like a photographer", Lightroom-style presets, batch editing a folder or SD card, matching a reference photo's look, or says 選片、修圖、調色、套濾鏡、修成攝影師風格、Lightroom 預設.
---

# photo-edit — 攝影師風格修圖

用 numpy/OpenCV 實作的 Lightroom 面板（基本、色調曲線、HSL、顏色分級、細節、效果、黑白、局部遮罩）。
參數名稱和範圍都跟 Lightroom 一致，所以每張成品都有一份看得懂的 recipe，也能匯出成 Lightroom 預設檔。

**修圖的核心是 Claude 自己用眼睛看。** 腳本負責算，Claude 負責判斷：每一輪都要 `Read` 渲染出來的對照圖，
按檢查清單挑問題、改參數、再看一次。不要沒看圖就交件。

`SKILL_DIR` = 這份檔案所在的資料夾（通常是 `~/.claude/skills/photo-edit`）。
所有指令：`python3 $SKILL_DIR/scripts/pe.py <cmd>`（缺套件就改用 `uv run $SKILL_DIR/scripts/pe.py <cmd>`，依賴寫在 PEP 723 header）。

## 原則

1. **原檔絕不覆寫。** SD 卡、相簿都只讀。輸出預設在 `~/Pictures/PhotoEdit/<資料夾>_<風格>/`，每張旁邊有 `.recipe.json`。
2. **先校正、後風格**（攝影師的順序）：逐張的 auto correction（曝光、白平衡、高光/陰影救回、ISO 對應降噪、逆光主體遮罩）＋ 整批共用的風格 ＋ 個別微調。
   疊加規則：基本面板滑桿（exposure/temp/contrast/highlights…）是**相加**；HSL、曲線、分級等巢狀設定是**覆蓋**；`masks` 清單是**串接**（同 `name` 的會被取代）。
3. **一批照片一個風格**，才像同一個攝影師修的；夜景／室內這類場景差太多的，可以拆成第二個風格。
4. **寧可克制，但要看得出來。** 專業成品很少一眼就看出「套了濾鏡」；但修完跟原圖看不出差別也是失敗。膚色、天空、白色物件、深色錨點是最容易露餡的地方。
5. **先全域、後局部。** 全域滑桿解決不了、而且硬拉會傷到別處時，才用遮罩（見「局部遮罩」）。

## 修圖紀律（每一張都適用）

**A. 動手前先寫「視覺簡報」**（3–5 行，依重要性排序，寫在給自己的筆記裡）：
1. **明暗結構**：主體在哪、亮塊與暗塊在哪、分離得強還是弱？哪些是光線造成、哪些可以靠後製改？
2. **黑白點與主體對比**：深色錨點（柏油、暗部、頭髮）夠不夠深？白色夠不夠亮？主體**內部**的明暗反差夠不夠？
3. **色彩，依材質與亮度區**：什麼東西帶什麼色、多強；哪些中性色必須維持中性（白牆、灰天、膚色）。
4. **質感最後**：顆粒、霧、柔焦是質感，救不了錯誤的影調。
最後一句：「這張最大的問題是＿＿，我要往＿＿方向修，必須保住＿＿。」

**B. 每次修改都用一句話寫出「問題 → 預期改善 → 可能代價」**，例如：「臉太暗 → 主體遮罩 +0.8EV → 可能在帽緣出現光暈，要看遮罩疊圖」。說不出問題就不要動。

**C. 回合上限：** 第一輪做一個完整、連貫的版本；看完最多再一輪針對性修正；第三輪只修實質缺陷（膚色壞掉、光暈、死白）或使用者要求。
連續兩輪沒有明顯變好就停，**交出最好的版本，不是最後一個版本**（舊版本的 recipe 都在 `.recipe.json`，可以回退）。

**D. 不要把同一件事做兩次。** highlights、whites、曲線肩部三個一起往下壓，白色會變灰；shadows、blacks、曲線趾部一起往上抬，深色錨點會消失。調之前先想清楚是哪一個控制在負責。

## 流程

### 0. 選片（照片多於 ~30 張時先做）
```bash
pe.py cull "/Volumes/NO NAME/DCIM/103MSDCF" -o ~/Pictures/PhotoEdit/103MSDCF_cull
```
- 腳本負責**技術面**：依拍攝時間＋外觀相似度把連拍分組 → 否決（模糊、暗到救不回、大面積過曝）→ 組內依清晰度＋曝光排名 → `picks.txt`（每組第 1 名）＋ `cull.json`。
  1662 張約 6–7 分鐘，結果有快取，重跑只算新照片。`--gap` 秒數／`--max-hash` 相似度門檻可調分組鬆緊。
- Claude 負責**瞬間**：`Read` 輸出的 `bursts_*.jpg`（每列一組連拍；綠框＝技術推薦，紅框＝否決）。
  有人物的組，依下列權重逐項判斷、覆寫技術推薦：
  瞬間與情緒 30%、清晰對焦 20%、曝光 15%、構圖 15%、光影 10%、色彩 5%、乾淨度 5%。
  - 一票否決：閉眼、臉被大面積遮住、主體糊掉、表情扭曲（要看臉，不要憑縮圖印象）。
  - 「瞬間」是動作或表情的巔峰定點，不是「正在動」；看鏡頭、笑開、手勢到位的那張通常勝過轉頭中的過渡幀。
- 再 `Read` `picks_*.jpg` 做整體取捨：重複的景點只留最好的、沒故事的記錄照可以拿掉。
  使用者指定張數時照張數挑；沒指定就交出全部入選，並說明各剔除了多少、為什麼。
- 把最終名單寫成一個檔案（一行一個路徑），之後用 `pe.py develop @final.txt ...` 處理。

### 1. 了解這批照片
```bash
pe.py contact @final.txt -o /tmp/.../contact.jpg --cols 8   # 或目錄、glob
pe.py analyze IMG1 IMG2 ...      # 曝光統計、場景標籤（night / backlit / foggy / people / foliage…）、auto_correction
```
搞清楚：拍什麼（旅行／人像／街拍／風景／夜景）、光線（陰天、逆光、鎢絲燈）、常見問題（灰、暗、歪、雜訊）。

### 2. 選風格
- 使用者指定了風格、攝影師或參考圖 → 照做（參考圖見第 5 步）。
- 否則挑 2–3 張有代表性的照片做風格板：
  ```bash
  pe.py board IMG --size 700 -o /tmp/.../board.jpg            # 全部風格
  pe.py board IMG --styles moody-autumn,film-portra,cinematic-teal-orange
  ```
  `Read` 風格板，依 `pe.py styles` 和 `references/editing-guide.md` 的「場景 → 風格建議」挑一個；拿不準時把風格板給使用者選（AskUserQuestion，附 2–3 個推薦）。
- 風格強度：`--strength 0.6`（淡）～`1.2`（重），對應 Lightroom 預設檔的 Amount。

### 3. 預覽＋逐張檢查（最重要）
```bash
pe.py develop IMG... --style STYLE --preview 1200 -o /tmp/.../preview     # 1200px 一張 <1 秒
pe.py review ORIGINAL_DIR /tmp/.../preview --per-sheet 6                  # 整批「原圖｜成品」成對排版
pe.py compare ORIG v1.jpg v2.jpg --labels "orig|v1|v2" --sep "|" -o /tmp/.../cmp.jpg
```
`Read` 每張 review sheet，用「修圖紀律 A」的簡報對照，逐項檢查 `references/editing-guide.md` 的檢查清單與**失敗訊號**。
有疑慮的地方（膚色、邊緣、天空漸層、雜訊）另外裁 100% 局部來看，縮圖看不出光暈和色階斷層。

有問題就寫個別微調檔（key 用檔名或去副檔名的 stem）：
```json
{"DSC07270": {"hsl": {"blue": {"sat": 5, "lum": -10}}},
 "DSC07059": {"straighten": -1.5, "crop": [0.0, 0.05, 1.0, 1.0]},
 "DSC06913": {"temp": -6, "skin_protect": 0.8, "hsl": {"yellow": {"sat": -15}}},
 "DSC07761": {"masks": [{"name": "sky", "mask": {"type": "sky"}, "adjust": {"exposure": -0.3, "dehaze": 15}}]}}
```
`pe.py develop ... --per-file fixes.json` 重新渲染，再看一次（遵守回合上限）。
整批都同一個問題（例如都太暖）→ `--adjust '{"temp": -8}'` 一次修全部。

### 4. 全解析度輸出
```bash
pe.py develop @final.txt --style STYLE --per-file fixes.json        # 不加 --preview = 全解析度
pe.py xmp STYLE                                                      # 匯出 Lightroom 預設檔
pe.py xmp OUT/DSC07761_moody-autumn.recipe.json --name "Hallstatt fog"
```
24MP 一張約 15 秒（8GB 機器）。`-j` 預設依記憶體自動決定（8GB＝1），**不要手動加大**：兩個 24MP 工作同時跑會 swap，每張變 5 分鐘。
上百張請用 `run_in_background`。EXIF 會保留，方向轉正後 Orientation 改成 1。

交件時附上：輸出資料夾、選片結果（幾張中留幾張）、用了哪個風格與理由、哪幾張有個別修正或遮罩（各修了什麼）、前後對照總覽圖。

### 5. 從參考照片學風格（使用者給了喜歡的攝影師作品）
```bash
pe.py match TARGET.jpg --ref REF1.jpg REF2.jpg -o my-look.json --strength 0.8
pe.py develop IMGS... --style my-look.json --preview 1200
```
轉移的是調色，不是內容：以中位數為軸的對比形狀、黑白點（褪色感）、近中性色像素的亮暗部色偏、整體飽和度。
參考圖給 2–5 張同一系列的效果最好。先描述參考圖「可轉移的特徵」與「只屬於那個場景的光線／內容」，不要把參考圖裡的藍天、橘葉當成調色。

## 局部遮罩

recipe 的 `masks` 是一串局部調整，每個 = 遮罩 + 局部滑桿：
```json
{"masks": [
  {"name": "subject lift", "mask": {"type": "subject", "subtract": [{"type": "sky"}]}, "adjust": {"exposure": 0.7, "shadows": 20}},
  {"name": "sky", "mask": {"type": "sky"}, "adjust": {"exposure": -0.4, "highlights": -30, "saturation": 10}},
  {"name": "grad top", "mask": {"type": "linear", "start": [0.5, 0.0], "end": [0.5, 0.45]}, "adjust": {"exposure": -0.5}},
  {"name": "green tame", "mask": {"type": "color", "hue": 100, "range": 40}, "adjust": {"saturation": -25}}
]}
```
| type | 用途 |
|---|---|
| `subject` / `face` | 人物（以偵測到的臉為錨點的頭＋上半身／只有臉）；逆光人像、臉太暗 |
| `sky` | 頂端連通、明亮平滑的區域；天空太亮、想加深或加色 |
| `linear` | 漸層濾鏡：`start` 處 100%、`end` 處 0%；天空亮前景暗 |
| `radial` | 橢圓（`center`、`size` 為寬高比例、`feather`）；把光打在主體上 |
| `luminance` | 亮度範圍（0–1）；只動暗部或亮部 |
| `color` | 色相範圍；只動某個顏色（綠草太螢光、水太綠） |
| `boxes` | 手動指定的人物框 `[x,y,w,h]`（偵測失敗時用） |

共通選項：`invert`、`amount`、`feather`、`intersect: [...]`、`subtract: [...]`。人物／天空遮罩預設會做邊緣貼合（guided filter）。
局部滑桿：temp、tint、exposure、contrast、highlights、shadows、whites、blacks、clarity、texture、dehaze、saturation、vibrance、hsl。

**遮罩一定要看疊圖再相信：**
```bash
pe.py masks IMG --auto                        # 自動建議的遮罩
pe.py masks IMG --recipe fixes.json           # 個別微調檔裡的遮罩
pe.py masks IMG --mask subject --mask sky     # 快速試
```
紅色＝被影響的範圍。檢查：有沒有吃到不該吃的（天空遮罩吃進白牆、主體遮罩蓋到背景）、邊緣有沒有斷層。
渲染後再看一次邊緣（帽緣、髮絲、建築輪廓）有沒有光暈。

自動行為：`analyze` 偵測到「臉比場景暗很多」（逆光、藍調時刻自拍）時，會自動加一個 `auto: subject lift` 遮罩（扣掉天空、只作用在暗部），全域曝光只補一點，避免整張被拉亮、天空和夜景燈光被洗掉。
不要的話，在個別微調檔放一個同名項目把它關掉：`{"masks": [{"name": "auto: subject lift", "mask": {"type": "subject", "amount": 0}}]}`。

常用組合（意圖 → 動作）：
- 天空亮、前景暗 → `sky` 或上方 `linear` 壓曝光／亮部 ＋ 下方 `linear`（`start` 在底部）提陰影。
- 主體不夠突出 → `subject` 提 0.3–0.6EV ＋ `subject` 的 `invert` 降飽和或降曝光 10–20%。
- 綠色太螢光但只想動草地 → `color` hue≈90–110 降飽和，再 `intersect` 下半部 `linear`。

## 自訂／新增風格
`styles/*.json` 一檔一風格，格式見 `references/editing-guide.md` 的「recipe 欄位」。
新風格建議先在 3 種場景（人像、風景、夜景）做 board 確認沒有翻車再存。

## 限制（老實說）
- 這是 Lightroom 演算法的近似，不是 Adobe 的引擎；同一組數字在 Lightroom 裡會接近但不會像素級一致。
- 輸入是 JPG（相機已經套過曲線）：高光救回的空間比 RAW 小得多，已經死白的天空救不回細節。
- 人物遮罩是「人臉錨點＋橢圓＋邊緣貼合」，不是 AI 分割；側臉、背影、很小的人可能偵測不到，這時用 `boxes` 手動指定。
- 選片的技術分數只比較同一組裡幾乎一樣的照片；哪些瞬間值得留，要靠 Claude 看圖判斷。
- `xmp` 匯出不包含局部遮罩（Lightroom 預設檔的遮罩格式不同），只匯出全域設定。
