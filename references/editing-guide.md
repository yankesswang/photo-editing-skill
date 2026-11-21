# 攝影師修圖指南（給 Claude 判斷用）

## 攝影師的修圖順序
1. **構圖**：地平線／建築垂直線拉正（`straighten`，正值＝順時針），裁掉邊緣干擾（`crop`）。
2. **白平衡**：白色、灰色物件該是中性的；但日落、鎢絲燈餐廳要**保留部分暖色**，那是氣氛。
3. **曝光**：以**主體**為準，不是整張平均。人像看臉（臉的亮度約落在 0.5–0.6），風景看中間調。
4. **動態範圍**：天空偏亮就 `highlights -40~-80`，暗部太黑就 `shadows +20~+50`；再用 `whites`/`blacks` 定黑白點。
5. **立體感**：`clarity`（中尺度對比，建築／風景 +10~+25，人像 ≤ +5 甚至負值柔膚）、`texture`（細節）、`dehaze`（霧、遠景）。
6. **風格**：色調曲線（褪色＝抬高黑點）、HSL（攝影師風格的核心）、顏色分級（亮暗部分色）。
7. **收尾**：降噪（高 ISO）、銳利化、暗角、顆粒。

## 逐張檢查清單（看前後對照時逐項掃過）
| 檢查 | 症狀 | 修法 |
|---|---|---|
| 主體曝光 | 臉／主體太暗或太亮 | `exposure ±0.2~0.5`；只有暗部太暗就加 `shadows`；只有主體暗 → `subject` 遮罩 |
| 膚色 | 偏紅、偏橘、像曬傷；偏綠、偏灰 | `hsl.orange.sat -10~-20`、`hsl.red.hue +5~+10`、`temp` 往回拉；有人臉時會自動 `skin_protect` |
| 高光 | 天空死白、白牆沒層次 | `highlights` 更負、`whites -10~-20` |
| 陰影 | 死黑一片（夜景除外） | `shadows +`、`blacks +5~+15` |
| 色偏 | 整張偏黃／藍／綠／洋紅 | `temp`（正＝暖）、`tint`（正＝洋紅、負＝綠） |
| 過飽和 | 草地螢光綠、天空藍得假 | `hsl.green.sat`、`hsl.blue.sat` 降、`saturation -` |
| 平淡 | 霧濛濛、灰 | `contrast +`、`dehaze +10~+25`、`clarity +` |
| 地平線 | 水平線／建築歪 | `straighten`（每次 0.5–2 度試） |
| 雜訊 | 高 ISO 夜景顆粒、彩色斑點 | `noise.luminance 30~50`、`noise.color 40~60` |
| 光暈 | 高對比邊緣出現亮邊 | 降 `clarity`、降 `shadows`/`highlights` 的幅度 |
| 一致性 | 同一批照片色調跳來跳去 | 同一風格；個別只修曝光／白平衡，不要逐張換風格 |
| 主體不突出 | 人跟背景一樣亮、一樣花 | `subject` 遮罩 +0.3~0.6EV；反向遮罩降飽和／降曝光 10–20% |
| 逆光暗臉 | 臉黑、背景正常 | `subject` 遮罩（扣 `sky`、交集暗部亮度），不要只靠全域曝光 |
| 天空 vs 前景 | 天空亮、地面暗，全域怎麼拉都顧此失彼 | `sky` 或 `linear` 遮罩分開處理 |
| 看不出差別 | 修完跟原圖並排幾乎一樣 | 提高 `--strength`；用遮罩拉開主體層次，而不是只加飽和／銳利／暖色 |

## 失敗訊號（看到任何一個就退回去）
- **橘色塑膠皮膚**：降 `hsl.orange.sat`、降高光分級飽和度，或加 `skin_protect`。
- **頭髮陰影出現青色髒污**、**紫色黑位**：陰影分級飽和度太高，砍 30–50%。
- **天空漸層出現彩虹斷層**：HSL 色相移動或藍色飽和度太大；天空用明度壓深，不要硬拉飽和。
- **白色變灰**：highlights、whites、曲線肩部疊在一起往下壓；只留一個負責。
- **深色錨點被洗成灰**：柏油、黑衣、暗部變灰會讓整張發「浮」、顯髒。「暗部不死黑」不等於「把暗部提亮」——提陰影只為找回細節。
- **遮罩光暈**：帽緣、髮絲、建築輪廓外出現亮邊／暗邊；縮小遮罩、加 `subtract`、降局部幅度。
- **每張都像同一個 IG 濾鏡**：風格強度太高或風格不適合這個場景；降 `--strength`。
- **夜景修成白天**：夜景／藍調時刻要保留夜感，暗部提亮只為找回層次。

