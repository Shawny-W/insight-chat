[English](MEMORY_SYSTEM_TEST_SCRIPTS.md) | [简体中文](MEMORY_SYSTEM_TEST_SCRIPTS.zh-CN.md)

# Memory System Manual Acceptance Checklist

This document is a **manual UI acceptance checklist** for the user-memory features: short-term session context, long-term chart preferences, metric mappings, personal Q&A exemplars, promotion, decay, forgetting, and user isolation. It is not an executable test: there is no script, fixture, or browser automation behind it, and it makes no claim about automated REST or UI coverage. Automated commands are listed separately in [section 16](#16-automated-checks-separate-from-this-checklist).

The authoritative description of how memory works is [Memory System](MEMORY_SYSTEM.md); setup and operational checks are in [Runbook](RUNBOOK.md).

## 0. Before you start

### 0.1 What you need

- A local demo that is already initialized and running, from the repository root:

  ```bash
  python scripts/gen_retail_data.py
  python scripts/init_db.py
  python scripts/check_llm.py
  python scripts/run_server.py
  ```

  `gen_retail_data.py` recreates the demo business database; never run it against data you want to keep.
- A working `DEEPSEEK_API_KEY` and model names that your account can actually call. Steps that ask a data question make **real LLM calls and cost tokens**; strict memory commands do not.
- A single server process. Pending clarifications and the in-process context cache are process-local; multiple workers will not share them.
- A desktop browser. The UI is a single page served at `http://127.0.0.1:8000/`.

### 0.2 Test accounts

| Account | Password | Use |
|---|---|---|
| `analyst` | `Init@123456` | Primary account for writes, reads, inheritance, and management |
| `east_mgr` | `Init@123456` | Second account for cross-user isolation; also has the East-region row policy |
| `admin` | `Init@123456` | Optional third account for the 3-user global exemplar path |
| `viewer1` | `Init@123456` | Cannot ask questions; useful for checking that memory APIs are open to any authenticated user |

Passwords are demo values from the seed data. All four accounts share the same demo password.

### 0.3 Side effects you are creating

Each run writes to the metadata database (`data/meta.db`):

- `chat_session`, `chat_message`, `chat_context` (session and short-term context);
- `query_log`, `answer`, `llm_call_log` (queries, result snapshots, model calls);
- `user_memory`, `user_memory_event` (long-term memories and their event history);
- `audit_log` (logins and REST memory operations);
- `feedback`, `feedback_history` (👍/👎);
- `query_exemplar` (shared global exemplars; see 10).

Deletion is logical: forgetting a memory keeps its historical value and events for auditing. The demo business database (`test_data/data/retail.db`) is not modified by any step here. Cleanup is described in [section 14](#14-cleanup).

### 0.4 Dates

The demo dataset covers **2023-06-01 through 2024-06-30**. Use explicit dates in every prompt, exactly as written below. Relative expressions such as "last month" are resolved against the machine's current date, which is outside the demo data range, and will return empty results. If a step returns no rows, first check that you did not shorten the date.

### 0.5 Where to look

| Signal | Where |
|---|---|
| Intent | Analysis timeline, first step "意图识别 识别为 …"; open "意图详情" for the raw event (`question`, `rewritten`, `time_range`, `grain`, `mentions`) |
| Clarification card | Rendered in the assistant message with clickable options |
| Assumptions | Yellow warning bars above the answer |
| Trace | Collapsible "留痕核验": SQL, SemQL, 匹配解释, 已应用规则, 记忆使用, semantic version |
| Memory state | Header memory icon, panel titled "我的记忆" |
| Reply text | Exact strings are quoted below; compare them literally |

Two useful distinctions:

- A memory command is intercepted before the query pipeline. Its intent step reads `识别为 MEMORY`, and the reply has no SQL block.
- A data question reads `识别为 DATA_QUERY` (or another data intent) and produces SQL in the trace.

### 0.6 Model-dependent steps

Clarification cards depend on the model emitting the ambiguous mention (for example `销售额`), and chart families depend on the generated SemQL. When an expected clarification card does not appear, retry that single step once before recording a failure. Never "fix" it by approving a different behavior.

### 0.7 Reset between sections

| Reset | Effect | Use when |
|---|---|---|
| Chat command `忘掉全部记忆` | Soft-deletes all long-term memories **and** resets the current session context | Starting a section from a clean state |
| Panel button "清空长期记忆" | Soft-deletes long-term memories only; session context survives | Testing the difference (8.3) |
| New session ("＋ 新会话" in the history drawer) | New `session_id`; long-term memories remain | Testing cross-session behavior |

## 1. Chat chart switching and cross-session chart preference

1. Clean up with `忘掉全部记忆`, then send:

   ```text
   2024年3月到5月每月净销售额
   ```

   Expected: 3 rows (month) and a line chart, with no "记忆使用" entry.

2. In the same conversation, send:

   ```text
   换成柱状图
   ```

   Expected:

   - intent step reads `识别为 MEMORY`; no new SQL runs, and the SQL in 留痕核验 is unchanged;
   - the chart switches to a bar chart;
   - the yellow assumption bar shows `已切换为柱状图，并记住您的趋势分析图表偏好`.

3. Click "＋ 新会话", then send the same question again:

   ```text
   2024年3月到5月每月净销售额
   ```

   Expected: a bar chart, with `已沿用您的趋势分析图表偏好：柱状图（可在记忆管理中修改）` in the yellow assumption bar and in 记忆使用. Note that the text says "可在记忆管理中修改", but the panel has no edit control (see 13).

4. Send:

   ```text
   你记得我什么
   ```

   Expected: a list containing `趋势分析 → 柱状图`.

5. Send:

   ```text
   忘掉图表偏好
   ```

   Expected: `已忘掉 1 条图表偏好记忆，历史审计记录保留。`

6. Click "＋ 新会话" and send the question again. Expected: back to the line chart.

## 2. Explicit long-term chart preferences

Send, one at a time:

```text
以后趋势都用折线图
```

Expected: `已记住：您在趋势分析场景偏好折线图，后续查询会自动沿用。`

```text
以后分类对比都用柱状图
```

Expected: `已记住：您在分类对比场景偏好柱状图，后续查询会自动沿用。`

```text
以后占比分析都用饼图
```

Expected: `已记住：您在占比分析场景偏好饼图，后续查询会自动沿用。`

Then send:

```text
查看我的记忆
```

Expected: a list with three entries (trend → line, category → bar, proportion → pie), followed by the hint about deleting entries in the panel or saying `忘掉全部记忆`.

Open the memory panel and check each row: content, source pill (`明确指定`), state (`生效中`), effective confidence as a percentage, and usage count.

## 3. A one-off chart request must not persist

1. Clean up with `忘掉全部记忆`.
2. Send:

   ```text
   2024年3月到5月每月净销售额，用柱状图展示
   ```

   Expected: a bar chart, and `按您当次要求使用柱状图（未写入长期偏好）` in the assumption bar and 记忆使用.

3. Click "＋ 新会话" and send:

   ```text
   2024年3月到5月每月净销售额
   ```

   Expected: the default line chart, proving the previous turn did not write a long-term preference.

## 4. Negation and ordinary use of "表格"

### 4.1 Negated chart types

```text
2024年4月各大区的净销售额，不要用饼图
```

Expected: the query runs and keeps the rule-selected bar chart; no `按您当次要求` note appears.

```text
2024年3月到5月每月净销售额，别再用折线图了
```

Expected: no `按您当次要求使用折线图` note. The default line chart may still be shown because the data is a time series; what matters is that no one-off request was detected.

### 4.2 "表格" as an ordinary noun

```text
这个表格里的数据是什么意思
```

Expected: a conversational or unsupported reply. It must not be treated as a "render as table" instruction, and it must not write a chart preference.

## 5. No chart to switch, and incompatible chart type

### 5.1 Switch before any query

In a new session, send directly:

```text
换成折线图
```

Expected: `还没有可切换的图表，请先完成一次数据查询。`

### 5.2 Incompatible target type

1. Send:

   ```text
   2024年4月各大区的净销售额
   ```

   Expected: 4 rows and a bar chart.

2. Send:

   ```text
   换成折线图
   ```

   Expected: `当前数据不支持折线图，可选：柱状图、条形图、饼图、表格`. The previous answer stays rendered, and no long-term preference is written (verify with `查看我的记忆`).

Note: the switchable types are exactly the ones offered as buttons on the chart card for that answer.

## 6. Long-term metric mapping

1. Clean up with `忘掉全部记忆`.
2. Send:

   ```text
   记住销售额指净销售额
   ```

   Expected: `已记住：「销售额」按「净销售额」查询。`

3. Click "＋ 新会话" and send:

   ```text
   2024年5月华东区的销售额是多少
   ```

   Expected:

   - no clarification card for 净销售额 / 毛销售额;
   - `沿用您的长期口径偏好：「销售额」按「净销售额」查询` in the assumption bar and 记忆使用;
   - SemQL in 留痕核验 uses `metric_net_sales`;
   - the value is about 140742.24 元 (deterministic demo data generated with seed 42; a different data build or dataset will differ).

4. Send:

   ```text
   2024年5月华东区的毛销售额是多少
   ```

   Expected: a gross-sales query (SemQL uses `metric_gross_sales`), not overridden by the long-term mapping.

5. Send:

   ```text
   忘掉销售额口径
   ```

   Expected: `已忘掉 1 条「销售额」口径记忆。`

6. Click "＋ 新会话" and repeat step 3. Expected: the clarification card appears again.

## 7. Promotion after three distinct sessions

A clarification choice starts as a candidate and is promoted to a long-term preference after the same choice appears in 3 **distinct sessions**. Repeating it inside one session must not count.

For each round below, click "＋ 新会话" first; never continue in the same conversation.

| Round | Prompt | Then |
|---|---|---|
| 1 | `2024年4月的销售额怎么样` | Select `净销售额` on the card |
| 2 | `2024年4月的销售额怎么样` | Select `净销售额` on the card |
| 3 | `2024年4月的销售额怎么样` | Select `净销售额` on the card |
| 4 | `2024年4月的销售额怎么样` | No card expected |

Expected after round 3: `已根据您在 3 个会话中的一致选择，将「销售额」的口径记为长期偏好` in the assumption bar.

Expected in round 4: the question is answered directly with `metric_net_sales`, and `沿用您的长期口径偏好：「销售额」按「净销售额」查询` appears in the trace. Check the panel: the mapping row now shows the source `澄清学习` and the state `生效中`.

Negative check (do it before the third distinct session): inside one session, answer the card and then ask the same question again. No new card appears (the session choice is reused, with `沿用本次会话的澄清选择：「销售额」按「净销售额」查询` in the trace), and the promotion counter must not advance: the panel still shows `待跨会话确认`.

## 8. Short-term context: restart, pending clarification, and clear semantics

### 8.1 Completed context survives a server restart

1. Send:

   ```text
   2024年4月各大区的净销售额
   ```

   Expected: 4 rows.

2. Restart the server in its terminal (Ctrl+C, then `python scripts/run_server.py`). **Do not reload the browser page, do not click "＋ 新会话", and do not pick a history entry.** The page keeps the current `session_id` in memory; a reload loses it and starts a new session on the next message.

3. After the server is back, send in the same page:

   ```text
   那华北呢
   ```

   Expected:

   - `question` in 意图详情 is `2024年4月华北区的净销售额` (with `rewritten: true`);
   - `time_range` is `2024-04-01` to `2024-04-30`;
   - the result is a single value of about 117719.74 元 (HB, net sales, demo data);
   - no metric or time clarification is requested.

4. Optional follow-up in the same session: send `记住销售额指净销售额`, then `那华北呢` again. Expected: the mapping is remembered explicitly and the follow-up still resolves to April, HB, net sales, without asking about the metric again.

### 8.2 Pending clarification does not survive a restart

1. Send:

   ```text
   2024年3月到5月净销售额的趋势
   ```

   Expected: a clarification card asking `请问按什么粒度查看？` with options 按日 / 按周 / 按月.

2. Restart the server, leaving the page and the card untouched.

3. Click `按月`.

   Expected: an error card `澄清已过期，请重新提问`. Ask the question again to continue; session context is unaffected.

### 8.3 Panel clear vs chat clear

1. Send `2024年4月各大区的净销售额`, then open the memory panel and click "清空长期记忆".

   Expected: the list becomes empty. The current session context is untouched.

2. In the same conversation, send `那华北呢`.

   Expected: it still inherits the April period and net sales; `question` in 意图详情 is still `2024年4月华北区的净销售额` (with `rewritten: true`).

3. Now send `忘掉全部记忆`, then `那华北呢` again.

   Expected: no inheritance. 意图详情 shows `question` as the unchanged `那华北呢` (with `rewritten: false`) and `time_range` as null. The turn afterwards usually resolves to an empty default-range query or asks you to restate the question; both are acceptable, as long as it does not resolve to the April, net-sales question.

## 9. Personal Q&A: 👍 and 👎

1. Send:

   ```text
   2024年4月各大区的净销售额
   ```

2. Click 👍 on that answer.

3. Click "＋ 新会话" and ask the identical question.

   Expected: `参考了您 1 条已确认的历史问答（仅本人可见）` in 记忆使用, and a row in the memory panel whose source is `点赞确认`.

4. Go back to the original answer, click 👎, then open a new session and ask the identical question again.

   Expected: the personal Q&A line no longer appears. 👎 removes the personal exemplar; it does not remove the global exemplar described in section 10.

5. Alternative cleanup: send `忘掉问答记忆`.

   Expected: `已忘掉 N 条个人问答对记忆，历史审计记录保留。` Chart and metric preferences are untouched.

## 10. Personal exemplars vs the shared global exemplar path

These are two different mechanisms and must not be conflated:

- 👍 immediately creates a **personal** exemplar for the voting user (`user_memory`, category `query_exemplar`, source `feedback`). It is recalled only for that user, and its trace line is `参考了您 N 条已确认的历史问答（仅本人可见）`.
- 👍 also counts toward a **shared** `query_exemplar` row (`source='user_feedback'`). That row only becomes active after 3 **distinct users** upvote answers to the same question, and it is then used as a few-shot example for everyone. There is no UI signal for this path, 👎 cannot undo it, and the UI has no way to inspect it.

Optional global-path check (writes shared state):

1. Log in as `analyst`, ask `2024年4月各大区的净销售额`, click 👍.
2. Log out, log in as `east_mgr`, ask the same question, click 👍. Expect one 华东 row due to the row policy; that is fine.
3. Log out, log in as `admin`, ask the same question, click 👍.
4. Inspect the metadata database (project Python environment):

   ```bash
   python - <<'PY'
   import sqlite3
   c = sqlite3.connect("data/meta.db")
   print(c.execute("SELECT nl_question, quality_score, is_active "
                   "FROM query_exemplar WHERE source = 'user_feedback'").fetchall())
   PY
   ```

   Expected: one row for that question with `quality_score = 3` and `is_active = 1`.

This shared row is not removable through the UI or the API. See [section 14](#14-cleanup).

## 11. User isolation

1. As `analyst`, send `以后趋势都用柱状图`, then `记住销售额指净销售额`.
2. Log out and log in as `east_mgr`. Ask:

   ```text
   2024年3月到5月每月净销售额
   ```

   Expected: a line chart. `analyst`'s bar preference is not inherited, and 记忆使用 shows no applied preference.

3. Ask:

   ```text
   2024年5月华东区的销售额是多少
   ```

   Expected: the 净销售额 / 毛销售额 clarification card still appears; `analyst`'s mapping is not inherited.

4. Log back in as `analyst` and confirm both preferences are still listed in the panel. Isolation is per `user_id`, in both directions.

## 12. Memory command edge cases

### 12.1 Missing data family

```text
以后都用折线图
```

Expected: `请说明偏好的数据场景，例如「以后趋势都用折线图」或「以后分类对比都用柱状图」。` Nothing is saved, and the answer does not fall back to a data query.

### 12.2 Unknown metric

```text
记住销售额指不存在的指标
```

Expected: `找不到唯一指标「不存在的指标」，请使用完整指标名，例如「净销售额」。` Nothing is saved.

### 12.3 Ordinary questions starting with 以后 / 今后

```text
以后 2024年3月到5月每月净销售额
```

```text
今后 2024年4月各大区的净销售额
```

Expected: neither is captured as a memory command — the intent step reads something other than `MEMORY` — and the turn proceeds as a data question. Query success itself depends on the model; the point of this check is that the `以后` / `今后` prefix does not hijack the request.

### 12.4 A one-off chart request is not a memory command

```text
2024年3月到5月每月净销售额，用柱状图展示
```

Expected: the intent is not `MEMORY`; the turn runs as a data query with a bar chart and writes no long-term memory.

## 13. Memory panel and display limits

With several memories present, open the panel and check:

- rows show content, source (`明确指定`, `切图学习`, `澄清学习`, `点赞确认`), state, effective confidence, and usage count;
- candidate memories show `待跨会话确认`;
- a newly promoted clarification mapping shows `生效中`, not `待跨会话确认`;
- deleting one row removes only that row, and the rest remain;
- "清空长期记忆" asks for confirmation and then shows the empty state;
- with no memories the panel shows the empty-state hint;
- if the API call fails (for example, stop the server and open the panel), the panel shows a read error and a 重试 button; restart the server, click 重试, and the list loads again.

Documented limits of the UI:

- There is no edit control. To change a preference, issue a new command; the new value supersedes the old one, which disappears from the current list but remains in history.
- There is no history view. Superseded and deleted rows are only available through `GET /memories?include_history=true`.
- Term rows display the raw `term_id` (for example `「销售额」→「term_net_sales」`), while the chat list command resolves the human-readable name.
- The chat list command shows at most 10 semantic memories; the panel lists all.
- The decay state `已衰减 · 不生效` is a display rule for active memories below the minimum effective confidence. With the default half-lives (365 days for explicit memories, 90 days for learned ones) you will not reach it during a single session; do not list it as a quick pass/fail item.
- Chart switching re-renders the stored answer snapshot, which contains at most the **first 200 rows**. If the original result had more rows, the switched chart is flagged as truncated. Aggregate results in the demo are well below this limit, so treat it as a stated boundary rather than an exercise.

## 14. Cleanup

1. For each account you used (`analyst`, `east_mgr`, `admin`), log in and send `忘掉全部记忆`, or open the panel and click "清空长期记忆". This is a logical delete; the rows and events remain in the metadata database for auditing.
2. Sessions and messages are not removable from the UI. If you need them gone, call `DELETE /api/v1/chat/sessions/{id}` per session, or reset the metadata database (next step).
3. For a pristine demo, stop the server, delete `data/meta.db`, re-run `python scripts/init_db.py`, and start the server again.

   This wipes **all** users' memories, sessions, feedback, audit rows, and global exemplars, and republishes the seed snapshot v1. Do not do it on a database whose contents must be preserved. The demo business database is unaffected.

## 15. Acceptance checklist

| Capability | Pass signal |
|---|---|
| Chat chart switch | Intent is `MEMORY`, no re-query, SQL unchanged, chart switches |
| Long-term chart preference | Same-family query in a new session uses the preferred chart; other users do not |
| One-off chart request | Applied for that turn only, with `未写入长期偏好` |
| Negation | A negated chart type is not forced; no false "per your request" note |
| Long-term metric mapping | No repeated clarification; SemQL uses the chosen metric |
| Clarification promotion | Promoted after 3 distinct sessions; same-session repeats do not count |
| Restart recovery | Completed context survives a restart on the same page; pending card does not |
| Panel clear vs chat clear | Panel clears long-term memories only; `忘掉全部记忆` also resets session context |
| Personal Q&A | Recalled after 👍, withdrawn after 👎, isolated per user |
| Global exemplar path | Active only after 3 distinct users 👍; no UI signal; not removable |
| Isolation | `east_mgr` inherits nothing from `analyst` |
| Memory commands | Unknown metric and missing family are rejected with guidance, without saving |
| False-positive guard | Ordinary `以后` / `今后` questions and the noun "表格" are not hijacked |
| Panel | List, per-item delete, clear, empty state, error state; no edit and no history |

## 16. Automated checks (separate from this checklist)

```bash
# Memory unit tests (isolated in-memory SQLite; the second file loads the real seeded snapshot)
python -m pytest tests/test_memory.py tests/test_clarify_memory.py -q

# Whole suite
python -m pytest tests/ -q

# Compilation, policies, and execution without LLM calls
python scripts/dev_query.py
```

Prerequisites and scope:

- The unit tests make **no LLM calls**, but importing the project requires `DEEPSEEK_API_KEY` in `.env`, and `tests/test_clarify_memory.py` requires an initialized metadata database with a published snapshot.
- Covered by the suite (selection): chart preference application and per-user isolation, family and available-type restrictions, one-off requests and negation, snapshot-based chart switching, supersede and soft delete, events, decay without access renewal, cross-session promotion, session-vs-long-term priority, context round-trip, expiry, corruption and ownership, restart simulation through cache clearing, LRU protection of pending clarifications, clarification authorization and forged options, personal exemplar recall and isolation, category clearing, strict command parsing, custom-time fallback.
- Not covered: REST routes, the UI, `audit_log`, the 👍-to-personal-exemplar integration, the chat command list/forget paths, a real restart against `data/meta.db`, `include_history`, 422 validation behavior, and live LLM/SSE flows.
- There is no script in `scripts/` that drives memory scenarios, and no browser automation. `scripts/run_pipeline_test.py` makes real model calls but does not assert memory behavior.

Use the automated checks as a regression gate for the deterministic core, and this checklist for the end-to-end behavior a user actually sees.

## Related documents

- [Memory System](MEMORY_SYSTEM.md) for storage, lifecycle, consumption rules, and limits.
- [Memory System Features](MEMORY_SYSTEM_FEATURES.md) for the feature-level summary.
- [Runbook](RUNBOOK.md) for initialization, server operation, and resets.
- [Permissions](PERMISSIONS.md) for who can call which endpoint, including the memory APIs.
- [README](../README.md) for the project entry point.
