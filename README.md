# CFPB credit card agreement pipeline (Pipeline 1: PDF to Databases)

Turns the credit card agreement PDFs that issuers submit to the CFPB into checked, quotable field
values: one JSON file per card, the same values in Postgres, and the agreement text as vectors in
Qdrant. Everything runs locally with open-source models (GLM-OCR, Qwen3.5-4B, BGE-M3).

```
Source PDFs -> OCR -> Extraction LLM -> Validation Gate -> Postgres -> Embeddings -> Qdrant
                                            |  fail                                    |
                                       Repair Agent -> Human Review       Integrity Check -> publish
Alongside every step: tracing (every model call logged), offline evals + release gate, data lineage
```

---

## 1. Which files you run

You only ever run four files. Everything else is used by them.

| Order | File | When | What it does |
|---|---|---|---|
| 1 | `setup.ps1` | Once per computer (and after `git pull` if packages changed) | Installs Git, Python 3.12, Ollama and Docker Desktop if missing; checks the NVIDIA driver; creates the Python environment with PyTorch for CUDA and all libraries; downloads the models; writes `config.yaml` / `config.pilot.yaml`; starts Postgres + Qdrant; checks everything |
| 2 | `start.ps1` | Every time you open a new PowerShell window | Starts Docker (Postgres, Qdrant) and Ollama, and activates the Python environment. Run it as `. .\start.ps1` (dot, space, path) |
| 3 | `next_steps.ps1` | To run the pipeline | Runs the pipeline stages in order for a whole stage of work (pilot, full quarter, answer-key templates, labelling, publish) and stops at the first failure |
| 4 | `run.py` | To run one stage or tool by hand | The pipeline's command line: every stage and tool is a `python run.py <command>` |

`check_setup.py` is the fifth, optional one: run `python check_setup.py --models` any time to check
the installation (setup.ps1 runs it for you).

### First time on a new computer

```
git clone https://github.com/itsabhishekm/Extraction-pipeline.git
cd Extraction-pipeline
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -InputRoot "D:\2026_Q1"
```
- Copy the PDFs first (they are not in git): the `2026_Q1` folder (one subfolder per issuer) and
  the 19 pilot PDFs in `2026_Q1\_pipeline_sample\pilot` (or pass `-PilotRoot <folder>`).
- `-DataRoot E:\cc_pipeline` chooses where models, the Python environment and all output go
  (default `D:\cc_pipeline`, or `C:\cc_pipeline` without a D: drive). About 40 GB free is needed.
- Installing Docker Desktop asks for administrator approval and needs one restart: restart and run
  the same command again; finished steps are skipped.

### Which banks are processed