## 交件前三問（風景、旅行特別適用）
1. 有沒有忠實呈現按快門那一刻看到的東西？（沒有發明不存在的雲、光、結構）
2. 有沒有呈現身歷其境的感覺？
3. 跟原圖並排，差異落在「增強已有內容」，而不是「改變了照片」？

## 場景 → 風格建議
| 場景 | 首選 | 備選 |
|---|---|---|
| 陰天、霧、秋天風景、湖、山村 | `moody-autumn` | `nordic-matte`、`film-portra` |
| 人像、旅行合照（白天） | `film-portra` | `clean-natural`、`bright-airy`、`golden-hour` |
| 城市、街拍、建築 | `muted-chrome` | `cinematic-teal-orange`、`bw-fineart` |
| 夜景、室內、博物館、高 ISO | `night-city` | `cinematic-teal-orange`、`bw-fineart` |
| 日落、傍晚 | `golden-hour` | `film-portra` |
| 咖啡廳、食物、雪景、明亮室內 | `bright-airy` | `clean-natural` |
| 混光、顏色很醜但光影很好 | `bw-fineart` | — |
| 只想「修好」不要風格 | `clean-natural` 或 `--style none` | — |

## recipe 欄位（與 Lightroom 對應）
| recipe | Lightroom | 範圍 |
|---|---|---|
| `temp`, `tint` | 色溫、色調（JPG 為相對值） | -100..100 |
| `exposure` | 曝光度 | EV |
| `contrast`, `highlights`, `shadows`, `whites`, `blacks` | 對比、亮部、陰影、白色、黑色 | -100..100 |
| `texture`, `clarity`, `dehaze` | 紋理、清晰度、去朦朧 | -100..100 |
| `vibrance`, `saturation` | 細節飽和度、飽和度 | -100..100 |
| `tone_curve.{master,red,green,blue}` | 點曲線 | `[[x,y],...]` 0..255 |
| `hsl.{red,orange,yellow,green,aqua,blue,purple,magenta}.{hue,sat,lum}` | HSL/顏色 | -100..100 |
| `color_grade.{shadows,midtones,highlights,global}.{hue,sat,lum}`、`balance`、`blending` | 顏色分級 | hue 0..360、sat 0..100 |
| `bw.enabled`, `bw.mix.{band}` | 黑白＋黑白混合 | -100..100 |
| `sharpen.{amount,radius,masking}` | 銳利化 | 0..150 / 0.5..3 / 0..100 |
| `noise.{luminance,color}` | 雜訊減少 | 0..100 |
| `vignette.{amount,midpoint,feather}` | 裁切後暗角 | -100..100 / 0..100 |
| `grain.{amount,size,roughness}` | 顆粒 | 0..100 |
| `straighten`, `crop` | 拉直、裁切 | 度；`[l,t,r,b]` 0..1 |
| `masks` | 遮罩（局部調整） | 見 SKILL.md「局部遮罩」 |
| `skin_protect` | （本引擎專有）膚色保護 | 0..1 |

## 常見風格的手法（自創風格時參考）
- **褪色／底片感**：曲線黑點抬到 15–30（`[0,20]`），白點壓到 240；加顆粒 10–20。
- **電影感青橙**：陰影分級 hue 180–200、高光 hue 30–45；`hsl.green.hue +40~+60` 把綠推向青、`hsl.blue.hue -20~-30`。
- **暗調秋景**：綠色 hue 往黃移（-30~-50）、飽和 -40；橘色飽和 +；藍色明度降；暗角。
- **日系明亮**：曝光 +、對比 -、陰影大量 +、曲線整體提亮、綠色往青移並降飽和。
- **戲劇黑白**：黑白混合藍色 -40（天空變暗），橘色 +20（膚色亮），clarity +20。
