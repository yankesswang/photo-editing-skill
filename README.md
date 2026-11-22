# photo-edit-skill

讓 Claude 像攝影師一樣修照片的 [Claude Code](https://claude.com/claude-code) skill：從一整張 SD 卡選片、逐張校正、套上 Lightroom 式的風格，最後**親眼看前後對照再修正**，輸出全解析度 JPG 和可匯入 Lightroom 的 `.xmp` 預設檔。

A Claude Code skill that culls, corrects and colour-grades your photos like a photographer — a numpy/OpenCV re-implementation of the Lightroom develop panels, driven by Claude's visual judgement.

![Hallstatt — before / after](docs/images/DSC07991_compare.jpg)

## 實際效果

以下都是同一趟旅行（Sony a5100 JPG，未經任何前處理）用這個 skill 修出來的成品。左邊原圖、右邊修後。
從 1645 張選出 158 張；白天用 `film-portra`、夜間用 `night-city`，再依場景個別修正。

| | |
|---|---|
| ![](docs/images/DSC07766_compare.jpg) 霧中的國王湖：陰天專用修正（壓黑位、加對比、修掉灰天變米黃） | ![](docs/images/DSC07663_compare.jpg) 霧與秋色：保留空氣感，色彩更有底片味 |
| ![](docs/images/DSC07277_compare.jpg) 英雄廣場：晴天的底片藍天 | ![](docs/images/DSC07550_compare.jpg) 日落剪影：天空層次拉出來，剪影維持黑色 |
| ![](docs/images/DSC08496_compare.jpg) 普拉特夜景：深藍夜空＋乾淨暖光 | ![](docs/images/DSC08306_compare.jpg) 自然史博物館：室內低光降噪、壓住亮部 |
| ![](docs/images/DSC08288_compare.jpg) 博物館標本：高 ISO 降噪與立體感 | |

清除路人（`pe.py erase`）：自動找出畫面裡的每個人並分成主角／路人，路人用 LaMa 補上背景。下圖清掉了廣場上的 4 位遊客，右下角站在深色門口前的那位刻意保留，因為他背後的結構太複雜，補出來會穿幫。

![bystander removal](docs/images/erase_DSC07991.jpg)

同一張照片套上多種風格（`pe.py board`）：

![style board](docs/images/style_board.jpg)

## 功能一覽

### 📥 讀取與取片
- **JPG／HEIC／RAW 直接讀**：支援 Sony ARW、DNG、Canon CR3/CR2、Nikon NEF、Fuji RAF 等；RAW 亮度自動對齊相機內嵌 JPG，EXIF 照常讀取
- **從 macOS「照片」圖庫取片**：依日期＋GPS 找出某次旅行的照片，分批匯出（SKILL.md 內有完整流程）
- **iPhone 原檔遮罩**（`pe.py mattes`）：抽出 iPhone 自帶的皮膚／頭髮／牙齒／人物／天空遮罩，精度到髮絲

### 🗂️ 選片與相簿整理
- **選片**（`pe.py cull`）：連拍分組、否決模糊／過暗／過曝，組內挑最好的一張，人物照以**臉部**清晰度判斷
- **相簿整理**（`pe.py organize`）：依活動（時間）＋內容（人像、風景、美食、夜景…）分類
- **建議刪除**：閉眼、失焦、手震、太暗、過曝、誤拍、連拍重複；輸出可勾選的 HTML 報告，**絕不自動刪檔**

### 🎨 修圖與調色
- **自動校正**（`pe.py analyze`）：曝光（中央加權或人臉測光）、白平衡、高光陰影、依 ISO 降噪
- **12 種攝影師風格**（`pe.py board` 並排比較）：底片 Portra、電影感、暗調秋景、黑白、夜景城市、日系明亮…
- **整批一致**（`develop --consistent`）：同場景照片共用白平衡與影調，曝光仍逐張，不會一張暖一張冷
- **天空修復**（recipe `sky`）：壓亮部救雲層、漸層減光、陰天加淡藍；白牆／岩石不會被誤當天空
- **局部遮罩**（recipe `masks`）：人物、天空、漸層、橢圓、亮度範圍、顏色範圍、iPhone 遮罩
- **從參考照片學風格**（`pe.py match`）：給一張喜歡的作品，推出一組調色參數

### 👤 人像
- **輕微美顏**：以 iPhone 皮膚遮罩讓臉維持相機原色，只做輕微磨皮（保留毛孔）、淡化黑眼圈、牙齒去黃、稍微瘦臉
- **`vivid-iphone` 風格**：背景鮮豔、臉保持原樣，強度已依實際退件經驗調到「看不出修過」

### 🌙 降噪
- **連拍疊合降噪**（`pe.py stack`）：手持連拍自動對齊（特徵點＋ECC＋光流）後合併，移動的人車自動排除；3 張約降噪 45%
- **單張 NLM 降噪**：夜景／高 ISO 用，比預設更強且保留邊緣

### 🧹 清除路人
- **人物偵測與編號**（`pe.py people`）：YOLOv8 分割每個人，自動猜主角／路人
- **AI 補背景**（`pe.py erase`）：LaMa 補上路人位置，清完再偵測一次補清殘影
- **用真實像素補**（`pe.py fill`）：有連拍時拿另一張的真實背景補，不靠 AI 生成；對不齊或貼到別人時會警告

### 📤 輸出
- **全解析度 JPG**＋每張的 `.recipe.json`（記錄所有參數，可重現）
- **Lightroom 預設檔**（`pe.py xmp`）：風格或單張參數匯出成 `.xmp`，可匯入 Lightroom Classic／Camera Raw
- **匯入照片 App**（`pe.py photos`）：每個資料夾一本相簿，分批匯入、可重跑不重複，開了 iCloud 照片就自動同步到手機
- **前後對照**（`pe.py review`／`compare`／`contact`）：整批「原圖｜成品」總覽，Claude 逐張看過再交件

引擎實作的 Lightroom 面板：白平衡、曝光、對比、亮部／陰影／白色／黑色、紋理、清晰度、去朦朧、細節飽和度／飽和度、點曲線（RGB 分色）、8 色 HSL、顏色分級（陰影／中間調／亮部／整體）、黑白混色、銳利化、降噪、暗角、顆粒、旋轉／拉直／裁切、局部遮罩。參數名稱和範圍都對應 Lightroom。

## 內建風格

| 風格 | 適合 |
|---|---|
| `clean-natural` | 任何照片；乾淨、真實、不像套濾鏡 |
| `film-portra` | 人像、旅行、白天；暖色底片、柔和高光、好看的膚色 |
| `moody-autumn` | 霧、陰天、秋天風景；暗調、橄欖綠、暖亮部 |
| `cinematic-teal-orange` | 城市、街拍、黃昏；電影感青橙色調 |
| `muted-chrome` | 街拍、建築、紀實；低飽和、硬朗陰影 |
| `bright-airy` | 人像、咖啡廳、雪景；明亮、低對比、粉嫩 |
| `bw-fineart` | 建築、戲劇性天空；高反差黑白 |
| `night-city` | 夜景、室內、高 ISO；深藍陰影＋暖色燈光 |
| `nordic-matte` | 極簡、雪、海岸；冷調霧面 |
| `golden-hour` | 日落、人像；暖金色光暈 |
| `film-portra-clear` | 晴天旅行；Portra 色調但保留藍天 |
| `vivid-iphone` | iPhone 照片；鮮豔背景＋臉維持原色的輕微美顏（搭配 `--no-auto`） |

風格就是 `styles/*.json`，可以自己加；`pe.py xmp <風格>` 可以帶進 Lightroom 用。

## 安裝

```bash
git clone https://github.com/yankesswang/photo-edit-skill.git
ln -sfn "$PWD/photo-edit-skill" ~/.claude/skills/photo-edit
```

依賴：Python 3.9+、numpy、opencv-python-headless、pillow。RAW 需要 `rawpy`、HEIC 直讀需要 `pillow-heif`（`pip install rawpy pillow-heif`；沒裝 pillow-heif 會自動用 macOS 的 `sips` 轉）。
沒裝的話用 `uv run scripts/pe.py ...`，依賴寫在 PEP 723 header，會自動安裝。

清除路人另外需要 `torch` 和 `ultralytics`（`pip install torch ultralytics`）；兩個模型（YOLOv8s-seg 24MB、LaMa 196MB）第一次使用時自動從官方 GitHub release 下載到 `~/.cache/photo-edit/models/`。

相簿整理（`pe.py organize`：分類＋建議刪除閉眼／失焦／模糊／重複）另外需要 `mediapipe`、`torch`、`transformers`（`pip install mediapipe torch transformers`）；臉部模型（MediaPipe，約 4MB）第一次使用時下載，CLIP（`openai/clip-vit-base-patch32`，約 600MB）走 HuggingFace 快取。

## 使用

在 Claude Code 裡直接說：

> 幫我把 /Volumes/NO NAME/DCIM/103MSDCF 的照片選片並修成攝影師風格

Claude 會依 [SKILL.md](SKILL.md) 的流程：選片 → 分析 → 做風格板讓你挑 → 預覽並逐張檢查 → 個別修正 → 全解析度輸出。原檔不會被修改。

也可以直接用 CLI：

```bash
python3 scripts/pe.py styles                                   # 列出風格
python3 scripts/pe.py cull /path/to/DCIM -o out/cull            # 選片
python3 scripts/pe.py board photo.jpg -o board.jpg              # 風格板
python3 scripts/pe.py develop @out/cull/picks.txt --style film-portra --preview 1200 -o out/preview
python3 scripts/pe.py review /path/to/DCIM out/preview          # 前後對照
python3 scripts/pe.py develop @out/cull/picks.txt --style film-portra --per-file fixes.json -o out/final
python3 scripts/pe.py develop DIR --style film-portra --consistent                     # 同場景共用白平衡／影調
python3 scripts/pe.py develop DSC0001.ARW --style clean-natural                        # RAW 直接修
python3 scripts/pe.py develop IMG.jpg --style clean-natural --adjust '{"sky": {"blue": 40}}'   # 陰天天空
python3 scripts/pe.py stack burst1.jpg burst2.jpg burst3.jpg -o out/merged.jpg         # 夜景連拍疊合降噪
python3 scripts/pe.py masks photo.jpg --mask subject --mask sky # 遮罩疊圖
python3 scripts/pe.py people photo.jpg -o people.jpg            # 人物編號疊圖（綠＝主角、紅＝路人）
python3 scripts/pe.py erase photo.jpg --keep 0 -o out/erased     # 只留 0 號，其他人清掉
python3 scripts/pe.py fill photo.jpg --source burst2.jpg --keep 0 -o out/erased   # 用另一張連拍的真實背景補
python3 scripts/pe.py match photo.jpg --ref ref1.jpg ref2.jpg -o my-look.json
python3 scripts/pe.py xmp film-portra                           # Lightroom 預設檔
```

個別微調檔（`--per-file`）的 key 是檔名：

```json
{
  "DSC07550": {"shadows": -55, "blacks": -20},
  "DSC07047": {"rotate": 90},
  "DSC07480": {"masks": [{"name": "face", "mask": {"type": "subject", "subtract": [{"type": "sky"}]},
                          "adjust": {"exposure": 0.8, "shadows": 20}}]}
}
```

## 設計重點

- **先校正、後風格**：每張照片先做自動校正（曝光、白平衡、高光陰影、降噪），再疊上整批共用的風格。這是攝影師的順序，也讓同一批照片看起來像同一個人修的。
- **Claude 要親眼看**：腳本負責算，判斷交給 Claude。SKILL.md 規定動手前先寫視覺簡報、每次修改寫明「問題 → 預期改善 → 代價」、回合上限、交出最好的版本而不是最後一個，並附上失敗訊號清單（橘色塑膠皮膚、灰掉的白色、遮罩光暈…）。
- **個人審美檔**：`~/.config/photo-edit/taste.md` 累積你的偏好，每次開工先讀。
- **8GB 筆電可跑**：24MP 一張約 15 秒；選片 1600 張約 6 分鐘（有快取）。

## 限制

- 這是 Lightroom 演算法的近似，不是 Adobe 的引擎；同一組數字在 Lightroom 裡會接近，但不會像素級一致。
- JPG 輸入高光救回空間小；有 RAW 就用 RAW。RAW 解碼沒有鏡頭變形／暗角校正。
- 連拍疊合只適合站位不動的連拍；人或相機在動的一系列照片合不起來（會退回單張，不會壞）。
- 人物遮罩是「人臉錨點＋橢圓＋邊緣貼合」，不是 AI 分割；側臉、背影偵測不到時要手動指定框。
- 選片的技術分數只比較同一組裡幾乎一樣的照片；哪些瞬間值得留，靠 Claude 看圖判斷。
- `.xmp` 匯出不含局部遮罩和清除路人。
- 清除路人是在**生成**原本不存在的背景：小路人在石板路、牆面、天空前效果很好；人比較大、背後是門口／招牌／人群時會留下抹痕或殘影，這時應該保留那個人。紀實或證件用途的照片不要用。

## 參考

設計上參考了這些開源專案的方法（沒有複製程式碼）：
[lightroom-style](https://github.com/Wyyyyuu/lightroom-style)（分亮度區色彩量測、回合紀律）、
[raw-photo-agent](https://github.com/Timverhoogt/raw-photo-agent)（候選版本與獨立評審）、
[photo-skills](https://github.com/konanok/photo-skills)（選片與一致性模式）、
[chemigram](https://github.com/chipi/chemigram)（意圖 → 動作詞彙、taste 檔）、
[muse-skills](https://github.com/GuohongLi/muse-skills)（選片評分維度、畫質標準）、
[computer-use-photo-editing-skills](https://github.com/eamonboyle/computer-use-photo-editing-skills)（調色失敗訊號、構圖決策）、
[photo-retouch-pro](https://github.com/qzfcoder/photo-retouch-pro)（強度分級）。

範例照片為作者本人拍攝。
