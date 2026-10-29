[English](RUNBOOK.md) | [简体中文](RUNBOOK.zh-CN.md)

# InsightChat Runbook

This runbook covers installing, initializing, running, testing, and troubleshooting InsightChat on a local machine. Run every command from the repository root. The commands use POSIX shell syntax; on Windows, Git Bash runs them unchanged, and PowerShell alternatives are noted where the syntax differs.

For the architecture behind these steps, see [IMPLEMENTATION_SPEC.md](IMPLEMENTATION_SPEC.md). For the project overview and user-facing features, see [../README.md](../README.md).

## 1. Prerequisites

- Python 3.10 or newer. Python 3.11 is recommended; the package declares `requires-python = ">=3.10"`.
- A DeepSeek API key and outbound HTTPS access to the configured API endpoint.
- Optional: conda or `venv` for an isolated environment.
- No external database server, Redis, message queue, or vector store. The metadata database and the demo business database are local SQLite files created by the scripts below.

## 2. Create the Python environment

With conda:

```bash
conda create -n insightchat python=3.11 -y
conda activate insightchat
python --version
```

If `conda activate` fails in your shell, try `source activate insightchat` instead, or initialize the shell once with `conda init bash` and reopen the terminal.

With `venv` on Linux or macOS:

```bash
python -m venv .venv
. .venv/bin/activate
```

On Windows Git Bash, use `source .venv/Scripts/activate`. On Windows PowerShell, the activation script is `.venv\Scripts\Activate.ps1`.

`python --version` should report the interpreter you just created. Reopen the shell and activate the environment again in every new terminal.

## 3. Install dependencies

```bash
pip install -e .
```

`pip install -e .` is the recommended option: `chatbi` becomes importable from any directory and edits under `chatbi/` take effect without reinstalling.

If you prefer pinned, known-good versions, install from the lock-style list instead:

```bash
pip install -r requirements.txt
```

Either command installs FastAPI, uvicorn, the `openai` SDK, sqlglot, jieba, pydantic, python-dotenv, sse-starlette, PyYAML, httpx, and pytest.

## 4. Configure `.env`

Create the local configuration file from the template:

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
notepad .env
```

Then edit `.env` and set at least a real `DEEPSEEK_API_KEY`. The application reads only the `.env` at the repository root, regardless of the current working directory.

| Key | Default | Purpose |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | none (required) | DeepSeek API key. Keep it only in `.env`. |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible API base URL. |
| `LLM_MODEL_HEAVY` | `deepseek-v4-pro` | Model for SemQL generation and insight narratives. |
| `LLM_MODEL_LIGHT` | `deepseek-flash` | Model for rewrite, intent, linking rerank, and summaries. |
| `LLM_TIMEOUT_SEC` | `60` | Per-call timeout in seconds. |
| `META_DB_PATH` | `./data/meta.db` | Metadata, semantic layer, history, feedback, and memory database. |
| `BIZ_DB_PATH` | `./test_data/data/retail.db` | Demo business database, opened read-only. |
| `AUTH_SECRET` | `change-me` (code default) | HMAC key for login tokens. Replace it outside local demos. |
| `TZ_NAME` | `Asia/Shanghai` | Time zone used to resolve relative time expressions. |
| `INSIGHT_SIGNIFICANT_PCT` | `5` | Percentage-change threshold for a significant fluctuation. |
| `INSIGHT_SIGMA_K` | `2` | Standard-deviation threshold for a significant fluctuation. |
| `MEMORY_SESSION_TTL_HOURS` | `72` | Session-context time to live. |
| `MEMORY_CONTEXT_CACHE_SIZE` | `128` | In-process session-context cache size. |
| `MEMORY_TERM_PROMOTE_SESSIONS` | `3` | Distinct sessions required to promote a term choice. |
| `MEMORY_MIN_CONFIDENCE` | `0.35` | Effective-confidence floor for using a long-term memory. |
| `MEMORY_EXPLICIT_HALF_LIFE_DAYS` | `365` | Confidence half-life for explicit preferences. |
| `MEMORY_INFERRED_HALF_LIFE_DAYS` | `90` | Confidence half-life for inferred preferences. |

Notes:

- The two model names in `.env.example` are examples. Set `LLM_MODEL_HEAVY` and `LLM_MODEL_LIGHT` to model IDs actually enabled for your DeepSeek account; a wrong name fails at the first model call.
- Relative database paths are resolved against the repository root, not the current directory.
- Never commit `.env`, print the key, or paste it into logs. `.env` is git-ignored.

## 5. Verify model connectivity

```bash
python scripts/check_llm.py
```

This makes one real API call to the light model (`1+1=?`), prints the answer, and confirms that `llm_call_log` was written. It creates missing metadata tables first, so it can run before `scripts/init_db.py`. A 401 means the key is wrong; a 404 or model-not-found error means the configured model name is not available to the account; a timeout usually means the endpoint is unreachable from this machine.

## 6. Initialize the data (in this order)

All scripts import `chatbi.config`, which requires a non-empty `DEEPSEEK_API_KEY`; complete section 4 first even though data generation itself does not call the model.

### 6.1 Generate the demo business database

```bash
python scripts/gen_retail_data.py
```

> **Destructive:** this script deletes and recreates the SQLite file at `BIZ_DB_PATH` (default `test_data/data/retail.db`). Never run it against a database whose contents must be preserved.

Generation is deterministic (fixed seed 42) and produces 32 stores, 200 customers, about 9.1k orders, and about 18.3k order lines covering 2023-06-01 through 2024-06-30. The script prints a month-by-region sales-volume pivot and a 2024-04/2024-05 East China product-line breakdown so you can eyeball the built-in data story.

### 6.2 Build the metadata database and publish the semantic layer

```bash
python scripts/init_db.py
```

The script exits with a hint if the business database is missing. Otherwise it:

1. creates any missing metadata tables;
2. imports the foundational seed data idempotently and publishes semantic snapshot v1 only when no published version exists;
3. executes every Evidence SQL statement against the business database and stores the result; if any statement fails, the script exits non-zero;
4. rebuilds the jieba dictionary under `data/dict/`.

It is safe to re-run: foundational rows are inserted when absent and existing rows are left unchanged, the published v1 snapshot is kept, Evidence results are refreshed, and the dictionary is rebuilt. If you regenerate `retail.db`, re-run `init_db.py` so the stored Evidence results match the new data.

Application startup does **not** seed. Starting the server on an empty metadata database only creates missing tables and loads the dictionary; users, roles, policies, and the semantic snapshot come from `scripts/init_db.py`.

### 6.3 Full reset (optional and destructive)

To start from a clean slate, stop the server first, then delete both generated databases:

```bash
rm -f data/meta.db test_data/data/retail.db
```

On Windows PowerShell:

```powershell
Remove-Item data/meta.db, test_data/data/retail.db
```

These are the default paths; if you changed `META_DB_PATH` or `BIZ_DB_PATH`, delete the files those variables point to instead.

Then repeat steps 6.1 and 6.2.

> **Destructive:** this removes chat history, answers, query logs, insight reports, feedback, long-term memories, audit logs, the semantic snapshot, and the demo business data. Keep a copy first if any of it matters, and do not use this on a machine where `META_DB_PATH` or `BIZ_DB_PATH` points at a real database.

## 7. Start the server

```bash
python scripts/run_server.py
```

The service listens on `http://127.0.0.1:8000`; open that URL in a browser. The terminal stays attached to the server, and `Ctrl+C` stops it.

