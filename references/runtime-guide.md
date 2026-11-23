# 執行環境指南

## 能力檢查與 AI 適配

先確認專案位置，切換到含有 `SKILL.md` 的資料夾並執行：

```sh
uv run scripts/pe.py doctor
```

`doctor` 只檢查作業系統、工具路徑與套件是否存在，不測試模型載入、系統授權、圖片檢視能力或網路。加上本次需要的 `--with` 再執行一次，才代表實際執行環境。例如：

```sh
uv run --with rawpy --with pillow-heif scripts/pe.py doctor
```

AI 宿主可為 Codex、Claude Code、Gemini CLI 或其他產品。無需特定模型 API、API key 或工具名稱；由宿主提供讀檔、命令執行與圖片檢視能力。本專案不代為呼叫任何聊天模型 API，也不配置宿主帳號。

| 能力 | 執行方式 | 不具備時 |
|---|---|---|
| 讀取文件 | 讀取 SKILL.md、相關參考文件與 JSON | 使用者提供內容；不能宣稱已讀取未提供的文件 |
| 執行命令 | 使用宿主的終端／shell 工具 | 提供可執行命令，由使用者執行 |
| 檢視圖片 | 開啟風格板、選片總覽、前後對照與 100% 裁切 | 由使用者檢視或交給視覺模型；標記尚未完成視覺檢查 |
| 修改參數 | 寫入 UTF-8 的 JSON，再執行預覽 | 提供 JSON 內容，由使用者存檔 |

## 三平台共用工作流程

1. 確認輸入照片目錄與輸出目錄；不要覆寫原檔。
2. 依格式選依賴，再執行 `doctor`。初次模型下載需網路，離線時先確認模型快取存在。
3. 大量照片先 `cull`；要內容分類與建議刪除才用 `organize`。
4. `analyze` 與 `board`，讓使用者挑風格。
5. `develop --preview 1200`，使用圖片工具檢查 `review`／`compare` 和必要的 100% 裁切。
6. 把修正存為 JSON；需要個別調整用 `--per-file`。依 SKILL.md 的回合規則修正。
7. 全解析度輸出 JPG 與 `.recipe.json`，按需求匯出 XMP。只有 macOS 才能接續照片 App 匯入。

CLI 只執行命令，不會自動找 AI 模型看圖。無圖片工具時，依然可以輸出，但交件說明需列出待人工檢查的項目。

## 平台差異

| 項目 | Windows PowerShell | Linux shell | macOS shell |
|---|---|---|---|
| 相片路徑範例 | `"E:/DCIM"` | `"/media/user/SD/DCIM"` | `"/Volumes/SD/DCIM"` |
| 共用入口 | `uv run scripts/pe.py` | 同左 | 同左 |
| 已安裝依賴的 Python | 先確認 `python` 或 `py` | 先確認 `python3` | 先確認 `python3` |
| 清單參數 | `"@out/cull/picks.txt"` | 同左 | 同左 |
| 暫存／預覽 | `out/preview` 或系統暫存目錄 | 同左 | 同左 |
| 分類目錄 | 預設只出報告；需要檔案時 `--apply copy` | 可選 link／copy | 可選 link／copy |
| HEIC 缺少 pillow-heif | 補套件或先轉 JPG | 補套件或先轉 JPG | 可用系統 sips |
| Apple 照片 App／遮罩抽取 | 跳過，使用本機資料夾／既有 sidecar | 同左 | 按使用者需求執行 |

`~` 指當前使用者家目錄，Windows 不代表磁碟根目錄。Python 預設輸出為 `Path.home() / "Pictures" / "PhotoEdit"`；要使用其他位置請明確指定 `-o`。審美檔使用家目錄下的 `.config/photo-edit/taste.md`，這是本工具約定，不假設平台有自動建立它。

所有文字輸出使用 UTF-8；讀入 JSON 與路徑清單接受 UTF-8 BOM。Windows PowerShell 寫入檔案可使用 `Set-Content -Encoding utf8`。避免把 JSON 直接塞進命令列；先存成檔案再交給 `--adjust`、`--per-file` 或 `--thresholds`。

## 功能依賴與替代流程

| 功能 | 額外依賴／工具 | 替代流程 |
|---|---|---|
| RAW | `--with rawpy` | 使用相機輸出的 JPG |
| HEIC | `--with pillow-heif`；macOS 可用 sips | 先匯出 JPG |
| 人像修飾 | `--with mediapipe`，首次下載臉部模型 | 不啟用 portrait 修飾；正常調色仍可用 |
| 相簿整理 | `--with mediapipe --with torch --with transformers` | 使用較輕量的 cull，手動分類 |
| 人物偵測／清除／真實背景填補 | `--with torch --with ultralytics`，首次下載模型 | 保留人物；不能把 fill 描述成完全不生成 |
| 抽取 iPhone 遮罩 | macOS、swiftc、Apple ImageIO，且原檔含對應資料 | 在 Mac 抽取後複製 sidecar，或使用一般遮罩 |
| 讀取既有遮罩 | `<照片 stem>.mattes/` 與照片同目錄 | 沒有 sidecar 時不要承諾 iPhone 原生遮罩效果 |
| 照片 App | macOS、osascript、使用者允許控制照片 App | Windows／Linux 交付檔案到指定目錄，由使用者匯入相簿服務 |

複合功能必須合併依賴。例如：

```sh
uv run --with pillow-heif --with torch --with ultralytics scripts/pe.py people "photo.HEIC" -o out/people.jpg
```

套件是否提供適用的 Python 版本、作業系統與 CPU 架構 wheel，需在目標環境確認；有 GPU 並不代表目前所有處理會使用 GPU。

## 原檔與刪除清單

`organize` 的 `delete.txt` 是建議，不是刪除命令。讓使用者檢查 HTML 與總覽圖，再提供確認清單。只有使用者授權後才使用可用的作業系統垃圾桶／資源回收筒工具；不可跨平台硬套 `mv ~/.Trash`，也不可改成永久刪除。沒有可用回收工具時交付確認清單，由使用者處理。

## 驗證範圍

本次修改在 Linux 執行核心 CLI 冒煙測試，並模擬平台條件檢查不支援功能的訊息。模擬不等於 Windows／macOS 實機測試；Apple 照片 App、Swift、HEIC／RAW 與選配模型仍需對應平台與素材驗證。

## 執行驗證

核心依賴限制 OpenCV 4.x，因為目前的人臉偵測使用 `CascadeClassifier`。開發驗證使用同樣的版本限制：

```sh
uv run --with numpy --with "opencv-python-headless>=4.8,<5" --with pillow python tests/test_portability.py
```

測試涵蓋中文與空白路徑、PowerShell 常見的 UTF-8 BOM、非 macOS 功能防護，以及選片、風格板、預覽調色、對照與 XMP 輸出的 CLI 流程。測試產物放在系統暫存目錄並自動清理。
