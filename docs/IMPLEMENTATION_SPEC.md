[English](IMPLEMENTATION_SPEC.md) | [简体中文](IMPLEMENTATION_SPEC.zh-CN.md)

# InsightChat Implementation Specification

This document describes how the current InsightChat code base works. It is a current-state overview, not a frozen contract: the code, seed data, scripts, and tests are the source of truth. Where older design notes differ from the code, this document follows the code.

Related documents:

- [../README.md](../README.md) — project overview and quick start.
- [RUNBOOK.md](RUNBOOK.md) — installation, initialization, running, and troubleshooting.
- [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md) and [MEMORY_SYSTEM_FEATURES.md](MEMORY_SYSTEM_FEATURES.md) — user-level memory design and behavior.
- [MEMORY_SYSTEM_TEST_SCRIPTS.md](MEMORY_SYSTEM_TEST_SCRIPTS.md) — memory verification scenarios.
- [PERMISSIONS.md](PERMISSIONS.md) — authentication, roles, and policy enforcement in detail.

## 1. Scope and system shape

InsightChat is a locally runnable, single-process conversational BI application. A user asks a question in Chinese, the application links it against a small semantic layer, has a large language model emit a constrained SemQL JSON object, compiles that object into SQLite SQL deterministically, passes the SQL through a security gateway, executes it read-only, and returns a chart plus a trace. "Why" questions go through a deterministic attribution engine before a model writes the narrative.

The implementation is deliberately local:

- One FastAPI process, one metadata SQLite database, one demo business SQLite database.
- DeepSeek's OpenAI-compatible API is the only external service the backend calls.
- No PostgreSQL, ClickHouse, Redis, Celery, vector database, embedding model, or React build step.
- The frontend is a static single page served by FastAPI.

This document does not describe a production deployment. See section 20 for the limits that follow from the local design.

## 2. Stack

| Layer | Technology | Location |
| --- | --- | --- |
| HTTP service | FastAPI, uvicorn, sse-starlette | `chatbi/api/` |
| Model client | `openai` SDK pointed at DeepSeek | `chatbi/llm.py` |
| Metadata storage | SQLite | `data/meta.db` |
| Demo business storage | SQLite, opened read-only | `test_data/data/retail.db` |
| Semantic layer | YAML seed loaded into SQLite snapshots | `chatbi/semantic/seed/ontology_seed.yaml` |
| Chinese segmentation | jieba with a generated custom dictionary | `chatbi/semantic/dictionary.py`, `data/dict/` |
| SQL parsing and rewriting | sqlglot with the SQLite dialect | `chatbi/query/gateway.py` |
| API models | pydantic v2 | `chatbi/query/semql.py` and API route modules |
| Frontend | native JavaScript, ECharts from a CDN | `chatbi/api/static/index.html` |
| Tests | pytest | `tests/` |

Python 3.10 or newer is required; 3.11 is recommended.

## 3. End-to-end architecture

A data question takes the following path. The order matters: contextual rewriting happens **before** intent classification, and the memory-command check happens before both.

```text
question
  -> memory command check (strict patterns; bypasses NL2SQL)
  -> contextual rewrite (light model, only when session context exists)
  -> intent classification (light model, rule fallback)
  -> schema linking (dictionary path + mention path + light-model rerank)
  -> ambiguity detection and clarification card when needed
  -> SemQL generation (heavy model, three-level prompt injection, up to three attempts)
  -> deterministic post-fixes, then SemQL validation against the snapshot
  -> deterministic SQL compilation (SQLite dialect)
  -> SQL security gateway (AST checks, row and column policies, limit)
  -> read-only execution (30 s timeout, 10,000-row cap)
  -> chart rules and memory personalization
  -> summary, match explanation, trace
  -> SSE result event
```

Two related paths reuse the same building blocks:

- **Insight path.** `INSIGHT_WHY` and `INSIGHT_ANOMALY_CHECK` intents use the same compiler, gateway, and executor for every scalar and drill-down query, so attribution numbers and user queries share one definition.
- **Chart switching.** `POST /memories/chart-switch` reuses the answer snapshot already stored for that answer; it does not rerun SQL.

The model never writes executable SQL. It writes SemQL, a narrow JSON DSL; the compiler is deterministic, and the gateway re-checks the compiled SQL before execution.

## 4. Repository layout