If port 8000 is already in use, run uvicorn directly on another port:

```bash
python -m uvicorn chatbi.api.main:app --host 127.0.0.1 --port 8001
```

The frontend calls the API through the relative path `/api/v1`, so it works unchanged on any port.

Startup creates missing metadata tables and hot-loads the jieba dictionary. It does not create users or seed semantic objects, so a fresh metadata database still needs `scripts/init_db.py` before login or data questions succeed.

## 8. Sign in

Open `http://127.0.0.1:8000` and use one of the demo accounts. All four share the demo password `Init@123456`; the login page is prefilled with `analyst`.

| Username | Roles | Data scope |
| --- | --- | --- |
| `admin` | `admin` | All data; admin and semantic browsing APIs. |
| `analyst` | `analyst` | All data; normal chat, insight, feedback, and memory. |
| `east_mgr` | `analyst`, `region_east` | All sales queries are filtered to East China (`dim_region = HD`) by a row policy. |
| `viewer1` | `viewer` | Demo read-only account. |

The `viewer1` caveat is important: `POST /chat/ask` returns `403` for a user who has the `viewer` role and neither `analyst` nor `admin`, so `viewer1` cannot ask questions in the chat UI. The account can still read its own history and memories and can call the synchronous `POST /insight/attribute` endpoint directly. Use `analyst` or `east_mgr` for chat demos.

These accounts and passwords are demo data only. Change `AUTH_SECRET` and do not expose the demo login outside a local environment.

## 9. Demo walkthrough

Use explicit dates from the demo period. The static dataset covers 2023-06-01 through 2024-06-30.

| Question (type or paste into the chat) | Expected behavior |
| --- | --- |
| `2024年4月各大区的净销售额` | Four regional rows in a bar chart; applied rules include the completed-only and exclude-test-channel rules. |
| `那华北呢` | Follow-up that inherits the period and metric and switches the region filter to North China (`HB`). |
| `2024年4月华东区各产品线的销量` | Three product-line rows for East China. |
| `2024年3月到5月每月净销售额` | Monthly line chart for March through May 2024. |
| `2024年4月销量最高的前5个门店` | Top-5 stores in descending order. |
| `为什么2024年4月销量比5月低` | Attribution: the fluctuation is significant, East China is the dominant contributor, the drill-down points to product line A, and April/May business events are listed. |
| `2024年3月销量正常吗` | Anomaly check against the historical baseline. |
| `列出2024年4月华东的10条订单明细` | Detail mode, rendered as a table. |
| `2024年4月的销售额怎么样` | Clarification card because `销售额` maps to both net and gross sales. |
| `2024年4月老客户的净销售额` | The `老客户` business term is expanded into its first-order rule SQL. |
| `把订单表删了` | Refused without executing any write operation. |

