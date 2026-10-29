[English](PERMISSIONS.md) | [简体中文](PERMISSIONS.zh-CN.md)

# Permissions and Access Control

This page describes the access control the current code actually implements: authentication, role guards, resource ownership, and the row/column enforcement performed by the SQL security gateway. It also states what is only declared, and where the boundaries are.

InsightChat is a single-process, locally runnable demonstration. Treat these controls as demonstration controls, not production-grade access control. A concrete list of gaps is in [Limits](#11-limits).

## 1. Where control happens

```text
HTTP request
  -> authentication: Bearer token -> user            (401 / code 1001)
  -> role guard and resource ownership               (403 / code 1003)
  -> SemQL -> deterministic SQL compilation
  -> SQL security gateway: row predicates injected, column hide/mask, LIMIT cap
  -> read-only SQLite execution: wall-clock timeout, row limit
```

Two properties define the design:

- The LLM never decides permissions. It produces constrained SemQL; the SQL text that reaches the database is compiled deterministically and then rewritten by `chatbi/query/gateway.py`.
- No path bypasses the gateway. Chat queries call it from `chatbi/services/query_service.py`; every internal insight SQL calls it from `chatbi/insight/fluctuation.py` and `chatbi/insight/attribution.py`.

## 2. Authentication

Implementation: `chatbi/security.py`, `chatbi/services/auth.py`, `chatbi/api/deps.py`.

- Login: `POST /api/v1/auth/login` with `{"username": ..., "password": ...}`. Only rows with `app_user.status = 'active'` can log in.
- Password storage: PBKDF2-HMAC-SHA256, 200,000 iterations, random 16-byte salt, serialized as `pbkdf2_sha256$iterations$salt_b64$hash_b64`. Verification uses `hmac.compare_digest`.
- Token: a demonstration HMAC token, not a standard JWT. Format is `base64url({"user_id","exp"}) + "." + base64url(HMAC-SHA256)`, keyed by `AUTH_SECRET`; lifetime is 8 hours (`TOKEN_TTL_SEC`). There is no refresh token and no server-side revocation list.
- Request authentication: `get_current_user` parses `Authorization: Bearer <token>`. Invalid, tampered, or expired tokens return HTTP 401 with `code=1001`.
- The browser stores the token and user in `localStorage`. A 401 surfaces as an error in the page; there is no central redirect back to the login screen.

Two consequences are easy to miss:

- Login checks `app_user.status`, but token validation checks only the signature, the expiry, and that the user row still exists (`chatbi/services/auth.py`). Disabling a user does not revoke tokens already issued: they keep working until they expire, for up to 8 hours.
- `AUTH_SECRET` defaults to `change-me` in `chatbi/config.py`. Replace it outside local demonstrations.

## 3. Roles and resource ownership

Roles are stored in `role`, assignments in `user_role` (many-to-many). The seeded roles are:

| Role code | Purpose in the demo |
|---|---|
| `admin` | System administrator; always passes role guards |
| `semantic_admin` | Semantic layer / ontology administration |
| `analyst` | Data queries, insight attribution, feedback |
| `viewer` | Viewer role; chat entry is restricted as detailed below |
| `region_east` | Custom role that carries the East-region row policy |

The guard is `require_roles(*roles)` in `chatbi/api/deps.py`: `admin` always passes; otherwise the user's role set must intersect the required set. A rejection is HTTP 403 with `code=1003`.

Endpoint guards as implemented:

| Endpoint (prefix `/api/v1`) | Guard | Notes |
|---|---|---|
| `POST /auth/login` | anonymous | Returns token and user |
| `GET /auth/me` | any authenticated user | Returns roles and aggregated policies |
| `POST /chat/sessions`, `GET /chat/sessions` | any authenticated user | Own sessions only in the list |
| `GET /chat/sessions/{id}` | owner | Another user's or unknown session returns 403 |
| `DELETE /chat/sessions/{id}` | owner or `admin` | Also drops the session's short-term context |
| `POST /chat/ask` | any authenticated user without the viewer restriction | Rejected with 403 when the roles include `viewer` but neither `analyst` nor `admin` |
| `POST /chat/clarify` | any authenticated user | A pending card can only be completed by its creator |
| `POST /insight/attribute` | any authenticated user | Includes `viewer` |
| `GET /insight/{report_id}` | owner or `admin` | |
| `GET /history/sessions` | any authenticated user | Own sessions only |
| `POST /history/answers/{id}/rerun` | owner | |
| `POST /feedback` | any authenticated user | |
| `GET /feedback/queue` | `admin`, `semantic_admin` | Pending 👎 items |
| `GET/POST/DELETE /memories*`, `POST /memories/chart-switch` | any authenticated user | Own memories only; includes `viewer` |
| `GET /semantic/summary` | `admin`, `semantic_admin`, `analyst` | Version and object counts |
| `GET /semantic/{terms,metrics,dimensions,rules,events}` | `admin`, `semantic_admin` | Object browsing |
| `GET/POST /semantic/debug/link` | `admin`, `semantic_admin` | Linking debug |
| `GET /sys/users`, `GET /sys/audit-logs` | `admin` | |

Ownership rules that are enforced inside services rather than by a route guard:

| Resource | Rule |
|---|---|
| Chat session list and detail | Filtered by `user_id` |
| Chat session deletion | Owner or `admin` |
| Insight report read | Owner or `admin` |
| Answer rerun | Owner only |
| Chart switch | The answer's `query_log.user_id` must match the caller |
| Memory read/delete/clear | Every query filters `user_id`; another user's memory id raises `PermissionError` |
| Pending clarification | The pending card records its `user_id`; another user gets a `FORBIDDEN` error event |

Boundaries worth stating plainly:

- An account with the `viewer` role but neither `analyst` nor `admin` cannot start a new question (`POST /chat/ask`), but it can still call `POST /insight/attribute`, `GET /history/*`, `POST /feedback`, and the memory APIs. "Viewer" therefore means "no chat entry point", not "read-only everywhere".
- `admin` bypasses role guards, not data policies. It simply has no `data_policy` rows, so its queries are unfiltered.
- Errors are not uniformly shaped: REST errors use `{"code": int, "message": ..., "trace_id": ...}`, while streamed chat errors use string codes such as `SQL_FORBIDDEN` or `CLARIFY_EXPIRED`.

## 4. Data policies

`data_policy` (see `chatbi/semantic/schema.py`) is attached to a role, not to a user. A user's effective policies are the union over all of their roles with `status = 'active'`.

| Field | Meaning |
|---|---|
| `policy_type` | `dataset`, `row`, or `column` |
| `dataset_id` | Dataset allowed by a `dataset` policy |
| `model_id`, `dimension_id` | Row policy target (the gateway derives the table from the dimension, not from `model_id`) |
| `allowed_values` | Row policy allow-list: a JSON array of technical values, e.g. `["HD"]` |
| `column_name`, `column_action`, `mask_rule` | Column policy: column name, `hide` or `mask`, masking rule |
| `status` | `active` or `invalid` |

### 4.1 Dataset policies: declared, not enforced

`get_user_policies` collects `dataset_ids`, and `/auth/me` returns them under `policies`. That is the whole effect.

- SemQL validation only checks that the dataset exists in the published snapshot (`chatbi/query/semql.py`).
- The compiler and the gateway never compare the query's dataset with the user's `dataset_ids`.
- `FORBIDDEN_DOMAIN` is a canned reply emitted when the LLM's intent classification chooses that label; it is not an enforcement point.
- The practical fallback is the gateway's physical-table allowlist: only `orders`, `order_items`, `stores`, and `customers` can be read.

With the single seeded dataset (`ds_sales`) this is not observable in the demo. Do not describe dataset-level permission as enforced.

### 4.2 Row policies: injected into the AST

The gateway (`chatbi/query/gateway.py`) turns each row policy into a `WHERE` predicate:

1. Resolve the policy's `dimension_id` to a published dimension, take its physical expression and owning table (`dim_region` resolves to `stores.region_name` on `stores`).
2. If that table already appears in the query, add the predicate directly:

   ```sql
   ... WHERE stores.region_name IN ('HD')
   ```

3. If it does not, find a path through the semantic-layer relationship graph (BFS) from an anchor table to the controlled table and nest subqueries outward. With `orders` as the anchor:

   ```sql
   ... WHERE orders.store_id IN (
         SELECT stores.store_id FROM stores WHERE stores.region_name IN ('HD'))
   ```

   Longer chains nest further (`order_items -> orders -> stores`, for a query that references only `order_items`); in practice the compiler always joins `orders`, so the `orders -> stores` form is the one you will see.
4. Fail closed. An unknown or retired dimension, an empty allow-list, or no path to an anchor table produces `1 = 0`: the query returns nothing instead of leaking rows. In the main query path, each case is reported in the applied-rules notes; insight queries do not surface gateway notes.

Aggregation semantics:

- Multiple row policies on the same dimension are merged as a union of allowed values; multiple roles therefore widen visibility.
- Values must be technical values (`HD`, `HB`, ...), not display names (`华东`).
- Because a nested `IN (SELECT ...)` is used, a row policy also applies to queries that never mention the controlled dimension.

### 4.3 Column policies: hide or mask

Column policies match column references (`exp.Column` nodes, including those in `WHERE` and `ORDER BY`) by column name, not by semantic dimension id:

- `hide`: any reference to the column aborts the query with `COLUMN_FORBIDDEN` ("no permission to view this column").
- `mask` with rule `phone`: the column expression is replaced by `substr(col,1,3) || '****' || substr(col,-4)`.
- `mask` with any other rule: replaced by `'**' || substr(col,-2)`.

No column policy is seeded, so this path is exercised by unit tests only. The insight engine has one extra safeguard: dimensions whose physical expression ends with a hidden column are removed from the attribution drill-down pool, so a hidden attribute cannot be inferred indirectly.

## 5. The SQL security gateway

`check_and_rewrite_detail(sql, policies, snapshot)` in `chatbi/query/gateway.py` runs a fixed sequence:

1. Parse with sqlglot in the SQLite dialect; a parse failure raises `SQL_PARSE`.
2. Whitelist check:
   - exactly one `SELECT` statement (a `WITH ... SELECT` is allowed);
   - forbidden nodes: `INSERT`, `UPDATE`, `DELETE`, `DROP`, `ALTER`, `CREATE`, `TRUNCATE`, `GRANT`, `SET`, `MERGE`, `PRAGMA`, `ATTACH`, `DETACH`, `COMMAND`;
   - forbidden functions: `load_extension`, `readfile`, `writefile`, `file`, `url`;
   - physical tables limited to `orders`, `order_items`, `stores`, `customers`; CTE names are exempt so `WITH` clauses still work.
3. Apply row predicates, then column hide/mask.
4. Cap `LIMIT`: a missing limit is added as 10000, and a larger limit is reduced to 10000.

Execution is read-only: the business database is opened through a read-only SQLite URI (`chatbi/db.py`), the executor (`chatbi/query/executor.py`) interrupts queries that exceed 30 seconds of wall-clock time and returns at most 10000 rows plus a `truncated` flag.

A gate rejection raises `SqlForbiddenError` and is not retried. Note that the gateway re-serializes the AST, so the final SQL text is normalized (function-name case, explicit `AS`, added `LIMIT`) and is not byte-identical to the compiler output.

## 6. Insight path

Insight attribution does not have a separate data path:

- `chatbi/services/chat_service.py` builds the user's row and column policies into one list and passes it to the base-period detection and drill-down queries.
- `chatbi/insight/fluctuation.py` and `chatbi/insight/attribution.py` compile SemQL and call the same gateway for every internal SQL, so row predicates are applied to period A, period B, per-dimension breakdowns, and monthly series.
- Consequence: an account with a row policy gets insight numbers computed only inside its own scope. The comparison is internally consistent, but it is not a company-wide comparison.
- A hidden column's dimension is excluded from the drill-down pool (see 4.3).

`POST /insight/attribute` persists the report in `insight_report` with the caller's `user_id`; `GET /insight/{report_id}` is limited to the owner or `admin`.

## 7. Audit and trace

| Mechanism | Location | Content |
|---|---|---|
| `audit_log` | `chatbi/api/deps.py` `audit()`, called from `routes_auth.py` and `routes_memory.py` | `login`, `login_failed` (user `anonymous`), `memory_upsert`, `memory_delete`, `memory_clear`, `memory_chart_switch`; includes actor, action, target, detail, and client IP |
| `query_log` | `chatbi/services/query_service.py` | Question, matched elements, SemQL, final SQL (after row injection), applied rules, semantic version, retry count, status, error code, elapsed ms; failures are logged too |
| `llm_call_log` | `chatbi/llm.py` | Task, model, prompt, response, latency, and error per LLM call |
| `answer` snapshot | `chatbi/services/history.py` | Chart type and metadata, column roles, first 200 result rows, row count, summary |
| UI 留痕核验 (trace panel) | `chatbi/api/static/index.html` | SQL, SemQL, matching explanation, applied rules, memory usage, semantic version |

The matching explanation includes a `数据范围` (data scope) entry derived from the caller's row policies, so an East-region user sees the restriction next to the answer.

Audit coverage is partial, and the code is the source of truth:

- Rejected queries and permission hits are **not** written to `audit_log`, even though a schema comment mentions `query_forbidden`.
- Memory commands typed into chat write `user_memory_event` but not `audit_log`; only the REST memory routes audit.
- `user_memory_event` (created / confirmed / observed / promoted / applied / superseded / deleted) has no read API and no UI; inspect it through the metadata database.
- `GET /sys/audit-logs` (admin) returns `audit_log` rows only.

## 8. Seeded users and the row policy example

The four seeded accounts share the demo password `Init@123456`; do not reuse it.

| Account | Roles | Effective data policies |
|---|---|---|
| `admin` | `admin` | None (unfiltered) |
| `analyst` | `analyst` | `dataset = ds_sales` |
| `east_mgr` | `analyst`, `region_east` | `dataset = ds_sales`, row policy `dim_region IN ('HD')` |
| `viewer1` | `viewer` | `dataset = ds_sales`; chat entry blocked |

The seeded row policy (`chatbi/semantic/seed/ontology_seed.yaml`):

```yaml
- id: pol_row_east_region
  role_id: role_region_east
  policy_type: row
  model_id: mdl_stores
  dimension_id: dim_region
  allowed_values: [HD]
```

Expected behavior with fixed demo dates:

- `analyst` asks `2024年4月各大区的净销售额` and gets four region rows.
- `east_mgr` asks the same question and gets one 华东 row; the trace panel shows `数据范围：华东（大区）`, and the final SQL carries the East predicate.

## 9. Changing policies in a local demo

There is no admin API and no UI for `data_policy` create/update/delete. Policies are seeded from the YAML above by `python scripts/init_db.py`, and read from the metadata database on every request, so a change takes effect on the next request without restarting the server.

Two practical notes:

- `init_db.py` inserts by primary key and skips rows that already exist. Editing an existing policy in the YAML and re-running it does **not** update the stored row; change the row itself (or add a new id).
- `allowed_values` must be a JSON array of technical values. A bare string such as `HB` is not a valid filter and will not match anything.

The following is a **local demo customization note**, not a supported administrative interface:

```sql
-- local demo only: no runtime policy management API exists
INSERT INTO role (id, code, name) VALUES ('role_region_north', 'region_north', 'North region');
INSERT INTO data_policy (id, role_id, policy_type, model_id, dimension_id, allowed_values)
VALUES ('pol_row_north', 'role_region_north', 'row', 'mdl_stores', 'dim_region', '["HB"]');

-- Assign the role to the seeded analyst account.
INSERT INTO user_role (user_id, role_id) VALUES ('usr_analyst', 'role_region_north');
```

The dimension must exist in the published semantic snapshot; otherwise the row policy fails closed and the user sees no rows. To undo a demo change, delete the added rows — re-running `init_db.py` will not remove them.

## 10. Verification

```bash
# Gateway unit tests: whitelist, row injection, nesting, fail-closed, union, column hide/mask
python -m pytest tests/test_gateway.py -q

# Compilation, policies, and execution without LLM calls
python scripts/dev_query.py

# End-to-end regression with real model calls (costs tokens)
python scripts/run_pipeline_test.py
```

Prerequisites for all three: `DEEPSEEK_API_KEY` present in `.env` (project imports require it, even for offline checks) and an initialized metadata database with a published snapshot (`python scripts/gen_retail_data.py`, `python scripts/init_db.py`).

Manual comparison with fixed demo dates:

1. Log in as `analyst` and ask `2024年4月各大区的净销售额`. Expect four regions, and no `数据范围` entry in the trace panel.
2. Log out, log in as `east_mgr`, ask the same question. Expect a single 华东 row, `数据范围：华东（大区）` in the trace, and a row predicate in the SQL shown under 留痕核验.
3. As `viewer1`, confirm the chat input is rejected with a permission error, then confirm the same account can still call the memory panel or `POST /insight/attribute` (by design, see Limits).

## 11. Limits

1. Not production-grade. The token is a demo HMAC format without revocation, refresh, key rotation, MFA, rate limiting, or login lockout.
2. Disabled users keep working with an existing token until it expires (up to 8 hours), because token validation does not re-check `status`.
3. Dataset policies are not enforced in query, compilation, or the gateway; only the physical-table allowlist limits which tables can be read.
4. `viewer` is not read-only everywhere: it can call `POST /insight/attribute`, history, feedback, and memory APIs while being blocked from `POST /chat/ask`.
5. Row policies are expressed through published dimensions only. Arbitrary SQL filter expressions are not supported.
6. Multiple roles combine as a union. There is no "intersection" or deny rule; the gateway merges per-dimension values as a union, so supporting intersection would require changing both the policy loading and the gateway merge logic.
7. Column policies match column names in the compiled SQL. A query that emitted `SELECT *` would not expose `exp.Column` nodes and therefore would not match; current SQL always comes from the compiler, which enumerates columns.
8. Fail-closed behavior is intentional: a misconfigured policy yields empty results, not an error. Check the applied-rules notes when a user suddenly sees no data.
9. Audit is partial: rejected queries and permission hits are not recorded, and chat-issued memory commands bypass `audit_log`.
10. `query_log.policy_hash` is defined but never written, so there is no stored record of which policy set produced a given SQL.
11. Memory APIs and the intent guard are per user; `admin` has no cross-user memory view.
12. Single-process assumptions: pending clarifications and the in-process context cache live in one process, so multiple workers or instances will not share them.
13. CORS is `*` with credentials enabled, the static frontend is publicly served, and login is enforced only by the data APIs. Pydantic validation failures return FastAPI's default 422 instead of the unified error envelope.
14. Read-only enforcement relies on the SQLite read-only URI plus the gateway; it is not a database-level account separation.

## Related documents

- [Runbook](RUNBOOK.md) for setup, initialization, and operational checks.
- [Implementation Spec](IMPLEMENTATION_SPEC.md) for the pipeline and interface contracts.
- [Memory System](MEMORY_SYSTEM.md) for how user memory is stored and applied without bypassing these controls.
- [Memory System Features](MEMORY_SYSTEM_FEATURES.md) for the feature-level summary.
- [README](../README.md) for the project entry point.