```text
chatbi/
├── chatbi/
│   ├── config.py              environment loading, paths, thresholds
│   ├── db.py                  read-write metadata connection, read-only business connection
│   ├── llm.py                 chat_json / chat_text wrappers, retries, llm_call_log
│   ├── security.py            password hashing, token signing
│   ├── semantic/
│   │   ├── schema.py          CREATE TABLE IF NOT EXISTS for the metadata database
│   │   ├── store.py           snapshot loading, indexes, published-version handling
│   │   ├── seed.py            seed import and Evidence execution
│   │   ├── dictionary.py      jieba dictionary build and load
│   │   └── seed/ontology_seed.yaml
│   ├── nlp/
│   │   ├── prompts.py         prompt templates and fixed texts
│   │   ├── rewrite.py         contextual rewrite
│   │   ├── intent.py          intent classification and time parsing
│   │   ├── linking.py         schema linking
│   │   └── clarify.py         deterministic ambiguity detection and cards
│   ├── query/
│   │   ├── semql.py           SemQL models and validation
│   │   ├── generator.py       prompt assembly and generation retries
│   │   ├── compiler.py        SemQL to SQLite SQL
│   │   ├── gateway.py         SQL security gateway
│   │   └── executor.py        timed, read-only execution
│   ├── insight/
│   │   ├── fluctuation.py     period comparison and significance
│   │   ├── attribution.py     contribution drill-down and events
│   │   └── narrative.py       model-written conclusion with template fallback
│   ├── chart/rules.py         chart type selection
│   ├── services/
│   │   ├── chat_service.py    SSE orchestration and context
│   │   ├── query_service.py   link -> generate -> compile -> gateway -> execute
│   │   ├── memory.py          user-level memory
│   │   ├── history.py         sessions, answers, rerun
│   │   ├── feedback.py        votes, exemplars, review queue
│   │   └── auth.py            login and policy aggregation
│   └── api/
│       ├── main.py            app factory, startup hook, static mount
│       ├── deps.py            bearer parsing, role guards, error envelope
│       ├── routes_*.py
│       └── static/index.html
├── scripts/                   initialization, server, connectivity, development, regression
├── tests/                     pytest suite
├── test_data/                 demo database, question fixtures, expected fixtures, output
└── data/                      runtime metadata database and generated dictionaries
```

Stable entry points are the module paths above, not exact line numbers. The application factory is `chatbi.api.main:create_app`; the module-level ASGI app is `chatbi.api.main:app`.

## 5. Semantic layer and snapshots

### 5.1 Seed and publication

`scripts/init_db.py` loads `chatbi/semantic/seed/ontology_seed.yaml`, imports version-independent foundation rows, and publishes exactly one semantic snapshot, v1. `chatbi/semantic/store.py` loads the published version into an in-memory `SemanticSnapshot` and builds two indexes:

- an alias index from words to semantic elements, used by schema linking;
- a value index from words to dimension technical values, used by linking and attribution.

The snapshot is read on demand by API requests and scripts. `load_snapshot(version=...)` can read a named version; `history.rerun_answer` uses this to rerun an answer against its original semantic version.

There is **no draft/review/approval workflow** and no semantic publishing API or UI. The schema contains version and status columns, and versioned reads work, but the only publication path is the seed script, which always publishes v1 and is idempotent.

### 5.2 Seeded objects

| Object | Count | Notes |
| --- | --- | --- |
| Dataset | 1 | `ds_sales`, a sales dataset over orders, order items, stores, and customers; default time window `last_month`. |
| Models | 4 | Orders, order items, stores, customers. |
| Columns | 19 | Used for detail queries and column-level policies. |
| Relationships | 3 | Order items to orders; orders to stores; orders to customers, all many-to-one. |
| Metrics | 5 | Quantity, net sales, gross sales, valid order count, average order value. |
| Dimensions | 7 | Region, province, city, store, product line, channel, order status. |
| Terms | 6 | Five metric terms plus the business term `老客户`. |
| Entities | 3 | Customer, store, product. |
| Attributes | 1 | Store region, bound to the region dimension. |
| Ontology relations | 1 | Average order value derived from net sales and order count. |
| Ontology-to-MDL mappings | 16 | Term-to-metric, term-to-rule-SQL, attribute-to-dimension, entity-to-model, and term-to-dataset scope. |
| Business rules | 3 | Completed-only, exclude test channel, currency unit. |
| Evidence | 7 | Recomputation SQL for the five metrics plus status and channel distributions. |
| Business events | 2 | A May Day campaign and an East China logistics maintenance window. |
| Roles | 5 | `admin`, `semantic_admin`, `analyst`, `viewer`, `region_east`. |
| Users | 4 | `admin`, `analyst`, `east_mgr`, `viewer1`; demo password `Init@123456`. |
| Data policies | 4 | Three dataset-scope policies and one row policy for `region_east`. |
| Seed exemplars | 6 | Few-shot question-to-SemQL pairs. |
| Dictionary entries | 4 | Manually boosted business words. |

### 5.3 Metrics, rules, and dimensions

| Metric | Definition | Kind |
| --- | --- | --- |
| `metric_qty` (销量) | `SUM(order_items.quantity)` over completed orders | atomic |
| `metric_net_sales` (净销售额) | `SUM(quantity * unit_price * discount_rate)` over completed orders | atomic |
| `metric_gross_sales` (毛销售额) | `SUM(quantity * unit_price)` with no status filter | atomic |
| `metric_order_cnt` (有效订单数) | `COUNT(DISTINCT orders.order_id)` over completed orders | atomic |
| `metric_aov` (客单价) | `metric_net_sales / metric_order_cnt` | derived |

The completed-only condition is part of the `filter_sql` of the metrics that require completed orders; the compiler also records the applicable rule in `applied_rules`. The test-channel exclusion is injected by the compiler when a query touches `orders` without an explicit channel filter. The currency rule is informational.

Seeded business rules:

- `rule_completed` (block): net sales, quantity, and valid order count count completed orders only.
- `rule_exclude_test` (block): test-channel orders are excluded from business figures by default.
- `rule_unit` (info): money figures are in Chinese yuan.

The seven seeded dimensions are:

