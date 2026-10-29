[English](README.md) | [简体中文](README.zh-CN.md)

# InsightChat: Auditable ChatBI with User-Level Memory

InsightChat is a locally runnable conversational BI application that combines natural-language analytics with a persistent, user-scoped memory system. It turns questions into validated SemQL rather than trusting LLM-written SQL, compiles SQL deterministically, enforces data controls at a security gateway, and explains both answers and the memory used to shape them.

The project's distinguishing feature is its **BI-aware memory loop**: completed conversation context survives service restarts, chart and metric preferences can be learned per user, repeated clarification choices can become durable mappings, and liked answers can become personal few-shot examples. Confidence decay, logical deletion, history, and `memory_trace` make that personalization inspectable and reversible.

Other implemented highlights include:

- **Constrained NL2SQL:** schema linking, deterministic clarification, validated SemQL, rule-based post-processing, and a deterministic SQLite compiler.
- **Security independent of the LLM:** SELECT-only AST checks, table/function restrictions, row/column policy handling, fail-closed row filters, timeouts, and row limits.
- **Explainable insight attribution:** fluctuation detection, contribution-based drill-down, hierarchy constraints, low-base warnings, event association, and LLM narration with template fallback.
- **Semantic evidence and traces:** published seed snapshots, Evidence SQL executed during initialization, applied rules, final SQL, semantic versions, and memory-use traces.
- **A lightweight complete demo:** FastAPI, two SQLite databases, and a native JavaScript UI with ECharts and table fallback—no vector database or separate frontend build.

## User-Level Memory System

The memory system has two layers, both isolated by `user_id`:

| Layer | What is stored | How it is used |
| --- | --- | --- |
| Short-term session context | Recent turns, the last SemQL snapshot, term/value clarification choices, and the last answer reference | Contextual follow-ups, session-level disambiguation, and chart switching |
| Long-term user memory | `chart_preference`, `term_mapping`, and `query_exemplar` | Scenario-specific chart personalization, automatic term disambiguation, and personal few-shot examples |

Key behavior:

- **Restart-aware context:** completed session state is persisted in SQLite with an in-process LRU. A pending clarification card is process-only and expires on restart.
- **Scenario-specific chart preferences:** a preference for trend analysis does not become an unrelated global chart setting. One-off chart wording affects only the current answer; compatible durable preferences are applied afterward.
- **Chart switching without re-querying:** conversational or toolbar chart switching reuses the owner's stored result snapshot. Snapshots contain at most 200 rows and retain truncation information.
- **Clarification learning:** a term choice is remembered in the current session. The same choice in three distinct sessions is promoted by default from a candidate to an active long-term mapping; candidates are not applied.
- **Personal examples with governance:** liking a successful answer creates a personal example, while disliking withdraws it. Personal examples use literal-overlap retrieval, not vectors. The separate global-example path still requires upvotes from three distinct users.
- **Decay without self-renewal:** effective confidence decays from `last_confirmed_at`; merely using a memory updates access statistics but does not extend its life.
- **User control and auditability:** users can inspect and delete their memories. Supersede/delete operations are logical, so historical values and events remain for auditing. “Forget” is not physical erasure.
- **Visible use:** applied long-term preferences and personal examples are recorded in `memory_trace` and exposed in the UI's trace section.

The UI's clear button removes long-term memories only. The chat command `忘掉全部记忆` also resets the current session context. Memory never bypasses SemQL validation, SQL compilation, the security gateway, or data permissions.

See the [memory system guide](docs/MEMORY_SYSTEM.md), [feature tour](docs/MEMORY_SYSTEM_FEATURES.md), and [manual acceptance checklist](docs/MEMORY_SYSTEM_TEST_SCRIPTS.md) for details and boundaries.

## How It Works

```text
Question
  -> strict memory-command handling
  -> contextual rewrite
  -> intent classification
  -> dictionary/index linking + constrained LLM reranking
  -> deterministic clarification when needed
  -> validated SemQL generation with retries
  -> deterministic SemQL-to-SQLite compilation
  -> SQL security gateway and policy rewriting
  -> read-only execution
  -> chart rules + memory personalization
  -> answer, summary, evidence, and traces

Why/anomaly question
  -> fluctuation detection
  -> deterministic contribution attribution and event association
  -> LLM narrative or template fallback
```

The LLM does not generate executable business SQL. Insight queries reuse the same compiler, gateway, and executor as ordinary data questions, so metric rules and row/column controls remain in the calculation path.

The semantic layer currently consists of a seed-published v1 snapshot, versioned snapshot reads, metric/dimension/term definitions, business rules, and initialization-time Evidence checks. It does not include a draft/review/approval workflow.

## Quick Start

### 1. Prerequisites

