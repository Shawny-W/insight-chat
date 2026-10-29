[English](PERMISSIONS.md) | [简体中文](PERMISSIONS.zh-CN.md)

# 权限控制说明

本文说明当前代码**实际实现**的访问控制：认证、角色守卫、资源归属，以及 SQL 安全网关对行级/列级权限的强制执行。同时明确指出哪些只是声明、哪些是边界。

InsightChat 是单进程、本地可运行的演示项目。请把这些控制当作演示级控制，而不是生产级访问控制。具体的缺口清单见[已知限制](#11-已知限制)。

## 1. 控制发生的位置

```text
HTTP 请求
  -> 认证：Bearer token -> 用户                 （401 / code 1001）
  -> 角色守卫与资源归属校验                      （403 / code 1003）
  -> SemQL -> 确定性 SQL 编译
  -> SQL 安全网关：注入行谓词、列 hide/mask、LIMIT 兜底
  -> 只读 SQLite 执行：墙钟超时、行数上限
```

两个性质定义了这套设计：

- **LLM 不参与权限判定**。LLM 只产出受限的 SemQL；真正到达数据库的 SQL 由确定性编译器生成，再由 `chatbi/query/gateway.py` 改写。
- **没有路径可以绕过网关**。对话查询在 `chatbi/services/query_service.py` 调用网关；归因链路中的每一条内部 SQL 也分别在 `chatbi/insight/fluctuation.py` 与 `chatbi/insight/attribution.py` 调用网关。

## 2. 认证

实现位置：`chatbi/security.py`、`chatbi/services/auth.py`、`chatbi/api/deps.py`。

- 登录：`POST /api/v1/auth/login`，请求体 `{"username": ..., "password": ...}`。只有 `app_user.status = 'active'` 的记录可以登录。
- 密码存储：PBKDF2-HMAC-SHA256，20 万次迭代，随机 16 字节 salt，序列化格式为 `pbkdf2_sha256$迭代次数$salt_b64$hash_b64`。校验使用 `hmac.compare_digest`。
- Token：演示级 HMAC token，不是标准 JWT。格式为 `base64url({"user_id","exp"}) + "." + base64url(HMAC-SHA256)`，签名密钥取 `AUTH_SECRET`；有效期 8 小时（`TOKEN_TTL_SEC`）。没有 refresh token，也没有服务端吊销列表。
- 请求鉴权：`get_current_user` 解析 `Authorization: Bearer <token>`。token 无效、被篡改或已过期时返回 HTTP 401，`code=1001`。
- 浏览器把 token 与用户信息存在 `localStorage`。401 只会在当前操作处显示错误，页面没有统一的“跳回登录页”处理。

有两个容易被忽略的后果：

- 登录时校验 `app_user.status`，但 token 校验只检查签名、过期时间以及用户记录是否仍存在（`chatbi/services/auth.py`）。**禁用用户不会使其已签发的 token 立即失效**：在过期前的 8 小时内仍然可用。
- `chatbi/config.py` 中 `AUTH_SECRET` 的默认值是 `change-me`。本地演示之外必须替换。

## 3. 角色与资源归属

角色存 `role` 表，分配关系存 `user_role` 表（多对多）。种子角色如下：

| 角色 code | 在演示中的用途 |
|---|---|
| `admin` | 系统管理员；在角色守卫中恒放行 |
| `semantic_admin` | 语义层 / 本体（Ontology）管理 |
| `analyst` | 数据查询、归因洞察、反馈 |
| `viewer` | 查看者角色；聊天入口限制见下文 |
| `region_east` | 自定义角色，承载华东大区行级策略 |

守卫实现为 `chatbi/api/deps.py` 中的 `require_roles(*roles)`：`admin` 恒放行；否则用户角色集合必须与所需集合有交集。被拒绝时返回 HTTP 403，`code=1003`。

按当前实现整理的接口守卫：

| 接口（前缀 `/api/v1`） | 守卫 | 说明 |
|---|---|---|
| `POST /auth/login` | 匿名 | 返回 token 与用户信息 |
| `GET /auth/me` | 任意登录用户 | 返回角色与聚合后的策略 |
| `POST /chat/sessions`、`GET /chat/sessions` | 任意登录用户 | 列表中只包含自己的会话 |
| `GET /chat/sessions/{id}` | 本人 | 他人的会话或不存在的会话返回 403 |
| `DELETE /chat/sessions/{id}` | 本人或 `admin` | 同时清除该会话的短期上下文 |
| `POST /chat/ask` | 任意登录用户，受 viewer 限制 | 角色含 `viewer` 且不含 `analyst`、`admin` 时返回 403 |
| `POST /chat/clarify` | 任意登录用户 | 挂起中的澄清卡只能由其创建者完成 |
| `POST /insight/attribute` | 任意登录用户 | 包含 `viewer` |
| `GET /insight/{report_id}` | 本人或 `admin` | |
| `GET /history/sessions` | 任意登录用户 | 仅自己的会话 |
| `POST /history/answers/{id}/rerun` | 本人 | |
| `POST /feedback` | 任意登录用户 | |
| `GET /feedback/queue` | `admin`、`semantic_admin` | 待处理的 👎 反馈 |
| `GET/POST/DELETE /memories*`、`POST /memories/chart-switch` | 任意登录用户 | 仅自己的记忆；包含 `viewer` |
| `GET /semantic/summary` | `admin`、`semantic_admin`、`analyst` | 语义版本与对象计数 |
| `GET /semantic/{terms,metrics,dimensions,rules,events}` | `admin`、`semantic_admin` | 语义对象浏览 |
| `GET/POST /semantic/debug/link` | `admin`、`semantic_admin` | linking 调试 |
| `GET /sys/users`、`GET /sys/audit-logs` | `admin` | |

不依赖路由守卫、而是在服务层强制执行的归属规则：

| 资源 | 规则 |
|---|---|
| 会话列表与详情 | 按 `user_id` 过滤 |
| 删除会话 | 本人或 `admin` |
| 读取归因报告 | 本人或 `admin` |
| 重跑回答 | 仅本人 |
| 切换图表 | 回答对应 `query_log.user_id` 必须与调用者一致 |
| 记忆的读取 / 删除 / 清空 | 每次查询都过滤 `user_id`；他人记忆 id 会抛 `PermissionError` |
| 挂起中的澄清 | 挂起记录中包含 `user_id`；他人操作会收到 `FORBIDDEN` 错误事件 |

需要直说的边界：

- 角色含 `viewer` 且不含 `analyst`、`admin` 的账号无法发起新提问（`POST /chat/ask`），但仍可调用 `POST /insight/attribute`、`GET /history/*`、`POST /feedback` 以及记忆相关接口。所以“viewer”的含义是“没有对话入口”，不是“处处只读”。
- `admin` 绕过的是角色守卫，不是数据策略。它只是没有任何 `data_policy` 记录，因此查询不受行/列过滤。
- 错误格式并不统一：REST 错误为 `{"code": 整数, "message": ..., "trace_id": ...}`，而对话流内的错误事件使用字符串码，例如 `SQL_FORBIDDEN`、`CLARIFY_EXPIRED`。

## 4. 数据策略

`data_policy`（见 `chatbi/semantic/schema.py`）挂在角色上，不挂在用户上。用户的有效策略是其全部 `status = 'active'` 角色的策略并集。

| 字段 | 含义 |
|---|---|
| `policy_type` | `dataset`、`row` 或 `column` |
| `dataset_id` | `dataset` 策略允许的数据集 |
| `model_id`、`dimension_id` | 行级策略的目标；网关根据维度推导物理表，不使用 `model_id` |
| `allowed_values` | 行级策略允许值：技术值的 JSON 数组，例如 `["HD"]` |
| `column_name`、`column_action`、`mask_rule` | 列级策略：列名、`hide` 或 `mask`、脱敏规则 |
| `status` | `active` 或 `invalid` |

### 4.1 数据集策略：只声明，未强制执行

`get_user_policies` 会汇总 `dataset_ids`，`/auth/me` 把它们放在 `policies` 下返回。这就是它的全部作用。

- SemQL 校验只检查数据集是否存在于已发布快照中（`chatbi/query/semql.py`）。
- 编译器与网关从不把查询的数据集与用户的 `dataset_ids` 做比较。
- `FORBIDDEN_DOMAIN` 是 LLM 意图分类给出该标签后返回的固定话术，不是强制点。
- 实际起兜底作用的是网关的物理表白名单：只有 `orders`、`order_items`、`stores`、`customers` 可被读取。

当前只种子了一个数据集（`ds_sales`），所以这一点在演示中无法被观察到。**不要把数据集级权限描述成已强制执行。**

### 4.2 行级策略：注入到 AST

网关（`chatbi/query/gateway.py`）把每条行级策略转成 `WHERE` 谓词：

1. 把策略中的 `dimension_id` 解析为已发布维度，取其物理表达式与所属物理表（`dim_region` 解析为 `stores` 表上的 `stores.region_name`）。
2. 若该表已在查询中出现，直接追加谓词：

   ```sql
   ... WHERE stores.region_name IN ('HD')
   ```

3. 若未出现，则在语义层关系图上做 BFS，从锚点表找到受控表的路径，再向外逐层嵌套子查询。以 `orders` 为锚点：

   ```sql
   ... WHERE orders.store_id IN (
         SELECT stores.store_id FROM stores WHERE stores.region_name IN ('HD'))
   ```

   更长的链路会继续嵌套（`order_items -> orders -> stores`，用于只引用 `order_items` 的查询）；实际编译结果总会 join `orders`，所以运行中看到的是 `orders -> stores` 这种形态。
4. 失败即关闭（fail-closed）。维度不存在或已失效、允许值为空、找不到通往锚点表的路径，一律注入 `1 = 0`：宁可查不到数据，也不越权返回行。在主查询链路中，每种情况都会写进“已应用规则”说明；归因查询不会展示网关说明。

聚合语义：

- 同一维度上的多条行级策略按允许值取并集，因此多个角色只会扩大可见范围。
- 允许值必须是技术值（`HD`、`HB` 等），而不是展示名（`华东`）。
- 由于使用 `IN (SELECT ...)` 嵌套，即使查询完全没有提到受控维度，行级策略同样生效。

### 4.3 列级策略：hide 或 mask

列级策略按列名匹配 SQL 中的列引用（`exp.Column` 节点，包括 `WHERE`、`ORDER BY` 中的引用），不是按语义层维度 id：

- `hide`：SQL 中一旦引用该列，立即以 `COLUMN_FORBIDDEN` 拒绝（“您没有查看该列的权限”）。
- 规则为 `phone` 的 `mask`：把列表达式替换为 `substr(列,1,3) || '****' || substr(列,-4)`。
- 其它规则的 `mask`：替换为 `'**' || substr(列,-2)`。

种子数据中没有列级策略，因此这条路径仅由单元测试覆盖。归因引擎另有一道保护：物理表达式以被隐藏列结尾的维度会被移出归因下钻候选池，避免通过归因侧面推断被隐藏的属性。

## 5. SQL 安全网关

`chatbi/query/gateway.py` 的 `check_and_rewrite_detail(sql, policies, snapshot)` 按固定顺序执行：

1. 用 sqlglot 以 SQLite 方言解析；解析失败抛 `SQL_PARSE`。
2. 白名单校验：
   - 只允许一条 `SELECT`（允许 `WITH ... SELECT`）；
   - 禁止节点：`INSERT`、`UPDATE`、`DELETE`、`DROP`、`ALTER`、`CREATE`、`TRUNCATE`、`GRANT`、`SET`、`MERGE`、`PRAGMA`、`ATTACH`、`DETACH`、`COMMAND`；
   - 禁止函数：`load_extension`、`readfile`、`writefile`、`file`、`url`；
   - 物理表仅限 `orders`、`order_items`、`stores`、`customers`；CTE 名称豁免，因此 `WITH` 子句仍可正常工作。
3. 先注入行谓词，再做列级 hide/mask。
4. `LIMIT` 兜底：缺失时补 10000，超过 10000 时改为 10000。

执行端同样是只读的：业务库通过只读 SQLite URI 打开（`chatbi/db.py`），执行器（`chatbi/query/executor.py`）会中断超过 30 秒墙钟时间的查询，并且最多返回 10000 行，同时给出 `truncated` 标记。

网关拒绝会抛 `SqlForbiddenError`，不会重试。需要注意：网关会把 AST 重新序列化，所以最终 SQL 文本会被归一化（函数名大小写、显式 `AS`、补上的 `LIMIT` 等），与编译器输出不是逐字相同。

## 6. 归因链路

归因没有独立的数据通路：

- `chatbi/services/chat_service.py` 把用户的行级与列级策略合成一个列表，传给基期识别与下钻查询。
- `chatbi/insight/fluctuation.py` 与 `chatbi/insight/attribution.py` 编译 SemQL 后对每条内部 SQL 调用同一个网关，因此 A 期、B 期、按维度的拆分以及月度序列都会带上行谓词。
- 结果是：带行级策略的账号得到的归因数字只在其自身范围内计算。内部口径一致，但它不是全公司口径的对比。
- 被隐藏列对应的维度会从下钻候选池中剔除（见 4.3）。

`POST /insight/attribute` 会把报告写入 `insight_report`，`user_id` 为调用者；`GET /insight/{report_id}` 仅限本人或 `admin`。

## 7. 审计与留痕

| 机制 | 位置 | 内容 |
|---|---|---|
| `audit_log` | `chatbi/api/deps.py` 的 `audit()`，由 `routes_auth.py`、`routes_memory.py` 调用 | `login`、`login_failed`（用户记为 `anonymous`）、`memory_upsert`、`memory_delete`、`memory_clear`、`memory_chart_switch`；含操作人、动作、对象、详情与客户端 IP |
| `query_log` | `chatbi/services/query_service.py` | 问题、命中元素、SemQL、最终 SQL（含注入后的行谓词）、已应用规则、语义版本、重试次数、状态、错误码、耗时；失败同样落库 |
| `llm_call_log` | `chatbi/llm.py` | 每次 LLM 调用的任务、模型、prompt、响应、耗时与错误 |
| `answer` 快照 | `chatbi/services/history.py` | 图表类型与元数据、列角色、结果前 200 行、行数、摘要 |
| 前端「留痕核验」 | `chatbi/api/static/index.html` | SQL、SemQL、匹配解释、已应用规则、记忆使用、语义版本 |

匹配解释中会包含由调用者行级策略生成的 `数据范围` 条目，因此华东区经理能在答案旁看到限制说明。

审计覆盖是**不完整**的，以代码为准：

- 被拒绝的查询与权限命中**不会**写入 `audit_log`，尽管 schema 注释里提到过 `query_forbidden`。
- 在对话中输入的记忆命令只写 `user_memory_event`，不写 `audit_log`；只有记忆的 REST 接口会写审计。
- `user_memory_event`（created / confirmed / observed / promoted / applied / superseded / deleted）没有读取接口、也没有前端入口，只能直接查元数据库。
- `GET /sys/audit-logs`（仅 `admin`）只返回 `audit_log`。

## 8. 种子用户与行级策略示例

四个种子账号共用演示密码 `Init@123456`，不要复用。

| 账号 | 角色 | 生效的数据策略 |
|---|---|---|
| `admin` | `admin` | 无（不过滤） |
| `analyst` | `analyst` | `dataset = ds_sales` |
| `east_mgr` | `analyst`、`region_east` | `dataset = ds_sales`，行级策略 `dim_region IN ('HD')` |
| `viewer1` | `viewer` | `dataset = ds_sales`；对话入口被拒 |

种子中的行级策略（`chatbi/semantic/seed/ontology_seed.yaml`）：

```yaml
- id: pol_row_east_region
  role_id: role_region_east
  policy_type: row
  model_id: mdl_stores
  dimension_id: dim_region
  allowed_values: [HD]
```

使用固定的演示日期时，预期行为是：

- `analyst` 问 `2024年4月各大区的净销售额`，得到四个大区的行。
- `east_mgr` 问同一句话，只得到华东一行；留痕核验中显示 `数据范围：华东（大区）`，最终 SQL 中带有华东谓词。

## 9. 在本地演示中调整策略

`data_policy` 没有管理接口，前端也没有增删改入口。策略由 `python scripts/init_db.py` 从上面的 YAML 导入，并在**每次请求**时从元数据库读取，因此改动会在下一次请求生效，无需重启服务。

两个实践提醒：

- `init_db.py` 按主键做插入，已存在的记录会被跳过。修改 YAML 中已有策略后重新执行**不会**更新库里的记录；要改就改这一行记录（或使用新的 id）。
- `allowed_values` 必须是技术值的 JSON 数组。像 `HB` 这样的裸字符串不是有效过滤条件，匹配不到任何数据。

下面这段仅用于**本地演示定制**，不是受支持的管理接口：

```sql
-- local demo only: no runtime policy management API exists
INSERT INTO role (id, code, name) VALUES ('role_region_north', 'region_north', 'North region');
INSERT INTO data_policy (id, role_id, policy_type, model_id, dimension_id, allowed_values)
VALUES ('pol_row_north', 'role_region_north', 'row', 'mdl_stores', 'dim_region', '["HB"]');

-- Assign the role to the seeded analyst account.
INSERT INTO user_role (user_id, role_id) VALUES ('usr_analyst', 'role_region_north');
```

维度必须存在于已发布的语义快照中，否则该行级策略会失败即关闭，用户看不到任何行。回滚演示改动需要删除新增的记录——重新执行 `init_db.py` 不会移除它们。

## 10. 验证方式

```bash
# Gateway unit tests: whitelist, row injection, nesting, fail-closed, union, column hide/mask
python -m pytest tests/test_gateway.py -q

# Compilation, policies, and execution without LLM calls
python scripts/dev_query.py

# End-to-end regression with real model calls (costs tokens)
python scripts/run_pipeline_test.py
```

三者共同的前置条件：`.env` 中存在 `DEEPSEEK_API_KEY`（项目导入即需要，离线检查也不例外），以及已初始化、含已发布快照的元数据库（`python scripts/gen_retail_data.py`、`python scripts/init_db.py`）。

使用固定演示日期的人工对比：

1. 以 `analyst` 登录，问 `2024年4月各大区的净销售额`。预期返回四个大区，留痕核验中**没有** `数据范围` 条目。
2. 退出后以 `east_mgr` 登录，问同一句话。预期只有华东一行，留痕核验中出现 `数据范围：华东（大区）`，且留痕中的 SQL 带有行谓词。
3. 以 `viewer1` 登录，确认对话输入被权限错误拒绝；再确认该账号仍可访问记忆面板或调用 `POST /insight/attribute`（这是当前设计，见「已知限制」）。

## 11. 已知限制

1. **不是生产级实现。** token 是演示级 HMAC 格式，没有吊销、续期、密钥轮换、多因素认证、限流或登录锁定。
2. **禁用用户的存量 token 在过期前仍可用**（最长 8 小时），因为 token 校验不会复查 `status`。
3. **数据集策略在查询、编译、网关中均未强制执行**；限制可读表的只有物理表白名单。
4. **`viewer` 并非处处只读**：它可以调用 `POST /insight/attribute`、历史、反馈与记忆接口，只是被挡在 `POST /chat/ask` 之外。
5. **行级策略只能用已发布维度表达**，不支持任意 SQL 过滤表达式。
6. **多角色按并集合并**，没有交集或拒绝规则；网关对同一维度的允许值取并集，因此若需要交集语义，必须同时修改策略加载与网关的合并逻辑。
7. **列级策略按编译后 SQL 的列名匹配**。若某条 SQL 使用 `SELECT *`，就不会出现 `exp.Column` 节点，因而匹配不到；当前 SQL 都来自编译器，列会被逐一列出。
8. **失败即关闭是刻意设计**：策略配错的结果是空数据，而不是报错。用户突然查不到数据时，先看“已应用规则”说明。
9. **审计不完整**：被拒绝的查询与权限命中不会记录，对话中触发的记忆命令也不进 `audit_log`。
10. **`query_log.policy_hash` 已定义但从未写入**，因此没有保存“某条 SQL 由哪套策略产生”的记录。
11. **记忆接口与意图守卫按用户隔离**，`admin` 没有跨用户查看记忆的能力。
12. **单进程假设**：挂起的澄清与进程内上下文缓存只存在于一个进程，多 worker 或多实例不会共享。
13. **CORS 为 `*` 且开启了凭据**，静态前端公开可访问，登录只约束数据接口。Pydantic 参数校验失败返回 FastAPI 默认的 422，而不是统一错误包。
14. **只读依赖 SQLite 只读 URI 加网关**，不是数据库层面的账号隔离。

## 相关文档

- [运行手册](RUNBOOK.zh-CN.md)：安装、初始化与运行期检查。
- [实现说明](IMPLEMENTATION_SPEC.zh-CN.md)：主链路与接口契约。
- [记忆系统](MEMORY_SYSTEM.zh-CN.md)：用户记忆如何存储与使用，且不绕过上述控制。
- [记忆系统功能说明](MEMORY_SYSTEM_FEATURES.zh-CN.md)：功能层面的摘要。
- [README](../README.zh-CN.md)：项目入口。