| Dimension | Display name | Values and notes |
| --- | --- | --- |
| `dim_region` | 大区 | `HD`/华东, `HB`/华北, `HN`/华南, `XB`/西北; hierarchy 大区 → 省份 → 城市 → 门店 |
| `dim_province` | 省份 | Part of the region-to-store hierarchy. |
| `dim_city` | 城市 | Part of the region-to-store hierarchy. |
| `dim_store` | 门店 | Store names; part of the same hierarchy. |
| `dim_product_line` | 产品线 | `A`/产品线A, `B`/产品线B, `C`/产品线C |
| `dim_channel` | 渠道 | `ONLINE`/线上, `OFFLINE`/线下, `TEST`/测试 |
| `dim_status` | 订单状态 | `completed`/已完成, `refunded`/已退款, `cancelled`/已取消 |

Value mappings translate display names and query aliases into technical values during compilation. The hierarchy is also used by attribution, which unlocks a level only after its ancestors have been drilled. Dimensions without value maps (province, city, store) use their stored values directly.

### 5.4 Evidence

Each metric term has at least one Evidence row containing a real SQL statement and a label. During `init_db.py`, every Evidence statement is executed against the business database and its result is stored in `ont_evidence.result_summary`. If any statement fails, initialization exits non-zero. Re-running initialization refreshes the stored results, so Evidence stays aligned with the current `retail.db`.

## 6. Demo dataset

### 6.1 Schema

| Table | Columns | Notes |
| --- | --- | --- |
| `stores` | `store_id`, `store_name`, `region_name`, `province`, `city` | 32 stores: 4 regions x 2 provinces x 2 cities x 2 stores. |
| `customers` | `customer_id`, `first_order_date`, `level` | 200 customers; levels A/B/C. |
| `orders` | `order_id`, `store_id`, `customer_id`, `status`, `order_date`, `channel` | About 9.1k orders. |
| `order_items` | `id`, `order_id`, `product_line`, `quantity`, `unit_price`, `discount_rate` | About 18.3k lines; one to three lines per order. |

Indexes exist on `orders(order_date)`, `orders(store_id)`, and `order_items(order_id)`. The generator recreates the file with a fixed seed, so counts and the data story are reproducible.

### 6.2 Volume and distributions

- Date range: 2023-06-01 through 2024-06-30.
- Stores: 32; customers: 200.
- Orders: about 9,148; order lines: about 18,297.
- Status mix: completed about 85%, refunded about 8%, cancelled about 7%.
- Channel mix: online about 55%, offline about 42%, `TEST` about 3%.
- Product-line baseline share: A 40%, B 35%, C 25%; base prices 100, 200, 150 yuan.

### 6.3 Built-in data story

Monthly completed, non-test sales volume by region is generated around HD 1,300, HB 900, HN 800, and XB 500 units with small random noise. Two deviations are deliberate:

- East China (`HD`) drops about 35% in 2024-04 versus 2024-03, and product line A accounts for about 70% of the drop;
- East China recovers about 25% in 2024-05 versus 2024-04, led by product line A.

This makes `为什么2024年4月销量比5月低` attribute mainly to East China and then to product line A. The generator prints a month-by-region pivot and the East China product-line split at the end so the story can be checked visually.

### 6.4 Business events

| Event | Window | Related dimension value |
| --- | --- | --- |
| 五一促销主会场活动 | 2024-05-01 to 2024-05-07 | Region `HD` |
| 华东物流线路检修 | 2024-04-10 to 2024-04-12 | Region `HD` |

Attribution lists an event when it overlaps the compared period or the interval between the two periods and is either global or matches a dimension value on the attributed path. Events are evidence for interpretation, not proof of causality.

## 7. Natural-language pipeline

### 7.1 Memory-command bypass

Before any model call, `chatbi/services/chat_service.py` applies strict pattern matching for memory commands: view memories, forget everything or a category or a term, switch chart type, remember a chart preference, and remember a term mapping. A match emits `intent=MEMORY` and handles the request without entering NL2SQL. Ordinary phrases such as "use a line chart this time" are not treated as durable memory commands.

The command strings and their behavior are documented in [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md).

### 7.2 Context and rewrite

Session state is kept in two layers: an in-process `OrderedDict` LRU and a persisted `chat_context` row. The persisted whitelist is `turns`, `last_semql`, `clarify_choices`, and `last_answer`. `clarify_pending` is **not** persisted; a restart loses a pending clarification card. Completed context can survive a restart.

`rewrite(question, turns)` returns `(rewritten_question, inherits, topic_changed)`. It is called only when context exists, uses the light model at temperature 0, and falls back to treating the question as a single turn on failure. `topic_changed=true` clears the stored SemQL. At most the last five turns are used for rewriting and intent classification.

### 7.3 Intent classification

`chatbi/nlp/intent.py` classifies into six intents:

- `DATA_QUERY`
- `INSIGHT_WHY`
- `INSIGHT_ANOMALY_CHECK`
- `CHITCHAT`
- `UNSUPPORTED`
- `FORBIDDEN_DOMAIN`

The light model is called at temperature 0.0, retried once at 0.2, and then replaced by a keyword rule classifier that marks the result `degraded=True`. The classifier returns mentions, a time range, a grain, ambiguities, and a dataset id.

Time handling prefers a model-provided `YYYY-MM-DD` range (with start/end corrected if reversed) and otherwise parses the model's time expression. The fallback parser supports explicit dates, a year-month, a month without a year, a year, "上个月", "本月", "今年", and "去年". A month without a year resolves against the current year in `TZ_NAME`; this is why the static demo data and relative questions can disagree.

Mentions are the union of model-provided mentions and dictionary hits whose jieba nature starts with `_`. An unknown dataset falls back to the first dataset in the snapshot.