> **Date caveat:** the demo data ends on 2024-06-30, while relative expressions such as “last month”, “this month”, or a month without a year are resolved against the machine's current date. The dataset default window is `last_month`, so when the machine date is outside the demo period those questions legitimately return zero rows. Use explicit 2024 dates in demos. Relative-time demonstrations are meaningful only when the machine date lies inside the demo period.

## 10. Run the automated tests

```bash
python -m pytest tests/ -q
```

The unit suite does not call the model, but it is not dependency-free. Before running it:

- `.env` must exist with a non-empty `DEEPSEEK_API_KEY`; importing the project validates the key even for offline tests;
- install the package with `pip install -e .` (or run pytest from the repository root);
- `scripts/init_db.py` must have run at least once, because several test modules load the published semantic snapshot.

The suite covers SemQL validation, SQL compilation, the security gateway, attribution, chart rules, clarification, and memory. Its size and pass count change as tests are added; treat a green run as a local signal, not a fixed guarantee.

## 11. Optional developer scripts

| Script | Real model calls | What it does |
| --- | --- | --- |
| `python scripts/dev_query.py` | No | Builds four SemQL examples by hand and runs compile, gateway, and execute, including the `east_mgr` row-policy case. |
| `python scripts/dev_e2e_llm.py` | Yes | Single-question end to end (`q01`) plus the clarification card from `q04`. |
| `python scripts/dev_insight.py` | Yes | Full attribution JSON and narrative for 2024-04 versus 2024-05, including the East China and product-line-A expectation. |
| `python scripts/dev_chat.py` | Yes | Chat orchestration: a data question, a why question, and a two-turn follow-up. |
| `python scripts/run_pipeline_test.py` | Yes | Runs `q01`–`q12` through the chat service and writes one JSON file per question plus `report.md` under `test_data/output/`. |

`dev_query.py` is the only development script that works without a model call. The other scripts and the pipeline regression call DeepSeek, need network access, consume API quota, and take minutes rather than seconds. They still require `.env` and an initialized metadata database.

## 12. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `conda: command not found` | conda is not installed or the shell is not initialized for it | Use the Anaconda Prompt, or run `conda init bash` and reopen the terminal. |
| `pip install` times out | The default package index is unreachable | Retry with a reachable mirror, for example `pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`. |
| `RuntimeError: 缺少环境变量 DEEPSEEK_API_KEY` | `.env` is missing from the repository root or the key is empty | Create `.env` from `.env.example` and set the key; the app reads only the root `.env`. |
| `check_llm.py` reports 401 | The key is invalid or contains extra spaces or quotes | Rewrite the `DEEPSEEK_API_KEY` line without quotes and restart the shell. |
| Model-not-found or 404 from the API | `LLM_MODEL_HEAVY` or `LLM_MODEL_LIGHT` is not enabled for the account | Set both to model IDs available in the DeepSeek console. |
| `check_llm.py` reports a timeout | No route to `DEEPSEEK_BASE_URL` | Check network, proxy, DNS, and the base URL. |
| Login returns 401 | Users were not seeded, or the app points at a different metadata database | Run `python scripts/init_db.py` and check `META_DB_PATH`. |
| `当前没有 published 的语义层版本` | The metadata database has no published semantic snapshot | Run `python scripts/init_db.py`. |
| Data questions return zero rows | The date is outside 2023-06-01 through 2024-06-30, or a relative term was resolved against today's clock | Ask with explicit 2024 dates; see the date caveat in section 9. |
| `init_db.py` exits saying the business database is missing | `retail.db` was not generated | Run `python scripts/gen_retail_data.py` first. |
| Port 8000 is already in use | Another process holds the port | Use the `--port 8001` uvicorn command in section 7. |
| The page stays on “正在理解您的问题…” | The model call is failing or hanging | Run `scripts/check_llm.py`, then inspect the server log. The historical conda `SSL_CERT_FILE` problem is self-healed in `chatbi/config.py`; if an environment still fails, make sure `$CONDA_PREFIX/ssl/cacert.pem` exists and restart the server. |
| Charts render as plain tables | The ECharts CDN could not be loaded | This is the intentional table fallback. Restore network access and refresh. |
| A clarification card is rejected as expired | The server restarted; pending clarification state is process-only | Ask the question again. |
| `403` on `/chat/ask` | You are signed in as `viewer1` | Sign in as `analyst` or `east_mgr`. |
| `ModuleNotFoundError: No module named 'chatbi'` | The package was not installed, or the command ran outside the repository | Run `pip install -e .` from the repository root. |

## 13. Clean up

Stop the server with `Ctrl+C`, then optionally leave the environment:

```bash
conda deactivate
```

Removing the environment (`conda env remove -n insightchat`) is optional. Deleting generated databases is destructive; use the reset procedure in section 6.3 only when losing history, feedback, and memories is acceptable.
