[English](AGENTS.md) | [简体中文](AGENTS.zh-CN.md)

# InsightChat Project Guide

## Current-state sources of truth

- Treat the code, seed data, scripts, and tests as the source of truth for current behavior.
- `notes_internal/` contains private notes, historical product decisions, and time-sensitive research. Do not describe those proposals as implemented behavior.
- `docs/` contains public, current-state documentation. `README.md` is the project entry point.
- When code and documentation disagree, verify the code and tests, then correct the documentation. Do not repeat historical test counts or acceptance results as current guarantees.

## Implemented project shape

InsightChat is a locally runnable, single-process ChatBI application built with FastAPI, SQLite, DeepSeek's OpenAI-compatible API, and a native JavaScript single-page frontend. It uses no vector database, Redis, Celery, PostgreSQL, ClickHouse, or React frontend.

The main data-query path is:

```text
question -> memory command check -> contextual rewrite -> intent classification
-> schema linking -> clarification when needed -> validated SemQL generation
-> deterministic SQL compilation -> SQL security gateway -> read-only SQLite execution
-> chart rules and memory personalization -> answer and trace
```

The LLM generates constrained SemQL JSON, not executable SQL. Insight calculations reuse the same compiler, gateway, and executor. Chart switching reuses the owner's stored answer snapshot and does not rerun SQL.

## Directory conventions

- `chatbi/`: application package.
- `scripts/`: initialization, server, connectivity, development, and regression commands.
- `tests/`: pytest suite; memory unit tests use isolated SQLite databases.
- `test_data/`: deterministic retail demo data and end-to-end fixtures.
- `data/`: runtime metadata database and generated jieba dictionaries.
- `docs/`: public bilingual Markdown documentation.
- `notes_internal/`: private notes; leave historical or research material there unless a current-state document is intentionally promoted.
- `demo/`: local demo media.

## Setup and verification

Python 3.10 or newer is required; Python 3.11 is recommended.

```bash
conda create -n insightchat python=3.11 -y
conda activate insightchat
pip install -e .
cp .env.example .env
```

Set `DEEPSEEK_API_KEY` in `.env`. Set `LLM_MODEL_HEAVY` and `LLM_MODEL_LIGHT` to models actually available to the configured DeepSeek account. Replace the demo `AUTH_SECRET` outside local demonstrations.

Initialize and run from the repository root:

```bash
python scripts/gen_retail_data.py
python scripts/init_db.py
python scripts/check_llm.py
python scripts/run_server.py
```

`gen_retail_data.py` recreates the demo business database. Do not run it against a database whose contents must be preserved. `init_db.py` creates missing metadata tables, seeds the published v1 snapshot when absent, executes Evidence SQL, and rebuilds dictionaries. Application startup only creates missing schema and loads dictionaries; it does not seed users or semantic objects.

Run the standard tests with:

```bash
python -m pytest tests/ -q
```

The unit tests do not call the LLM, but project imports require `DEEPSEEK_API_KEY` to be present and some tests require the initialized metadata database. `python scripts/dev_query.py` exercises compilation, permissions, and execution without LLM calls. The other `dev_*` scripts and `run_pipeline_test.py` make real model calls.

## Safety and implementation invariants

- Never print, commit, or copy secrets from `.env` into documentation or logs.
- Keep business database access read-only. All business SQL must pass `chatbi/query/gateway.py`; do not add a memory, insight, or administrative path that bypasses it.
- Preserve SELECT-only checks, table/function restrictions, row/column policy handling, fail-closed row filters, execution timeouts, and row limits.
- Do not claim that dataset policies are strongly enforced: current dataset policy data is exposed through `/auth/me`, while the gateway enforces a physical-table allowlist.
- Semantic initialization currently publishes seed snapshot v1 and supports versioned snapshot reads. There is no implemented draft/review/approval workflow.
- The demo dataset covers 2023-06 through 2024-06. Use explicit demo dates in examples; relative expressions such as “last month” follow the machine's current date.

## Memory-system invariants

- Keep session context and long-term memories isolated by `user_id`; memory must never bypass SQL compilation, permissions, or execution controls.
- Short-term context persists a whitelist of state in SQLite with an in-process LRU. Completed context can survive a server restart, but pending clarification state is process-only.
- Long-term categories are `chart_preference`, `term_mapping`, and `query_exemplar`. The implementation uses deterministic rules, literal-overlap exemplar recall, and no vector retrieval.
- Confidence decay is anchored to `last_confirmed_at`, not `last_accessed_at`. Using a memory must not renew it.
- Supersede and delete operations are logical. Historical memory values and events remain available for auditing; “forget” is not physical erasure.
- The chat command `忘掉全部记忆` also resets current session context. The UI clear button only clears long-term memories.
- Preserve the distinction between one-off chart requests and durable preferences, and only apply chart preferences compatible with the answer's available chart types.

## Documentation language and linking rules

- The default file is English, such as `docs/usage.md`; its Chinese counterpart is `docs/usage.zh-CN.md`. The same rule applies to `README.md` and `README.zh-CN.md`.
- Update both language versions whenever public behavior or instructions change.
- Start every public README/docs Markdown file with:

  ```text
  [English](usage.md) | [简体中文](usage.zh-CN.md)
  ```

  Adjust both filenames to the current document.
- Translate prose, headings, tables, and diagram labels. Do not translate code, commands, configuration keys, API names, identifiers, or file paths.
- English documents link to English versions. Chinese documents link to Chinese versions when they exist; otherwise keep a working original link and identify its language.
- Check relative paths, heading anchors, images, and external references after editing. Never create a link to a nonexistent Chinese document.