### 7.4 Schema linking

`chatbi/nlp/linking.py` uses three stages:

1. **Dictionary path.** jieba segments with part-of-speech tags; whole-word hits map directly to terms, dimensions, dimension values, entities, attributes, datasets, or models.
2. **Mention path.** Each intent mention is looked up in the dimension-value index and the alias index.
3. **Model rerank.** Only mentions still unmatched are sent to the light model, with candidates limited to terms and dimensions in the same dataset. Dimension values never go through the model. The model may return a candidate id or `NEED_CLARIFY`; unknown ids are recorded as hallucinated and discarded.

Linking does not resolve conflicts itself. A mention that matches multiple terms is kept, and the clarification layer decides whether to ask.

### 7.5 Clarification

`chatbi/nlp/clarify.py` detects ambiguities with deterministic rules, not a model:

| Ambiguity | Trigger |
| --- | --- |
| `TERM_CONFLICT` | One mention maps to at least two metric terms bound to at least two different metrics. |
| `VALUE_CONFLICT` | One mention resolves to at least two technical dimension values. |
| `DATASET_CONFLICT` | Mention resolution spans datasets; rarely reachable with one seeded dataset. |
| `TIME_MISSING` | No time range and the dataset has no default time window. |
| `GRAIN_MISSING` | The question mentions trend/change wording but no grain was extracted. |

At most one card is emitted per turn, in the priority order above, and at most two clarification rounds are allowed. Card options are built from the detected candidates: term name plus a short definition, recent time windows, day/week/month grains, or dimension values. When the user answers with free text, the service re-asks with the text appended as a clarification; when the user picks an option, the selected key must belong to the current card.

Pending state includes the original question, intent, link result, round, and candidates, and carries the user and session it belongs to. Because it is process-only, a restart makes the card expire. Session choices and still-valid long-term term preferences are reapplied only when the candidate still exists in the current snapshot.

## 8. SemQL and generation

### 8.1 SemQL shape

`chatbi/query/semql.py` defines the DSL. A query has:

- `dataset`
- `metrics[]`
- `dimensions[]`
- `time` with `field`, `range`, and optional `grain` (`day`, `week`, `month`)
- `filters[]` with `dimension` or `metric`, an operator (`in`, `not_in`, `eq`, `gt`, `lt`, `ge`, `le`), and values
- `order_by[]`
- `limit`
- `compare` (`mom`, `yoy`, or `period_vs_period`) with an optional base period
- `query_mode` (`aggregate` or `detail`)
- `business_terms[]`

Limits are capped at 10,000 for aggregate queries and 1,000 for detail queries. `validate_against(snapshot)` checks dataset, metrics, dimensions, columns, filter values, business terms, limit, and time expressions before compilation.

### 8.2 Prompt assembly and retries

`chatbi/query/generator.py` builds a three-level prompt:

- **L1:** the metric and dimension manifest for the dataset;
- **L2:** details of the matched elements, including definitions, units, direction, mandatory block rules, derivations, and value mappings;
- **L3:** candidate expansion for elements that were not matched.

Up to three exemplars and the last five turns are added. Exemplars come from seed data, the user's own upvoted question/query pairs, and globally active feedback exemplars.

Generation tries up to three times: the original prompt at temperature 0.0, an error-echo retry at 0.0, and a candidate-expanded retry at 0.3. A total failure raises `LLMGenerateFailedError`.

### 8.3 Deterministic post-fixes

After generation and before validation, `chatbi/services/query_service.py` applies rule-based corrections:

- The intent's time range overrides the SemQL time range. For comparison queries, the model's current period is kept and the start is extended only enough to cover the comparison buckets.
- If the matched term binds to exactly one metric, that metric is forced to be the only or first metric. This prevents exemplars from pulling the query to a different metric.
- When a business term was matched, its id is forced into `business_terms`.

If compilation then fails with `SEMQL_INVALID_REF` or `SEMQL_INVALID_VALUE`, the service regenerates once with the compiler error echoed back. Other compiler errors fail the query immediately.

## 9. Compiler

`chatbi/query/compiler.py` is deterministic and never calls a model.

- **Main model.** The compiler starts from the first metric, or from the components of a derived metric, and falls back to the first dimension or column.
- **Joins.** It walks the relationship graph breadth-first. A many-to-many relationship raises `COMPILE_FANTRAP_RISK`; an unreachable model raises `COMPILE_NO_JOIN_PATH`. The model that owns the time field is always joined.
- **Atomic metrics.** Expanded as `AGG(CASE WHEN <condition> THEN <expression> ELSE 0 END)`. `COUNT_DISTINCT` is handled separately.
- **Derived metrics.** Only `metric_a / metric_b` is supported, rendered as `ROUND(num * 1.0 / NULLIF(den, 0), 2)`. Other formulas raise `COMPILE_UNSUPPORTED_FORMULA`.
- **Value translation.** Display values, query aliases, and technical values are translated to technical values through `value_maps`. Unknown values raise `SEMQL_INVALID_VALUE`.
- **Business terms.** `term_rule_sql` templates are inlined as additional WHERE predicates, with `{t_start}` replaced by the query start date. The predicate is recorded in `applied_rules`.
- **Rule injection.** A query touching `orders` without an explicit channel filter gets `orders.channel != 'TEST'`. Metrics with a completed-only filter contribute the corresponding rule note.
- **Grouping and ordering.** Grain uses `strftime` (`%Y-%m` for month, `%Y-W%W` for week, `date()` for day). A query with a grain but no `order_by` is ordered by time ascending. The default limit is 100.
- **Comparison.** With a grain, month-over-month and year-over-year use a `LAG` window over the requested buckets; without a grain, two `CASE WHEN` columns compare two periods. `period_vs_period` with a grain is rejected.

