# Ovarian cancer parser

研究用的 NOTE／PATH／ICD workflow。共用入口為 python -m cancer_pipeline，每個癌別透過 study profile 定義 prompt、output format、分類規則及 ICD 清單。現有 ovarian 指令保留相容入口。

[共用流程與操作方式](docs/reusable_workflow.md) · [癌別定義方式](profiles/README.md) · [Ovarian 設定](profiles/ovarian.json) · [Breast 待定義範例](profiles/breast.example.json)

以下保留既有 ovarian 試跑與執行指令。新癌別使用上面的共用入口與獨立 run directory。

## 現在可以做什麼

- 用固定 NOTE prompt v1.3，對相同樣本比較多個模型。
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

在隱藏輸入框貼上新 key。它只存到本機 `config.local.json`（權限 600、git 忽略），不會寫進程式或顯示在終端。NYU gateway 使用 `AZURE_OPENAI_API_KEY` 環境變數覆蓋本機 `azure_api_key`；不會使用舊的 OpenAI key。

### 確認資料與 endpoint

目前預設照 pan-ca-prog2json 使用 NYU gateway：

- `provider`: `nyu_gateway`
- `base_url`: `https://kong-api.prod1.nyumc.org`；可用 `LLM_API_BASE_URL` 覆蓋 gateway host。
- `models[].id`: CLI 選模型用，例如 `gpt-5.4-mini`。
- `models[].model_version`: gateway 路徑，例如 `gpt-5.4-mini/v1.0.0`。
- 請求網址為 `{gateway}/{model_version}/chat/completions`，傳送 Bearer 與 `api-key` headers，和參考 repo 一致。
- 不需要一般 Azure endpoint 的 `api-version` query。三模型的權限與路徑仍需實測。

可先在你自己的 terminal 設定 `AZURE_OPENAI_API_KEY`，再執行下方 smoke test。該 terminal 的 export 不一定會傳給 Codex process；要讓 Codex 執行可用 `configure_key.py` 隱藏輸入至本機設定。

如需直接 OpenAI，明確改為 `provider: openai`、對應 `base_url`，使用 `OPENAI_API_KEY`／本機 `api_key`。不要把 gateway key 送往 OpenAI endpoint。

費用欄是設定中的參考估價，並非 NYU gateway 帳單或已驗證報價。