The full-quarter run only processes the issuer folders listed in **`issuers/large_banks.csv`**
(82 folders = 63 large banks, about 1,379 PDFs); every other folder under `input_root` is skipped.
- To change the selection, edit that CSV (column `folder_name` must match the folder name; case,
  extra spaces and a trailing dot don't matter). Column `entity` groups folders of one bank.
- To process every folder, remove the `issuers_file:` line from `config.yaml`.
- The scan log says how many folders were selected and warns about listed banks with no folder.
- If the database already holds banks from an earlier run without the list, start clean once:
  `.\next_steps.ps1 -Stage full -ResetDb`.
- The pilot (`config.pilot.yaml`) is not filtered: it always uses its own 19 test PDFs.

### Every working session

```
cd Extraction-pipeline
. .\start.ps1
powershell -ExecutionPolicy Bypass -File .\next_steps.ps1                    # pilot (19 PDFs)
```

### Running a quarter, start to finish

| Step | Command | Result |
|---|---|---|
| Pilot | `.\next_steps.ps1` | 19 pilot PDFs through every stage; review CSV written |
| Review the pilot | `python run.py review --config config.pilot.yaml` | decide the values the Repair Agent could not fix |
| Full quarter | `.\next_steps.ps1 -Stage full` | all PDFs through every stage |
| Review | `python run.py review` | until nothing is left |
| Answer key | `.\next_steps.ps1 -Stage templates` | 150 pre-filled answer-key files in `evals\answer_key\` |
| Label | `.\next_steps.ps1 -Stage label` | you check the 150 cards field by field (`q` saves, run again to continue) |
| Publish | `.\next_steps.ps1 -Stage publish` | eval release gate + integrity check; if both pass, a release folder |

(`.\next_steps.ps1` is short for `powershell -ExecutionPolicy Bypass -File .\next_steps.ps1`.)

`next_steps.ps1` options: `-ResetDb` recreates Postgres + Qdrant (deletes pipeline data; needed
after a schema change), `-Clean` also deletes everything the pipeline wrote under the data folder
(asks you to type YES), `-Reviewer <name>`, `-Count <n>` (answer-key cards).

### `run.py` commands

Add `--config config.pilot.yaml` to any command to work on the pilot instead of the full quarter.
Every stage only picks up work not done yet, so re-running is safe; stop any time with Ctrl+C.

| Command | Architecture box | What it does |
|---|---|---|
| `python run.py scan` | Source PDFs | Finds every PDF, hashes it (sha256 = file fingerprint), records it; exact duplicates are processed once |
| `python run.py text` | OCR (text pages) | PyMuPDF reads each PDF into one `.md` file with page markers; decides which pages need OCR; records metadata, document type, approach tag. `--retry` retries failed PDFs |
| `python run.py ocr` | OCR (scanned pages) | GLM-OCR reads the pages that have no usable text |
| `python run.py group` | | Decides which PDFs form each card version (same issuer + submission ID) |
| `python run.py extract` | Extraction LLM | Qwen fills the 31 fields + regulations; splits multi-card PDFs into one version per product. `--version N --redo` redoes one version |
| `python run.py validate` | Validation Gate | Schema, quote, number, range and cross-field checks, model check, double read, conflicts; failures go to the retry queue. `--redo` validates again |
| `python run.py retry` | Repair Agent | Re-asks only the failed values (thinking on, more text, page images for pricing); after 2 failed retries -> human review |
| `python run.py review` | Human Review | Approve / correct / reject the values left over. `--export f.csv` / `--import f.csv` to do it in Excel |
| `python run.py embed` | Embeddings -> Qdrant | BGE-M3 dense + sparse vectors of every page chunk. `--redo` embeds again |
| `python run.py search "question"` | | Hybrid search over the agreements. `--issuer`, `--card`, `--top` |
| `python run.py report` | | Bank inventory files + quarter summary (audit-v6 outputs 1-4) |
| `python run.py integrity` | Integrity Check | Counts match (files, pages, fields, JSON, Qdrant points) + test searches pass |
| `python run.py eval` | Offline Evals | Scores against the hand-checked answer key; release gate. `--make-template --count 150`, `--make-examples` |
| `python run.py label` | | Hand-check answer-key files field by field (opens the PDFs, checks every edit) |
| `python run.py publish` | publish | report + integrity + eval gate; only if both pass, copies a release with a manifest |
| `python run.py status` | | Where every PDF and card version is; validation counts; model calls, hours and tokens |
| `python run.py all` | | scan .. retry, embed, report, status |

---

## 2. What each file does

### Files in the main folder

| File | What it is |
|---|---|
| `setup.ps1` | Installer and configurator for a new computer (see section 1) |
| `start.ps1` | Starts Docker + Ollama and activates the Python environment (dot-source it) |
| `next_steps.ps1` | Runs the pipeline stages in order for pilot / full / templates / label / publish |
| `run.py` | Command line for every stage and tool (see the table above) |
| `check_setup.py` | Checks Python, packages, GPU, folders, Postgres, Qdrant, Ollama models and BGE-M3; `--models` also runs each model once |
| `config.example.yaml` | Settings template for the full quarter (in git). `setup.ps1` copies it to `config.yaml` with this computer's paths |
| `config.pilot.example.yaml` | Same, for the 19 pilot PDFs -> `config.pilot.yaml` |
| `config.yaml`, `config.pilot.yaml` | This computer's settings: PDF folder, data folder, database, models, thresholds (written by setup, not in git) |
| `fields.yaml` | **What the model looks for**: the 31 fields in 4 groups (identity, pricing, late-fee rules, legal), each with its question, rules, normalized format and the keywords that pick the pages; plus the regulations question. Changing it changes the prompt version |
| `issuers/large_banks.csv` | The banks the full-quarter run processes (folder name, PDF count, bank entity); set by `issuers_file` in the config |
| `examples.yaml` | Worked examples added to the prompts (built by `run.py eval --make-examples`; empty at first) |
| `search_tests.yaml` | Test searches the integrity check must pass |
| `requirements.txt` | Python libraries (installed by setup) |
| `docker-compose.yml` | Postgres 16 + Qdrant containers |
| `.gitignore`, `.gitattributes` | Keep machine-specific config, caches and data out of git; line endings |
| `README.md` | This file |

### `pipeline/` - the stage code

| File | Stage / role | What it does |
|---|---|---|
| `stage_scan.py` | scan | Walks the issuer folders, hashes every PDF, records issuers, documents and files; spots duplicates |
| `stage_text.py` | text | Checks each page (text vs scanned vs garbled), writes the `.md` file with page markers, reads metadata, sets document type and approach tag |
| `stage_ocr.py` | ocr | Renders the scanned pages and sends them to GLM-OCR; fills the text into the `.md` files |
| `stage_group.py` | group | Groups PDFs into card versions; merges identical content |
| `stage_extract.py` | extract | Builds the prompts from `fields.yaml`, picks the pages per field group, calls Qwen, lists products and splits multi-card PDFs, saves values, works out version dates and comparison eligibility |
| `stage_validate.py` | validate + retry | The Validation Gate (all checks) and the Repair Agent; writes the retry queue and human-review items |
| `review.py` | review | Human review in the terminal or through a CSV |
| `embed.py` | embed + search | Chunks the page text, BGE-M3 vectors, Qdrant collection, hybrid search |
| `report.py` | report | Bank inventory JSON files and the quarter summary (field availability, per-PDF approach, findings) |
| `integrity.py` | integrity | The Integrity Check and the per-bank self-check |
| `evals.py` | eval | Answer-key templates, scoring, release gate, worked examples |
| `label.py` | label | Field-by-field labelling tool for the answer key |
| `publish.py` | publish | Runs report + integrity + eval gate and copies a release |
| `status.py` | status | Progress, counts, failures, model usage |
| `llm.py` | shared | Every call to Ollama (OCR, extraction, checks): output caps, retry with thinking off, tracing to `traces\` and optional LangSmith |
| `pages.py` | shared | Reads `.md` files back into pages, picks the pages a field group needs (anchors + keywords), normalizes text for quote matching |
| `postprocess.py` | shared | Applies the audit-v6 status rules to each answer, display labels, date parsing, comparison eligibility |
| `json_out.py` | shared | Writes the card JSON files (audit-v6 format + validation, lineage, conflicts) |
| `docinfo.py` | shared | Document type, approach tag, copy of page text into Postgres |
| `db.py` | shared | Postgres connection, run records, code version (lineage) |
| `config.py` | shared | Loads the config file, logging |
| `naming.py` | shared | File-name rules (bank / card slugs, submission IDs) |

### Other folders

| Folder / file | What it is |
|---|---|
| `db/init/01_schema.sql` | The Postgres schema: 14 tables (issuers, documents, card versions, field values, conflicts, review items, regulations, ...) and 4 views. Postgres runs it on first start |
| `db/init/02_seed_field_definitions.sql` | The 31 field definitions |
| `db/init/03_seed_regulations.sql` | The 12 regulations (TILA, CARD Act, MLA, ...) with search terms |
| `evals/answer_key/` | Hand-checked cards for the offline evals (one JSON per card version; see its README) |
| `tests/test_gate.py` | Checks of the validation rules, chunking, eval scoring and the model-call fallback: `python -m tests.test_gate` |
| `tests/fake_llm.py` | Stand-in for Ollama (`CC_FAKE_LLM=1`) to test the stages without a GPU |

### What the pipeline writes (under the data folder, e.g. `D:\cc_pipeline`)

| Path | What |
|---|---|
| `venv\` | The Python environment |
| `models\ollama`, `models\bge-m3` | The models |
| `text\<issuer>\<file>.md` | Page text, one marker per page (`<!-- page 3 -->`, `<!-- page 4: ocr -->`) |
| `json\<bank>\<bank>__<card>.json` | One file per card, all versions, every field with value, quote, PDF, page, status, validation, lineage |
| `json\<bank>\<bank>__inventory.json` | One per bank: PDFs, metadata, approach tags, coverage, eligibility, cardholder note, self-check |
| `reports\<quarter>_summary.md` | Field availability table, per-PDF approach table, findings |
| `retry_queue.jsonl` | Values waiting for the Repair Agent |
| `traces\<date>.jsonl` | Every model call: prompt, answer, tokens, seconds |
| `logs\<date>_<stage>.log` | One log per stage per day (scan, text, ocr, group, extract, validate, retry, review, embed, report, integrity, eval, publish) |
| `logs\<date>_pipeline.log` | Every stage in one timeline |
| `logs\setup_*.log`, `logs\next_steps_*.log` | Setup and next_steps transcripts |
| `evals\`, `integrity\`, `releases\` | Eval reports, integrity results, published releases |

### Reading the logs

Every line says which stage wrote it and where that stage is in its job:

```
2026-10-26 22:20:22  VALIDATE  INFO    [ 5/17  29%  eta 3m | v5] minimum_interest_charge -> retry_queued: quote not found
   time               stage     level   item 5 of 17, 29% done, about 3 minutes left, working on card version 5
```

- **Progress** `[i/N pct eta | item]`: the item is the bank (scan, text, embed, report), the page (ocr) or the
  card version (extract, validate, retry; with the product name for multi-card PDFs).
- **Colours** in the console, one per stage: SCAN cyan, TEXT blue, OCR purple, GROUP light cyan, EXTRACT green,
  VALIDATE yellow, RETRY orange-red, REVIEW pink, EMBED light blue, REPORT grey, INTEGRITY light green, EVAL / LABEL /
  PUBLISH bold. Warnings are yellow and errors red, whatever the stage.
- Log files have the same columns without colours. Turn colours off with `logging: {color: never}` in
  `config.yaml` or the `NO_COLOR` environment variable.
- Find problems quickly: `Select-String -Path D:\cc_pipeline\logs\*_pipeline.log -Pattern "ERROR|WARNING"`.

In Postgres (schema `cc`) the same values are in `field_values`, `card_versions`, `cards`, `regulation_mentions`,
`conflicts`, `review_items`, with views `v_card_latest`, `v_field_coverage`, `v_field_changes`, `v_card_regulations`.
Qdrant holds the collection `cc_agreements`.

---

## 3. How it works (reference)

### Validation Gate (what each check does)

Per value (status found / stated_none):
- **Schema check:** `normalized_value` must match its format in `fields.yaml` (jsonschema).
- **Quote grounding:** the quote must be on the cited page of the cited PDF (after removing markdown; near-exact match allowed, looser on OCR pages). Found on another page of the same PDF -> page corrected.
- **Number check:** every number in the value must be in the quote (pricing and late-fee fields).
- **Business rules - plausible ranges:** e.g. APR 0-40, late fee $0-50, payment due 21-60 days (`DEFAULT_RANGES` in `pipeline/stage_validate.py`, override in `config.yaml` `validate.ranges`).
- **Model check:** Qwen (thinking on) confirms the quote supports the value.

Per card version:
- **Cross-field rules:** no arbitration clause -> opt-out set to not_applicable; a jury waiver "within_arbitration" or a military exception without an arbitration clause -> retry.
- **Quality gates (audit-v6):** purchase APR missing although the PDF has an "Interest Rates and Interest Charges" table -> retry; arbitration found but opt-out not_present -> retry; daily periodic rate x 365 matching no APR (+/-0.01) -> flag; multi-card document where not every product has a purchase APR -> flag.
- **Double read:** a second, independent read of the pricing fields, one call per PDF. Disagrees with the first read -> retry. Two PDFs of the same version give different values -> a `conflicts` row and human review (never decided automatically).

### Human review

```
python run.py review                         # one item at a time in the terminal
python run.py review --export review.csv     # all open items to a CSV
python run.py review --import review.csv     # apply the decision column (approve / correct / reject)
```
For `correct`, fill `new_status`, `new_raw_value`, `new_normalized_value` (JSON), `new_quote`, `new_source_pdf`, `new_page`. Decisions are stored in `review_items` (who, when, what) and the card JSON is rewritten.

### Offline evals and the release gate

1. `python run.py eval --make-template --count 150` writes 150 pre-filled answer-key files to `evals\answer_key\` (spread over issuers).
2. `python run.py label --reviewer <name>`: for each file it opens the PDFs and shows each field with the pipeline's answer and quote. Enter = correct, `e` = edit (checked against the field's format), `n` = not_present, `b` = back, `q` = save and stop (it continues where you left off). A finished file gets `"checked": true` and your name.
3. `python run.py eval` scores the pipeline (a field is right when the status and the normalized value match) and writes `evals\eval_<quarter>_<time>.md` under `data_root`.
4. The release gate passes only if: at least `min_cards` checked cards, overall accuracy >= `min_overall` (0.90), every field >= `min_field` (0.80), and no field dropped more than `max_drop` (0.02) since the last passing eval. Thresholds in `config.yaml` `eval`.

Worked examples: label 2-4 extra cards of different layouts, set `"used_as_example": true` (these are never scored), then `python run.py eval --make-examples` writes `examples.yaml`. Re-extract (`extract --redo`) for the examples to take effect.

### Tracing, lineage

- **Tracing:** every model call (OCR, extraction, check, double read, retry) is one line in `traces\<date>.jsonl` under `data_root`: stage, run id, version, field group, model, seconds, prompt and output tokens, and the full prompt and answer. `status` adds up calls, hours and tokens per stage. To also send calls to LangSmith: set `tracing.langsmith: true` in `config.yaml` and the environment variable `LANGSMITH_API_KEY` (this uploads the prompts and answers to LangSmith's cloud).
- **Lineage:** every value keeps the sha256 of its PDF, the page, and the run that produced it (`field_values.run_id` -> `pipeline_runs`: models, prompt version, code version = hash of the code + `fields.yaml` + `examples.yaml`). The card JSON shows this under each field's `lineage`.

### How it decides things

- **OCR pages:** a page goes to OCR when its text layer is garbled, or it has little text (under 200 characters or 30 words) AND images cover at least 20% of it or its letters are drawn as at least 500 vector shapes ("Print to PDF" files). Thresholds in `config.yaml`.
- **What to extract:** `fields.yaml`. Each field group gets only the pages that match its keywords, so a call fits in 6 GB. Thinking is on for the late-fee-grace and legal groups, and for every check, double read and retry.
- **Multi-card PDFs:** the model first lists the products a version's PDFs cover (rate tiers of one product = one product; secured and unsecured = different products). More than one -> one card version per product, all sharing the same PDFs, each extracted for its product only.
- **Version date:** effective date printed in the PDF, else a date in a file name, else the PDF creation date. Submission IDs never order versions.
- **Statuses and display labels:** audit-v6 rules, applied in code (`pipeline/postprocess.py`). "Not mentioned" is never stored as "none".

### Testing without a GPU

`tests/fake_llm.py` stands in for Ollama (`CC_FAKE_LLM=1`) and `CC_FAKE_EMBED=1` replaces BGE-M3 with hashing vectors. They are not models: they test that the stages work together, not the quality of the answers. `python -m tests.test_gate` checks the validation rules, chunking and eval scoring.