The compiler returns SQL text, `applied_rules`, and a trace used later to derive column roles.

## 10. Security gateway and execution

### 10.1 Gateway checks

`chatbi/query/gateway.py` processes SQL in a fixed order:

1. Parse a single statement with sqlglot's SQLite dialect.
2. Require a `SELECT` or `WITH ... SELECT`; reject write, DDL, command, attach, pragma, and merge nodes.
3. Reject file and network functions: `load_extension`, `readfile`, `writefile`, `file`, `url`.
4. Allow only the physical tables `orders`, `order_items`, `stores`, and `customers`; CTE names are exempt.
5. Inject row policies.
6. Apply column policies to output columns.
7. Add or lower a `LIMIT 10000` as needed.

Any forbidden construct raises `SqlForbiddenError` and is not retried.

### 10.2 Row and column policies

Row policies are loaded from `data_policy` rows of type `row`. For each policy dimension, allowed values are unioned and injected as a predicate. If a policy has no usable values, or its dimension or model no longer exists, the gateway injects `1 = 0`, so the failure is closed. If the dimension's table is not directly in the query, the gateway walks the relationship graph and injects a nested subquery predicate.

Column policies match **output column names**. A `hide` policy raises `COLUMN_FORBIDDEN`; a `mask` policy rewrites the selected expression with `substr` masking.

Dataset-level policies are **not** enforced here or anywhere else in the query path. They are stored, returned by `/auth/me` as `policies.dataset_ids`, and otherwise informational. The effective table boundary is the gateway's physical-table allowlist.

The gateway returns re-rendered SQL: sqlglot normalizes function case, aliases, and formatting, so the text executed may differ from the compiler's exact output. `applied_rules` includes both compiler rules and gateway notes such as the applied row scope.

### 10.3 Execution

`chatbi/query/executor.py` opens the business database read-only, sets a 30-second wall-clock timeout checked by a SQLite progress handler every 1,000 steps, caps results at 10,000 rows, and fetches one extra row to set `truncated`. A timeout raises `QueryTimeoutError`.

## 11. Insight pipeline

### 11.1 Trigger, metric, and periods

`INSIGHT_WHY` and `INSIGHT_ANOMALY_CHECK` are handled in `chat_service._run_insight`. The metric is the first matched metric term, or `metric_qty` when nothing matched.

Periods are chosen deterministically:

- if the question contains at least two months, the first two are used;
- if it contains one month and the intent has a time range, the starting month of that range is used as period A;
- otherwise the engine compares the last complete month with the month before it and records the assumption.

The base filter always excludes the `TEST` channel and merges dimension values mentioned in the question, so the insight uses the same scope as an equivalent data query.

### 11.2 Significance

`chatbi/insight/fluctuation.py` computes both period values through the full SemQL, compiler, gateway, and executor path. It builds a baseline from up to 12 complete months before period A, computes the mean and population standard deviation of month-over-month changes, and marks the result significant when the absolute percentage change is at least `INSIGHT_SIGNIFICANT_PCT` or the deviation from the historical mean is at least `INSIGHT_SIGMA_K` standard deviations. With fewer than six baseline periods, only the percentage rule applies and the result is marked `baseline_limited`. If either period has no data, the chain stops and the user is told that attribution is not possible.

### 11.3 Attribution

`chatbi/insight/attribution.py` is deterministic:

- For a derived metric, it first applies a logarithmic decomposition, `ln(A) - ln(B) = ln(num_A/num_B) - ln(den_A/den_B)`, reports numerator and denominator shares, and drills into the larger contributor.
- Candidate dimensions come from the metric's related dimensions, minus dimensions whose columns are hidden by policy.
- At each level it runs two grouped queries per candidate dimension and selects the dimension with the highest coverage, defined as the absolute contribution of the top three values divided by the local delta. Ties are broken by the largest absolute top contribution.
- A level is expanded only while coverage is at least 50%; a first level below that threshold returns a diffuse result with no drill-down. A node continues only if it contributes at least 20% of the local delta and at least 10% of the global delta, and the path is no deeper than three levels.
- Hierarchy dimensions unlock only after their ancestors have been drilled, while dimensions outside the hierarchy, such as product line and channel, are always candidates.
- When a dimension value has at least three monthly points, a z-score is computed; a small value with `|z| < 1` and less than 1% of the base period gets a low-base warning rather than being presented as a main cause.
- The waterfall is built as base total, top first-level factors, "other", and current total.
- Events overlapping period A or the A-to-B interval are attached when they are global or match a dimension value on the attributed path.

### 11.4 Narrative and persistence

`chatbi/insight/narrative.py` asks the heavy model for a short conclusion (result, main cause, event corroboration, low-base note, next step, up to 250 characters). If the model fails or returns unusable output, a deterministic template produces the text and the result is marked degraded. `POST /insight/attribute` runs synchronously, stores an `insight_report`, and returns the report and narrative. `GET /insight/{report_id}` returns a report to its owner or an admin. The chat frontend renders the narrative and waterfall directly from the chat result rather than calling these endpoints.

## 12. Chart selection