- Python 3.10 or newer; Python 3.11 is recommended.
- Conda for the commands below, or an equivalent Python environment manager.
- A DeepSeek API key and network access for natural-language queries. Offline unit tests still require the configuration value at import time, although they do not call the API.

From the repository root:

```bash
conda create -n insightchat python=3.11 -y
conda activate insightchat
pip install -e .
```

`pip install -r requirements.txt` is an alternative to editable installation.

### 2. Configure the application

```bash
cp .env.example .env
```

On Windows Command Prompt, use `copy .env.example .env` instead. Edit `.env` and set at least:

| Key | Purpose |
| --- | --- |
| `DEEPSEEK_API_KEY` | Required API credential; keep it secret |
| `DEEPSEEK_BASE_URL` | OpenAI-compatible endpoint; defaults to `https://api.deepseek.com` |
| `LLM_MODEL_HEAVY` | Model for SemQL generation and insight narration |
| `LLM_MODEL_LIGHT` | Model for intent, rewriting, reranking, and summaries |
| `AUTH_SECRET` | HMAC token secret; replace the demo default outside local demonstrations |

The model names in `.env.example` are configuration examples, not a guarantee of availability. Set them to models enabled for your DeepSeek account. Database paths, timezone, insight thresholds, and memory defaults are also documented in `.env.example`; relative database paths resolve from the project root.

### 3. Initialize the demo

```bash
python scripts/gen_retail_data.py
python scripts/init_db.py
python scripts/check_llm.py
```

- `gen_retail_data.py` **recreates the demo business database** with deterministic data. Do not run it over data you need to preserve.
- `init_db.py` creates metadata tables, imports the seed and publishes v1 when absent, executes Evidence SQL against the business database, and rebuilds the jieba dictionaries. It is safe to rerun for the normal idempotent initialization flow.
- `check_llm.py` makes one real light-model call and verifies call logging. Omit it only if you do not want that API call.

The retail demo covers **2023-06 through 2024-06**. Use explicit dates in questions: relative expressions such as “last month” use the machine's current date and can fall outside the demo data.

### 4. Start and use the application

```bash
python scripts/run_server.py
```

Open <http://127.0.0.1:8000>. The server listens on loopback only. Stop it with `Ctrl+C`.

Demo accounts all use password `Init@123456`:

| Account | Role and behavior |
| --- | --- |
| `analyst` | Full demo querying; recommended for the main walkthrough |
| `east_mgr` | Analyst plus a row policy restricting results to East China (`HD`) |
| `admin` | Administrative role guards and full demo data |
| `viewer1` | Viewer role; `/chat/ask` is blocked, useful for checking role boundaries |

For a portable step-by-step guide and troubleshooting, see the [Runbook](docs/RUNBOOK.md).

## Tests and Validation

Run the standard suite after configuration and metadata initialization:

```bash
python -m pytest tests/ -q
```

The unit tests make no LLM calls. Some tests load the initialized semantic snapshot, and importing project configuration requires `DEEPSEEK_API_KEY` to be present.

Additional checks:

```bash
python scripts/dev_query.py           # No LLM: compile, gateway, permissions, execution
python scripts/dev_e2e_llm.py         # Real LLM: query and clarification
python scripts/dev_insight.py         # Real LLM narrative plus deterministic attribution
python scripts/dev_chat.py            # Real LLM: query, insight, and follow-up orchestration
python scripts/run_pipeline_test.py   # Real LLM: q01-q12 regression; writes test_data/output/
```

The full pipeline and most development scripts incur model API calls. The manual memory checklist covers UI behavior that the unit suite does not fully exercise.

## Documentation

- [Runbook](docs/RUNBOOK.md): installation, startup, reset cautions, and troubleshooting.
- [Implementation overview](docs/IMPLEMENTATION_SPEC.md): actual architecture, modules, APIs, data, and limitations.
- [Memory system](docs/MEMORY_SYSTEM.md): storage, learning, decay, APIs, security, and degradation.
- [Memory features and design highlights](docs/MEMORY_SYSTEM_FEATURES.md): a shorter user-oriented tour.
- [Memory manual acceptance checklist](docs/MEMORY_SYSTEM_TEST_SCRIPTS.md): reproducible UI scenarios with side effects and cleanup.
- [Permissions](docs/PERMISSIONS.md): authentication, role guards, row/column controls, gateway enforcement, and known gaps.

## Current Boundaries

InsightChat is a local demonstration, not a production deployment package. It currently uses one process, one SQLite business database, and a seed-centric semantic publishing flow. Pending clarifications and the context cache are process-local. The HMAC token has no server-side revocation, dataset policies are not strongly enforced in the query path, and the frontend does not restore prior transcripts when a session is selected. The implementation and permissions guides describe these limits in detail.
