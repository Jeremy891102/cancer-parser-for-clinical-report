# Ovarian cancer parser

研究用的 PATH／progress NOTE／ICD pipeline。**目前已實作 NOTE 的本機模型比較與離線 patient-level 後處理**；PATH、ICD 與 patient-level 合併尚未實作。程式不做臨床診斷。

## 現在可以做什麼

- 用固定 NOTE prompt v1.2，對相同樣本比較多個模型。
- 預設比較 GPT-5.4-mini、GPT-6 Luna、GPT-6.1 Sol。
- 保留每次回應、格式檢查、分類／日期／subtype／origin／時間狀態比對、token、耗時與參考費用。
- 預設 **dry run**：不連網、不呼叫模型、不需要金鑰。
- Python 3.9+，只用標準函式庫，不需要安裝 SDK。

## 本機使用

在這台電腦的 repo 中，20 份原始 NOTE 與人工確認答案已放在 `data/`，**這些私有檔案不會上傳 GitHub**。從 GitHub clone 的版本須自行放入已核准資料。

```bash
cd /Users/jeremy/Documents/Codex/2026-10-15/for-ovarian-cancer-data-pull-workspace/ovarian-cancer-parser
python3 -m unittest discover -s tests -v
python3 run_note_pilot.py --limit 20
```

最後一行只檢查本機資料與預計請求數，沒有 API 費用。

### 不用 API：先跑通 patient-level pipeline

```bash
python3 postprocess_notes.py
```

預設讀取本機 `data/note_labels_20.jsonl`（你已 review 的 parsing outputs）。不需要 key、不讀 key、不連網。可用 `--input` 指定其他同格式 JSONL，或 `--output-dir` 指定新的輸出資料夾；既有資料夾不會覆寫。

輸出至被 git 忽略的 `runs/note-postprocess-<timestamp>/`：

- `note_patient_level.csv`：所有可連結 MRN，欄位為 MRN、progress_note_positive_date、in_progress_note、progress_note_source_id。
- `note_target_positive.csv`：有可用 target malignant 日期的病人。
- `note_review.csv`：日期未知、原發／分類未定、格式衝突等問題與來源 ID；不是最終 cohort exclusion 判定。
- `note_non_target.csv`：目前未建立 target malignancy 的其餘病人；不等於合格 negative controls。
- `validation_and_patient_audit.json`、`summary.json`：完整狀態與來源追溯。

CSV 的 source ID 欄以 JSON array 保存多個同日來源，避免漏掉支持證據。MRN 保留為字串；若用 Excel 開啟請按文字欄匯入，避免前導零被改掉。

程式先驗證格式，再按 MRN 合併。只使用 Malignant 且診斷支持已確認 target origin 的日期；NED 或後續良性結果不取消既往癌症。不同精度日期若重疊且無法自動比較，列入 review，不補 1 月 1 日。相同 NOTE ID 的互相矛盾版本不會默默選一筆。

20 份真實 review 結果來自20位不同病人，只驗證格式及分類篩選；多 NOTE 同病人、NED、日期精度與 ID 衝突另外用合成資料測試。這次不是模型預測或準確率實驗。PATH／ICD／三來源合併尚未接入，不能把此輸出當作最終 onset CSV。

### 設定金鑰

先撤銷曾貼到聊天的金鑰，產生新的。執行：

```bash
python3 configure_key.py
```

在隱藏輸入框貼上新 key。它只存到本機 `config.local.json`（權限 600、git 忽略），不會寫進程式或顯示在終端。也可用 `OPENAI_API_KEY` 環境變數覆蓋。

### 確認資料與 endpoint

`config.local.json` 預設使用直接 OpenAI API；機構 gateway 的 URL 與模型名稱可能不同。

**目前本機原始 NOTE 含病人資訊。只有資料已去識別且允許送往該服務，或該 endpoint 已獲機構核准處理這份資料，才能執行。** 會員／API credits 不代表病歷外送授權。

將核准的資料指定為 `--inputs`／`--labels`，並在本機設定 `data_approved_for_endpoint: true`。這是操作者的資料使用確認，不是程式自動判定已去識別。

### 先測 1 份 × 1 個模型

```bash
python3 run_note_pilot.py --models gpt-5.4-mini --limit 1 --execute
```

確認結果後，再測 20 份 × 3 個模型（60 次請求）：