`chatbi/chart/rules.py` selects one chart type from column roles, row shape, and SemQL. Rules are evaluated in priority order and the first match wins.

| Rule | Condition | Result |
| --- | --- | --- |
| 11 | Columns are label/value/type | `waterfall` (attribution) |
| 9 | `query_mode=detail` | `table` |
| 1 | One row, one metric, no dimension | `metric_card` |
| 2 | Time x one metric, no category, at least three rows | `line` |
| 3 | Time x one metric x one category | `multi_line` |
| 4 | Category x one metric, at most 12 rows | `bar`, or `bar_horizontal` when average label length exceeds 6 |
| 5 | Category x one metric, more than 12 rows | `top10_bar_horizontal` |
| 6 | Category x one metric whose name contains share/ratio/percent wording | `pie` up to 7 categories, otherwise `treemap` |
| 10 | Anything else | `pivot_table`, falling back to `table` |

Column roles come from the compiler trace: time aliases are `time`, dimension aliases are `dimension`, metric aliases or names ending in `_基期` or `_差值`, or containing `%`, are `metric`, and the rest are `column`.

Durable chart preferences are applied afterward. An explicit chart request in the current turn wins and is not written as a long-term preference. Otherwise a compatible preference for the same data shape is applied, but only if the target type remains in `available_types`.

Known implementation gaps in this area are listed in section 20.

## 13. History and feedback

### 13.1 History

Chat sessions, messages, query logs, and answers are stored in `chat_session`, `chat_message`, `query_log`, and `answer`. Session lists are ordered by `updated_at` with cursor pagination. A session detail returns the latest 100 messages to its owner. Answer snapshots store chart type, chart metadata, column roles, the first 200 result rows, row count, and summary.

`POST /history/answers/{answer_id}/rerun` is owner-only. It reloads the question from the linked query log and reruns it against the current published snapshot by default, or against the original semantic version when `use_original_version=true`. The response includes `rerun_of` and the original version, plus a version note when the versions differ. A rerun goes through generation again and does not produce a chart or summary object.

The frontend does not call the rerun endpoint or the session-detail endpoint.

### 13.2 Feedback

`POST /feedback` accepts `up` or `down`. A downvote requires one of `wrong_data`, `wrong_intent`, `bad_chart`, `untrusted_insight`, or `other`, and at least one of `answer_id` and `insight_id` must be supplied. Re-submitting the same user's vote for the same object overwrites it and appends a `feedback_history` row.

An upvote on an answer immediately writes a personal question/query exemplar for that user. It also records the question and SemQL as a global `query_exemplar` candidate; the candidate becomes active only after three distinct users upvote the same question. A downvote removes the user's personal exemplar. `GET /feedback/queue` lists pending downvotes with the question and final SQL for admins and semantic admins.

The frontend only shows feedback buttons for data answers. A downvote always sends `reason_tag=other`; there is no reason picker or comment box. Insight answers have no feedback button in the UI, although the API accepts `insight_id`.

## 14. Memory integration seams

The memory system is documented in [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md). Its integration with the query path is limited to these seams:

- Session context persists the whitelist described in section 7.2; pending clarification is process-only.
- Long-term categories are `chart_preference`, `term_mapping`, and `query_exemplar`, isolated by `user_id`.
- Confidence decay is anchored to `last_confirmed_at`; using a memory does not renew it.
- A clarification choice can come from the current session or from a valid long-term term preference, but only if the candidate still exists in the current snapshot.
- Chart personalization applies a durable preference only when the target type is compatible with the answer.
- Upvoted answers become personal exemplars and can be retrieved as few-shot examples; a model or memory failure falls back to the normal query path.

## 15. Permissions

Authentication and enforcement are detailed in [PERMISSIONS.md](PERMISSIONS.md). The current implementation can be summarized as follows:

- Passwords use PBKDF2-HMAC-SHA256 with 200,000 iterations.
- A token is a base64url payload `{user_id, exp}` plus an HMAC-SHA256 signature, valid for 8 hours. There is no refresh or server-side revocation.
- Login accepts only active users. Token validation checks the signature and user existence but not the user's current status, so a disabled user's existing token remains valid until it expires.
- `admin` bypasses role guards. `analyst` can browse the semantic summary; `semantic_admin` and `admin` can browse semantic objects, use the linking debug endpoint, and read the feedback queue; `admin` can list users and audit logs.
- Any authenticated user can read their own sessions, histories, memories, and insight reports they own, and can call `POST /insight/attribute`.
- Chat requires a non-viewer role. `viewer1` therefore cannot ask questions through `/chat/ask`, but can still call the attribution API.
- Row and column policies are enforced by the gateway. Dataset policies are not enforced; they are only exposed through `/auth/me`.
- Audit rows are written for login success and failure, memory REST writes, deletes, and clears, and REST chart switching. Chat memory commands write `user_memory_event`, not `audit_log`. Rejected queries and policy hits are not audited.

## 16. HTTP API

All API routes are mounted under `/api/v1`; the static frontend is served from `/`. REST success responses use `{"code": 0, "data": ...}`. REST errors use `{"code": <int>, "message": ..., "trace_id": ...}`. SSE error events use a string `code` and a `retryable` flag, which is a different convention from REST.