參考：[pan-ca-prog2json api_manager.py](https://github.com/nyu-shenlab/pan-ca-prog2json/blob/main/pan_ca_prog2json/gpt/api_manager.py)。

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

### PATH pilot (reference labels pending)

`run_path_pilot.py` uses PATH prompt v1.5 and a separate fixed output template. It does not load reference labels or calculate accuracy. Private `data/path_inputs_20.jsonl` contains exactly the masked report text shown in the manual-review HTML, plus original MRN/accession/collection metadata; no draft answers are sent. Body dates masked in that review input remain unavailable to the model.

```bash
python3 run_path_pilot.py --limit 20
python3 run_path_pilot.py --models gpt-6-luna --limit 20 --execute
```

The default completion cap is 12,000 (PATH outputs contain many fixed-key diagnosis objects). It is a ceiling, not a billed fixed amount. Raw replies, normalized outputs, source-metadata-restored outputs, usage, status and request IDs are saved under ignored `runs/path-<timestamp>/`. Invalid or truncated outputs remain recorded and are not silently retried. Execution stops after authorization/routing/rate-limit failures or three consecutive transport failures. Results are predictions awaiting human reference review, not gold labels. No automatic patient-level PATH processing is implemented yet.

PATH v1.3 groups all findings per organ-level specimen, uses numeric split suffixes, and requires diagnosis_id to match the linked specimen_id. The v1.3 runner flags per-bullet splitting or named ID suffixes without repairing the original response. Earlier v1.2 predictions remain unchanged.

PATH v1.5 clarifies that unknown primary origin does not erase a supported diagnostic-event date. Non-Malignant dates do not contribute to target onset. Output keys are unchanged; prior v1.4 pilot scores remain historical and have not been rerun.

### Local PATH-50 pilot and compressed result layout

`prepare_path50.py` selects 50 source rows without replacement (seed 20261108) from the local original PATH pull and stores a private input snapshot and source hash. It does not load reference labels. `run_path_pilot.py --models gpt-6.1-sol --inputs data/path_inputs_50.jsonl --limit 50 --execute` uses prompt v1.5; existing requests remain capped at 60 total per invocation, with no automatic retries.

After the run completes, export using:

```sh
python3 export_path_batch.py --run runs/<saved-run-directory> --inputs data/path_inputs_50.jsonl
```

Private ignored `results/` contains `note/`, `path/`, `icd/`, `csv/`, and `audit/`. Valid PATH output objects are stored one per line in `path/batch_0001.jsonl.gz`. Raw attempts are retained separately in `audit/path_attempts_0001.jsonl.gz`. Invalid predictions are not silently repaired or counted as negative; they appear in `csv/path_review.csv` and the audit batch. PATH record/patient CSVs are populated; NOTE/ICD and final cohort CSVs are header-only until those modules and the final merge have run. Header-only final files must not be interpreted as a completed empty cohort. `manifest.json` records completeness, status and configured-rate cost estimates.

Only eligible Malignant dates contribute to PATH patient-level positive dates. Historical target diagnoses need not appear as fabricated current specimens. Unknown initial dates and overlapping date precision remain flagged. The future final merge prioritizes eligible NOTE/PATH dates and uses ICD only when neither has an eligible date. An unresolved review flag can still affect final eligibility. Export does not call an API and refuses to overwrite an existing batch.

Pilot data-quality note: two sampled source rows contain the literal MRN sentinel `null`. These are normalized to JSON null, retained in report-level output and `path_review.csv`, and excluded from MRN grouping. They must not be merged into a fictitious patient named `null`. Identifier/timestamp fields otherwise come from authoritative source metadata; original model replies remain in audit.

### Full PATH module (resumable)

`python3 run_path_module.py --workers 8 --execute` runs all local source rows with Sol/PATH v1.5, reusing the exact matching 50-case pilot predictions. Without `--execute` it only prints the plan. The same command resumes from the private append-only, fsynced audit journal without repeating recorded requests. A lock prevents simultaneous module runs; source/prompt/model fingerprints must match. No reference labels are loaded, and no deduplication is performed. Fatal authorization/routing/rate-limit responses or repeated transport failures stop scheduling new requests; active requests finish and results are retained. Failed/invalid records remain review items rather than being silently retried or reclassified.

Before the first full run, the existing pilot export is preserved under ignored `runs/path50-export-<UTC>/`. Full outputs occupy `results/`: nine source-row batches of up to 500 rows, rolling record/patient CSVs, a source-row index, a prompt snapshot and an audit journal. Check `manifest.json` state and `finished_records` before treating CSVs as final PATH-module outputs. They are provisional while running; NOTE/ICD and final cross-source cohorts are not executed by this command. `verify_path_module.py` checks gzip/schema/CSV/journal integrity offline after completion. API costs are configured-rate estimates, not confirmed gateway invoices. Keep the local machine running and its NYU network connection available.

### NOTE strict Structured Outputs pilot

Legacy JSON mode remains the default. `note_v1.4_structured.txt` replaces only the v1.3 output-template section; extraction, classification, date rules and the 56-item reference are unchanged. The strict schema requires every field, forbids extra keys, and constrains category/certainty/status enums. Subtype membership, calendar validity and cross-field consistency are still checked locally.

```sh
python3 run_note_pilot.py --models gpt-6-luna --limit 20 \
  --prompt prompts/note_v1.4_structured.txt \
  --structured-schema schemas/note_structured_schema.json --execute
```

Prompt/schema snapshots, hashes, raw responses and usage are preserved per run. No automatic retries or JSON-mode fallback. From the workspace root, compare with `python3 work/compare_note_structured.py --run ovarian-cancer-parser/runs/RUN_ID`. The comparison uses the same 20 reviewed development cases and the preserved v1.3 Luna baseline, not an independent holdout. Costs use configured reference rates, not gateway invoices; cached and no-cache estimates are separate. Cache hits, output length and model-serving conditions can affect the historical comparison.

### NOTE canary before production

Run `python3 run_note_canary.py` from the repo for offline preflight. It uses the prepared 56-note sample in the private `runs/note-production-preflight-20261110/` directory, Luna with low reasoning, the v1.3 prompt, and JSON mode. The sample exercises the runner and MRN aggregation.

For an approved live canary, add `--execute --budget-usd 0.30`. The command is limited to that sample and the NYU gateway. The budget gate compares the configured reference rates with a no-cache reserve based on request size and completion cap. The reserve is a local estimate. Confirm any actual spending limit and billing against the gateway.

Successful submissions are checkpointed in `canary_luna/ledger.sqlite3`. Re-running with identical settings skips completed submissions. Timeouts, interrupted requests, invalid outputs, and missing usage stop the run. The one automatic schema correction sets subtype qualification to null when the subtype is null and that is the only validation error. The ledger preserves the original response and the field change. Reconcile other unresolved requests before any manual replay. The completed `canary_luna/predictions.jsonl` is published only after every response passes validation. Pass that file to `postprocess_notes.py --input` for deterministic MRN aggregation.

Before full execution, reconcile the canary request count and usage with the gateway, then validate clinical classification and date selection against a separate reviewed sample. Establish the full-run budget and quantify the effect of long notes and rare target cases before expanding the run. Empty source notes require explicit tracking in the production export.

### Full NOTE module

`python3 run_note_module.py` checks the twelve local source files and whether the validated 56-note canary can be reused. To run inference on the approved NYU gateway, use `python3 run_note_module.py --execute --workers 8 --budget-usd 500`. The budget applies to new requests at the configured reference rates. Actual charges come from the gateway.

The private `results/audit/note_manifest.json` reports progress. A source, prompt, or model change prevents resuming the same ledger. A stopped or interrupted request with an unknown outcome is held for reconciliation, and completed rows are skipped on resume. Use SIGTERM on the runner PID for a graceful pause after active requests finish. The runner pauses on gateway errors, invalid usage, a sustained error rate, or the reference budget gate.

Once inference and export finish, the manifest reports completion. Valid per-note JSON is grouped in `results/note/batch_*.jsonl.gz`. `results/csv/note_record_level.csv` retains every source row and its processing status. `results/csv/note_patient_level.csv` contains the MRN-level date, source IDs and review reasons. An Uncertain note only adds a clinical review reason when its extraction contains a relevant unresolved target finding or unresolved primary. Failed and blank source rows retain their MRN and note ID for operational review. Check the NOTE manifest before using the CSVs.

The parallel NOTE run assigns files 00–05 to `--shard local` and 06–11 to `--shard server`, with a US$250 reference-rate limit per shard. Keep the original local ledger when switching to the local shard. Each ledger stores its shard assignment and rejects an incompatible restart. The server uses `--source-dir` for its local copy and `--reference-contract` to verify file hashes and inference settings against the original run. Shard completion leaves results in the ledger. Final NOTE CSVs require combining both ledgers at record level before grouping by MRN.

NOTE scheduling now refills each completed request immediately while keeping at most eight outstanding requests per host. Graceful shutdown stops new submissions and saves all outstanding results before exiting. The budget gate includes reservations for requests still running.

For HTTP 429, the affected note is held without replay. New submissions wait for the gateway's numeric Retry-After interval, with a 60-second fallback and a 15-minute maximum, before continuing. Original HTTP status and the reference cost reserve remain in the ledger.