```bash
python3 run_note_pilot.py --limit 20 --execute
```

只有 `--execute` 會送出請求。程式不自動重試；401／403 會停止整批。重跑會建立新資料夾並重新計費，不是 resume。輸出上限包含 reasoning tokens，若 `truncated`，需調整設定後另跑並記錄費用。

## 模型與金鑰設定

`config.example.json` 是無金鑰範本。

- 直接 OpenAI API 使用原生名稱，例如 `gpt-5.4-mini`。
- Langone gateway 的 `/v1.0.0` 等名稱，須依該服務的設定替換；不能直接拿 gateway 名稱呼叫 OpenAI。
- 不支援的模型／帳號權限會被記錄為錯誤，不會偷偷換其他模型。
- 三個候選預設 `reasoning_effort: low`；實際設定會寫入 run manifest。
- 設定內的價格是 2026-11-04 查到的 OpenAI Standard **參考估價**，不代表 Langone 帳單。費用估算不含額外 cache-write、區域、工具或其他服務費。
- 額度標稱 $500 不代表已驗證硬上限；本程式不是帳單系統，也不保證供應商支出封頂。

## 輸出

每次執行建立私有的 `runs/<UTC timestamp>/`：

- `manifest.json`：prompt／input／label hashes、case IDs、endpoint、模型參數與參考價格，沒有金鑰。
- `results.jsonl`：原始回應、解析輸出、格式錯誤、逐筆比對、usage、request ID 和耗時。
- `summary.json`：各模型的成功率、欄位 exact match、target precision／recall、token 和參考費用。

解析失敗、截斷或無效 schema 都算失敗，不能只報成功案例的準確率。Reasoning tokens 已包含於 completion tokens，費用不重複相加；保留完整 usage 供核對。無法取得的費用不假裝是零，summary 的 `cost_complete` 會標示。

## 評估範圍

20 份是經使用者 review 的刻意難例，已參與 prompt 討論，屬 **development/challenge set**，不是盲測 holdout，也不是獨立醫師驗證。

- 五分類與日期採 exact match，部分日期精度不同也會標成不同，供人工檢查。
- Subtype／origin／certainty／temporal status 比較 multiset，不受診斷順序影響；另外比較它們的聯合組合，避免欄位對錯病灶。
- 原文截句不適合直接逐字算準確率，需人工 review；此程式不會以另一個 LLM 當裁判。
- MRN／NOTE_ID／簽署時間由程式帶入，不作為模型臨床能力評分。
- `Malignant` 是 ever-target；NED 不刪除以前確診。只有 target Malignant 的日期可進入最終 target onset。
- 後續需另備 patient-disjoint 隨機樣本，才可評估整體準確率與全量成本。

## 私有資料格式

Input JSONL，每行：

```json
{"case_id":"EXAMPLE001","input":{"metadata":{"pat_mrn_id":null,"note_id":null,"note_signed_time":null},"full_text":"An approved clinical note goes here."}}
```

Label JSONL，每行：

```json
{"case_id":"EXAMPLE001","output":{"mrn":null,"note_id":null,"note_signed_time":null,"diagnosis_date":null,"family_history_of_ovarian_cancer_documented":false,"diagnoses":[],"derived_classification":"Uncertain"}}
```

答案絕不加入模型輸入。不要把 review HTML、人工標註或 review notes 當作 inference input。

## 來源

- [Breast parser](https://github.com/nyu-shenlab/breast-pathology-parser)：參考固定欄位與 subtype 定義方式；不沿用忽略病史的規則。
- [Pan-cancer NOTE parser](https://github.com/nyu-shenlab/pan-ca-prog2json)：參考 current-or-past 和初始日期擷取；保留本研究格式及分類。
- [OpenAI Chat Completions](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)
- [JSON output](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat)
- [Pricing](https://developers.openai.com/api/docs/pricing)

沒有複製其他 repo 的執行程式。`data/`、`runs/`、本機設定與憑證均被 git 忽略。

## Troubleshooting

`data_approved_for_endpoint is false` means the local preflight stopped before sending a request. It does not mean the API key was rejected. Check that the configured endpoint is permitted to process the selected input data. If confirmed, edit only `data_approved_for_endpoint` to `true` in the ignored `config.local.json`; do not paste the key or full config into an issue/chat. Otherwise use approved de-identified inputs or the institution-approved endpoint. This flag records operator confirmation; it does not de-identify data.