| Method and path | Access | Purpose |
| --- | --- | --- |
| `POST /auth/login` | Anonymous | Log in; returns token and user. |
| `GET /auth/me` | Authenticated | Current user, roles, and aggregated policies. |
| `POST /chat/sessions` | Authenticated | Create a session explicitly. |
| `GET /chat/sessions` | Authenticated | List the caller's sessions; same implementation as the history list. |
| `GET /chat/sessions/{id}` | Owner | Session plus messages. |
| `DELETE /chat/sessions/{id}` | Owner or admin | Delete messages, session, and short-term context. |
| `POST /chat/ask` | Non-viewer | Ask a question; responds with SSE events `intent`, `clarify`, `progress`, `result`, `error`. |
| `POST /chat/clarify` | Authenticated | Continue a pending clarification; responds with SSE. |
| `POST /insight/attribute` | Authenticated | Run attribution synchronously and store a report. |
| `GET /insight/{report_id}` | Owner or admin | Read a stored insight report. |
| `GET /history/sessions` | Authenticated | Cursor-paginated session list. |
| `POST /history/answers/{answer_id}/rerun` | Owner | Rerun a stored answer; optional original semantic version. |
| `POST /feedback` | Authenticated | Submit or replace a vote. |
| `GET /feedback/queue` | `admin`, `semantic_admin` | Pending downvotes with question and SQL. |
| `GET /memories` | Authenticated | List the caller's memories. |
| `POST /memories` | Authenticated | Save an explicit chart preference or term mapping. |
| `DELETE /memories/{memory_id}` | Owner | Logically delete one memory. |
| `DELETE /memories` | Authenticated | Clear the caller's memories, optionally by category. |
| `POST /memories/chart-switch` | Owner of the answer | Switch the chart on a stored answer without rerunning SQL. |
| `GET /semantic/summary` | `admin`, `semantic_admin`, `analyst` | Semantic version and object counts. |
| `GET /semantic/terms`, `/metrics`, `/dimensions`, `/rules`, `/events` | `admin`, `semantic_admin` | Browse semantic objects. |
| `GET` or `POST /semantic/debug/link` | `admin`, `semantic_admin` | Debug dictionary-based linking; passes no mentions. |
| `GET /sys/users` | `admin` | List users with roles. |
| `GET /sys/audit-logs` | `admin` | Recent audit entries. |
| `GET /` | Anonymous | Static demo frontend. |

Common error codes are `1001` for unauthenticated or failed login, `1003` for forbidden, `1004` for not found, `1400` for invalid parameters, `3001` and `3004` for attribution errors, and `5000` for uncaught exceptions.

## 17. Frontend

`chatbi/api/static/index.html` is a single static page using native JavaScript and ECharts loaded from a CDN. It calls only these server endpoints:

- `POST /auth/login`
- `GET /history/sessions`
- `POST /feedback`
- `GET /memories`, `DELETE /memories/{id}`, `DELETE /memories`
- `POST /memories/chart-switch`
- `POST /chat/ask` and `POST /chat/clarify` as SSE

The token and user object are stored in `localStorage`. SSE is read with `fetch` and a manual reader that normalizes CRLF and splits events on blank lines; the server emits LF separators to match. The interface shows an analysis timeline, a collapsible trace panel with SQL, SemQL, matching explanation, applied rules, memory usage, and semantic version, and renders data, insight, or text answers.

Chart rendering supports `bar`, `bar_horizontal`, `top10_bar_horizontal`, `line`, `multi_line`, `pie`, `waterfall`, and `metric_card`. `table` and `pivot_table`, or any chart when ECharts is unavailable, render as HTML tables. Chart switch buttons call the memory endpoint and redraw from the returned chart.

Frontend limitations:

- The history drawer lists session titles but does not fetch and rehydrate the messages when a session is selected; server-side context still resumes, so follow-up questions inherit correctly while old bubbles stay hidden.
- There is no UI for session deletion, answer rerun, insight report retrieval, or semantic browsing.
- Feedback exists only for data answers, with a fixed `other` reason on downvote.
- Two example chips, `今年上半年各品类的毛利率趋势` and `销售额 Top10 的商品`, refer to metrics or dimensions that the seeded semantic layer does not have.
- The page itself is public; authentication gates the data APIs.

## 18. Storage

### 18.1 Metadata database

`data/meta.db` is created by `init_schema` and contains the following table groups:

- Semantic layer: `datasource`, `mdl_*`, `ont_*`, `ont_mdl_mapping`, `semantic_version`.
- Identity and policy: `app_user`, `role`, `user_role`, `data_policy`, `audit_log`.
- Conversation: `chat_session`, `chat_message`, `chat_context`, `query_log`, `answer`.
- Insight and feedback: `insight_report`, `feedback`, `feedback_history`, `query_exemplar`, `business_event`, `dict_entry`.
- Memory: `user_memory`, `user_memory_event`.
- Model telemetry: `llm_call_log`.

`query_log` stores the question, matched elements, SemQL, final SQL, applied rules, semantic version, retry count, status, error code, and timing. Some columns exist but are never written by current code: `rewritten_question`, `intent`, `assumptions`, `policy_hash`, and `degraded` in `query_log`, and `elapsed_ms` in `insight_report`. The `datasource` row is not used to open connections, and `mdl_measure` is not used by the compiler. Treat these as reserved.

### 18.2 Business database and generated files

- `test_data/data/retail.db` is the read-only demo business database, recreated by `scripts/gen_retail_data.py`.
- `data/dict/custom_dict.txt` and `data/dict/word_nature.json` are generated from the published snapshot. The text file stores only short codes because jieba's user-dictionary third column accepts lowercase ASCII only; full natures are restored from the JSON file at segmentation time.
- `test_data/output/` receives per-question JSON files and `report.md` from the pipeline regression script.

