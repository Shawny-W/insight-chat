[English](MEMORY_SYSTEM_FEATURES.md) | [简体中文](MEMORY_SYSTEM_FEATURES.zh-CN.md)

# Memory System: Features and Design Highlights

InsightChat remembers a small, specific set of things: what the current session is about, which charts and metric terms a user prefers, and which question/SemQL pairs that user has confirmed. This page is a short tour for users and reviewers. The authoritative behavior, APIs, storage model, and full limitation list are in [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md); the manual acceptance checklist is in [MEMORY_SYSTEM_TEST_SCRIPTS.md](MEMORY_SYSTEM_TEST_SCRIPTS.md).

## At a glance

| What a user can do | What the system does |
|---|---|
| Ask a follow-up such as "and North China?" | Reuses the recent metric, time range, and filters of the session |
| Say "use a line chart for trends from now on" | Stores a chart preference for the trend data family only |
| Click another chart type on an answer | Re-renders the stored result snapshot and learns the preference |
| Add "use a bar chart" to a question | Applies it once for that answer, without creating a durable preference |
| Confirm an ambiguous metric term repeatedly | Promotes the choice to a long-term mapping after three distinct sessions |
| Upvote a good answer | Adds that question and SemQL to the user's personal examples |
| Open "My memory" | Sees, deletes, or clears the user's own long-term memory |

The point is not to simulate a personality. It is to remove repeated BI work while keeping every answer auditable and every data access unchanged.

## Features you can feel

### 1. Follow-ups without repeating yourself

After "net sales by region in April 2024", a question like "and North China?" can inherit the metric and the time range. The session keeps up to five recent turns and uses them for contextual rewrite, intent classification, and generation.

### 2. Chart preferences by data scenario

A preference is stored per data family — trend, category comparison, proportion, single metric, detail, or other — not as one global chart type. "Line charts for trends" does not turn every pie chart into a line chart. A preference is applied only when the target type is still among the chart types the rule engine allows for that answer.

### 3. Switch the chart without re-running the query

Switching the chart on an answer reuses the stored columns and rows. No SemQL is regenerated and no SQL is executed, so the numbers cannot change during a cosmetic change. Because the stored snapshot holds at most 200 rows, a larger original result is explicitly marked as truncated after a switch.

### 4. One-off requests stay one-off

Adding "use a bar chart" to a question affects only that answer. "Use bar charts for category comparisons from now on" creates a durable preference. Negated phrasing such as "do not use a line chart" is not treated as a request, and a bare mention of "table" in ordinary text is not treated as a chart command.

### 5. Terminology learned across sessions

Choosing "net sales" on a clarification card resolves the current session immediately. The same choice in three distinct sessions promotes it to a long-term mapping, so later sessions stop asking. Repeated clicks in one session do not count. Until promotion, the candidate is never applied outside the session where it was chosen.

### 6. Your own confirmed Q&A as examples

An upvote on a successful data answer stores the question and its SemQL as a personal example. Later, similar questions can retrieve it as a few-shot example, and the answer says that personal history was used. A downvote revokes it. Retrieval is literal overlap, not vector similarity, and at most three examples are used in total.

### 7. Confidence decay that cannot self-renew

Long-term memories decay from their last confirmation date. Using a memory updates usage statistics only; it does not extend the memory's life. Below the configured confidence threshold, an active memory stops being applied, while remaining visible in the panel as decayed. Repeating or restating the preference refreshes it.

### 8. Control, visibility, and isolation

The memory panel shows each record's content, source, state, effective confidence, and use count, with per-item delete and clear-all. Memory is scoped by `user_id`: one user's preferences do not leak into another user's answers. Results also carry a short memory trace explaining which preference or example was applied.

## Design highlights

### 1. BI-scoped memory objects

The long-term layer stores only chart preferences, metric-term mappings, and personal question/SemQL pairs. There is no general fact memory, no procedural memory, and no cross-user or department sharing of chart or term preferences. Upvoted questions can still enter the shared exemplar pool (see Caveats). This narrow scope is what makes deterministic writes and predictable consumption possible.