The metadata database and the output directory are git-ignored. The demo business database is tracked so the demo data ships with the repository, and regenerating it is deterministic.

## 19. Configuration

`chatbi/config.py` loads only the project-root `.env`, resolves relative paths against the project root, fixes an invalid `SSL_CERT_FILE` before importing httpx or openai, and fails at import time when `DEEPSEEK_API_KEY` is missing.

| Key | Default | Effect |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | required | Model authentication. |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI-compatible endpoint. |
| `LLM_MODEL_HEAVY` | `deepseek-v4-pro` (example) | SemQL generation and narratives. |
| `LLM_MODEL_LIGHT` | `deepseek-flash` (example) | Rewrite, intent, rerank, summaries. |
| `LLM_TIMEOUT_SEC` | 60 | Per-call timeout. |
| `META_DB_PATH` | `./data/meta.db` | Metadata database. |
| `BIZ_DB_PATH` | `./test_data/data/retail.db` | Read-only business database. |
| `AUTH_SECRET` | `change-me` (code default) | Token HMAC key. |
| `TZ_NAME` | `Asia/Shanghai` | Relative-time reference zone. |
| `INSIGHT_SIGNIFICANT_PCT` | 5 | Percentage significance threshold. |
| `INSIGHT_SIGMA_K` | 2 | Sigma significance threshold. |
| `MEMORY_SESSION_TTL_HOURS` | 72 | Session-context TTL. |
| `MEMORY_CONTEXT_CACHE_SIZE` | 128 | In-process cache size. |
| `MEMORY_TERM_PROMOTE_SESSIONS` | 3 | Promotion threshold for term choices. |
| `MEMORY_MIN_CONFIDENCE` | 0.35 | Minimum effective confidence. |
| `MEMORY_EXPLICIT_HALF_LIFE_DAYS` | 365 | Explicit preference half-life. |
| `MEMORY_INFERRED_HALF_LIFE_DAYS` | 90 | Inferred preference half-life. |

Derived directories are `DATA_DIR` (the metadata database's parent), `DICT_DIR`, `DICT_FILE`, and `OUTPUT_DIR`. `ensure_dirs()` creates them before opening databases.

The model names in `.env.example` are placeholders. They must be replaced with models available to the configured account.

## 20. Known limits

1. **Relative time versus static demo data.** The demo data ends on 2024-06-30 while relative expressions use the machine clock, and the dataset default window is `last_month`. Relative questions can return zero rows outside the demo period. Use explicit 2024 dates. The attribution low-base z-score history is also anchored at the demo start, 2023-06-01.
2. **Dataset policies are not enforced.** `data_policy` rows of type `dataset` are only exposed by `/auth/me`. Neither the query path nor the gateway checks them.
3. **Viewer scope is partial.** `viewer1` cannot call `/chat/ask`, but can call `POST /insight/attribute`, which issues real queries through the same pipeline.
4. **Pending clarification is process-only.** A server restart discards a pending card and further clarification attempts return `CLARIFY_EXPIRED`.
5. **Single dataset, single process.** There is no multi-dataset routing, multi-tenancy, concurrency control, migration framework, or horizontal scaling. CORS allows `*` together with credentials, which browsers reject as a combination; the frontend uses a Bearer header instead.
6. **Semantic versioning is minimal.** Only the v1 seed is published; versioned reads exist, but there is no draft, review, approval, or publish workflow and no version management UI.
7. **History and rerun are backend-only.** The API supports session detail, session deletion, and answer rerun; the frontend exposes none of them.
8. **Feedback loop is incomplete.** Insight feedback is accepted by the API but has no UI. Downvotes from the UI always use `other` and cannot include a reason or comment.
9. **Chart rule text overstates behavior.** Rules 3 and 5 describe a top-N-plus-other merge that is not implemented; row counts are governed by SQL limits. Rule 6 is effectively unreachable because rules 4 and 5 match category-by-metric shapes first.
10. **Token lifecycle is basic.** Tokens last 8 hours with no refresh or revocation, and token validation does not re-check the user's status.
11. **Error conventions differ.** REST errors use integer codes and a uniform envelope; SSE errors use string codes and may include internal exception text in the fallback path.
12. **Some schema fields are reserved.** `query_log.rewritten_question`, `intent`, `assumptions`, `policy_hash`, `degraded`, and `insight_report.elapsed_ms` are not written; `datasource` and `mdl_measure` are not used at runtime.
13. **Frontend examples exceed the semantic layer.** Two built-in example chips reference metrics and dimensions that do not exist in the seed, so they lead to clarification or failure.
14. **The static page is public.** Authentication protects the API, not the HTML shell.

## 21. Verification

The repository ships a pytest suite under `tests/`. It does not call the model, but importing the project requires `DEEPSEEK_API_KEY`, and several test modules load the published semantic snapshot, so an initialized `data/meta.db` is required.

```bash
python -m pytest tests/ -q
```

`python scripts/dev_query.py` exercises compilation, gateway checks, and execution without any model call. The other `dev_*` scripts and `python scripts/run_pipeline_test.py` make real model calls; the pipeline script writes per-question JSON and `report.md` under `test_data/output/`. Test counts and pass results are not fixed guarantees; read the current output rather than relying on historical numbers.