### 2. Data shape decides whether a preference applies

The system derives a data family from the compiled column roles and SemQL, then looks up a preference for that family. A saved "line chart" preference cannot be forced onto a category breakdown, and an incompatible preference is ignored rather than producing a misleading chart.

### 3. Clarification is learned conservatively

A clarification choice is useful immediately but becomes durable only after consistent repetition across sessions. The gate is deliberate: one ambiguous click should improve the current session, not silently change future answers. Candidates are stored with a lower confidence and are never consumed.

### 4. Decay is anchored to confirmation, not use

The decay formula uses `last_confirmed_at`. Applying a preference increments `access_count` and `last_accessed_at` but never touches the anchor. A frequently used but long-unconfirmed preference therefore ages out honestly, while a fresh confirmation restores it.

### 5. Supersede instead of overwrite

Changing a preference marks the old version superseded with an end timestamp and inserts a new current version. Deletion is also logical. The current state stays simple, while history remains available to the API for auditing.

### 6. Deterministic, lightweight, and dependency-free

Writes come from strict command patterns, clarification choices, chart switches, and feedback. Recall is rule-based character overlap plus effective confidence. There is no vector store, no embedding call, no extra LLM extraction, and no new Python package, so behavior is reproducible and testable.

### 7. Degrade, never block

If a preference cannot be read, the rule-engine chart or the normal clarification card is used. If personal examples cannot be retrieved, shared examples are used. If a memory command fails, the user gets an explanatory text answer rather than a broken data query. Memory is an enhancement layer, not a dependency of the main pipeline.

### 8. Ownership and traceability at every step

Sessions, memories, pending clarifications, and chart switches are checked against their owner. The REST layer derives the user from the token, so a client cannot write memory for someone else or claim a different source or confidence. Trace notes and the memory panel make applied preferences visible, and the answer snapshot makes chart switching reproducible.

## What memory deliberately does not do

- It does not remember arbitrary conversational facts or user procedures.
- It does not retrieve by embeddings or vector similarity.
- It does not share chart or term preferences between users.
- It does not execute SQL, bypass the SQL gateway, or change row/column permissions.
- It does not let a candidate preference take effect before promotion.
- It does not physically erase deleted or superseded values; "forget" is logical.
- It does not re-query data when switching charts.

## Try it

```text
2024年3月到5月每月净销售额
换成柱状图
以后趋势都用折线图
记住销售额指净销售额
你记得我什么
忘掉销售额口径
忘掉全部记忆
```

These commands cover writing, using, viewing, and forgetting memory. Chart preferences can also be changed from the chart toolbar on any data answer.

## Caveats worth knowing

- Session context and pending clarifications live in one process. A pending clarification does not survive a restart, although completed context does.
- A browser reload loses the current `sessionId`; the server may still hold the context, but the UI does not restore the transcript.
- The UI clear button clears long-term memory only. The chat command `忘掉全部记忆` also resets the current session context.
- Chart switching renders at most the 200 rows stored with the answer.
- Memory endpoints require login but not a specific role, and administrators have no cross-user memory view.
- Chat memory commands are recorded as memory events but not in the security audit log.
- Upvoted questions can still be promoted into the shared exemplar pool after three distinct users, even though the personal memory record stays private.
- The memory panel has no edit or history view; changes are made by issuing a new command, and history is available only through the API.

## Where to go next

- [MEMORY_SYSTEM.md](MEMORY_SYSTEM.md) — authoritative design, storage, APIs, configuration, tests, and limitations
- [MEMORY_SYSTEM_TEST_SCRIPTS.md](MEMORY_SYSTEM_TEST_SCRIPTS.md) — step-by-step browser acceptance scenarios
- [PERMISSIONS.md](PERMISSIONS.md) — authentication, roles, and row/column enforcement
- [IMPLEMENTATION_SPEC.md](IMPLEMENTATION_SPEC.md) — module contracts
- [../README.md](../README.md) — project overview and quick start
