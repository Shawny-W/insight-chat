# InsightChat 权限控制说明

> 本文基于当前代码实现逐行核对写成，所有结论均可追溯到具体文件与行号。
> 一句话结论：**本项目能做到行级权限，且已经实现并默认启用**——华东区经理账号 `east_mgr` 只能看到华东大区（HD）的数据，全链路（查询 + 归因）生效。

## 1. 权限体系总览

权限控制分四层，自外向内依次为：

```
HTTP 请求
  └─ ① 认证层：Bearer token 校验（每个 API）
       └─ ② 功能权限层：角色守卫（哪些角色能调哪些 API）
            └─ ③ 数据权限层：数据策略 data_policy（数据集 / 行 / 列 三级）
                 └─ ④ 强制执行点：SQL 安全网关（所有业务 SQL 的唯一出口）
```

关键设计：**权限判定不依赖 LLM 自觉**。LLM 只生成 SemQL，最终 SQL 必须经过 `chatbi/query/gateway.py` 的 AST 级改写与拦截，行/列权限在网关处强制注入，任何路径（对话查询、归因引擎）都绕不开它。

## 2. 认证层：账号 + token

实现：`chatbi/security.py`、`chatbi/services/auth.py`、`chatbi/api/deps.py`

- **登录**：`POST /api/v1/auth/login`，账号密码校验（`auth.py:39`）。用户存 `app_user` 表，`status='active'` 才可登录。
- **密码存储**：标准库 PBKDF2-SHA256，20 万次迭代 + 随机 salt（`security.py:19-27`），格式 `pbkdf2_sha256$迭代次数$salt$hash`，校验用 `hmac.compare_digest` 防时序攻击。
- **token**：HMAC-SHA256 签名的简易 JWT 风格 token（`security.py:57`），格式 `base64url({"user_id","exp"}).签名`，有效期 **8 小时**（`TOKEN_TTL_SEC`），签名密钥为 `.env` 的 `AUTH_SECRET`。
- **请求鉴权**：FastAPI 依赖 `get_current_user`（`deps.py:28`）解析 `Authorization: Bearer <token>`，无效/过期返回 `401 / code=1001`。
- 前端 token 存 localStorage；过期后界面跳回登录页。

> 注意：token 为无状态演示级实现，无服务端吊销列表；`AUTH_SECRET` 生产环境必须更换（`AGENTS.md` 第 7 节）。

## 3. 功能权限层：角色守卫

实现：`deps.py:46` 的 `require_roles(*roles)`。

- 角色存 `role` 表，用户-角色关联存 `user_role` 表（多对多）。
- 种子角色（`semantic/seed/ontology_seed.yaml:463-468`）：

| 角色 code | 说明 |
|---|---|
| `admin` | 系统管理员，**守卫中恒放行**（`deps.py:51`） |
| `semantic_admin` | 语义管理员：语义层/Ontology 后台 |
| `analyst` | 数据分析师：查数 + 归因 + 反馈 |
| `viewer` | 查看者 |
| `region_east` | 自定义角色：华东区（行级策略载体） |

- API 权限矩阵（`chatbi/api/routes_*.py`）：
  - 对话/历史/洞察等：所有登录用户（仅 `get_current_user`）；
  - 语义层只读摘要：`admin / semantic_admin / analyst`（`routes_admin.py:15`）；
  - 语义层管理、反馈处理队列：`admin / semantic_admin`；
  - 用户列表、审计日志：仅 `admin`（`routes_admin.py:85,105`）。
- 越权返回 `403 / code=1003`。敏感操作写 `audit_log` 表（`deps.py:57` 的 `audit()`）。

## 4. 数据权限层：data_policy 三种策略

策略表 `data_policy`（DDL 见 `chatbi/semantic/schema.py:44-58`），**挂在角色上而非用户上**：

| 字段 | 说明 |
|---|---|
| `policy_type` | `dataset`（数据集级）/ `row`（行级）/ `column`（列级） |
| `dataset_id` | dataset 策略：可访问的数据集 |
| `dimension_id` + `allowed_values` | row 策略：维度 + 允许值（JSON 数组，存 tech_value 如 `["HD"]`） |
| `column_name` + `column_action` + `mask_rule` | column 策略：列名 + `hide`（拒绝）/ `mask`（脱敏）+ 脱敏规则 |
| `status` | `active` 生效 / `invalid` 停用 |

**聚合规则**（`auth.py:67` `get_user_policies()` / `query_service.py:23` `load_policies()`）：用户所有 active 角色的策略取**并集**；同一维度的多条行策略，允许值取**并集**（`gateway.py:164-170`）。

### 4.1 数据集级（dataset）

声明角色可访问哪个数据集。当前项目为单数据集（`ds_sales`），三个角色均已授权（种子 `yaml:485-487`）。
**已知限制**：服务端目前未对 `dataset_id` 做强校验——`FORBIDDEN_DOMAIN` 判定依赖意图识别环节（`chat_service.py:310`），属于提示级防护；数据集越权的兜底由网关表白名单承担（见 §5）。

### 4.2 行级（row）——本文重点，见 §6

### 4.3 列级（column）

实现于 `gateway.py:194-219`，默认种子未配置，机制已就绪：

- `hide`：SQL 中引用了被隐藏列 → 直接拒绝，抛 `COLUMN_FORBIDDEN`（"您没有查看该列的权限"）；
- `mask`：把该列表达式 AST 替换为脱敏表达式——`phone` 规则 `138****1234`，`name` 规则 `**伟`（`gateway.py:213-216`）。

## 5. 强制执行点：SQL 安全网关

实现：`chatbi/query/gateway.py`，入口 `check_and_rewrite_detail(sql, policies, snapshot)`（`gateway.py:240`）。**所有**到达业务库的 SQL 都必须过网关——对话查询在 `query_service.py:214` 调用，归因引擎的每一条内部 SQL 也调用（`insight/fluctuation.py:61`、`insight/attribution.py:68,83` 等）。

固定处理顺序：

1. **解析**：sqlglot 按 SQLite 方言解析为 AST，失败抛 `SQL_PARSE`；
2. **白名单校验**（`gateway.py:45-73`）：
   - 仅允许单条 `SELECT`（含 `WITH…SELECT`）；
   - 禁节点：`INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/GRANT/ATTACH/PRAGMA/Command` 等；
   - 禁函数：`readfile/writefile/load_extension` 等文件/网络探测函数；
   - **表白名单**：仅 `orders / order_items / stores / customers`（`BIZ_TABLES`），meta 库表、系统表一律拒绝——这也是数据集级越权的最后兜底；
3. **权限改写**：行谓词注入 → 列 hide/mask（见 §6）；
4. **LIMIT 兜底**：无 LIMIT 或 LIMIT > 10000 一律改写为 10000（`gateway.py:224-235`），执行器另有 30s 超时。

命中禁止项抛 `SqlForbiddenError`，不重试。

## 6. 行级权限：怎么做的、能不能做到

**结论：能，已实现、默认启用、有回归用例守护。**

### 6.1 演示配置（种子数据）

`semantic/seed/ontology_seed.yaml:476-494`：

```yaml
user_roles:
  - {user_id: usr_east_mgr, role_id: role_analyst}       # 能查数
  - {user_id: usr_east_mgr, role_id: role_region_east}   # 但受华东行策略约束
policies:
  - id: pol_row_east_region
    role_id: role_region_east
    policy_type: row
    dimension_id: dim_region        # 语义层维度「大区」
    allowed_values: [HD]            # tech_value，HD = 华东区
```

即：账号 `east_mgr`（密码 `Init@123456`）只能看华东大区的数据。

### 6.2 注入机制（`gateway.py:158-189`）

行策略不是简单拼字符串，而是基于语义层关系图的 AST 级谓词注入：

1. **维度定位**：策略里的 `dimension_id` 映射到语义层维度定义，取其物理表达式（`dim_region` → `stores.region_name`）和所属物理表；
2. **直接命中**：若该表已被 SQL 引用（如 `stores` 已在 JOIN 中），直接加 WHERE 谓词：
   ```sql
   ... WHERE stores.region_name IN ('HD')
   ```
3. **关系链嵌套**：若维度表未被引用（例如只查了 `orders`/`order_items`），沿语义层 relationship 无向图 **BFS 找路径**，从受控维度表向外层锚点表逐层嵌套子查询（`gateway.py:120-155`）：
   ```sql
   ... WHERE orders.store_id IN (
         SELECT stores.store_id FROM stores WHERE stores.region_name IN ('HD'))
   ```
   更长的链（如 `order_items → orders → stores`）继续向内嵌套；
4. **fail-closed（失败即关闭）**：维度已失效、允许值为空、找不到关系路径——一律注入 `1 = 0`，**宁可查无数据，不可越权放行**（`gateway.py:134,137,176-181`）。

### 6.3 全链路生效

- **对话查询**：`east_mgr` 问"2024年4月各大区的净销售额"，返回只有华东一行；「匹配解释」区明确展示"数据范围：华东（大区）"（`chat_service.py:_build_match_explain`）。回归用例 **q11** 守护此行为（`test_data/input/questions.json:22-23`）。
- **归因链路**：波动识别、贡献度下钻、事件关联的每一条内部 SQL 同样过网关注入行谓词，因此 `east_mgr` 问"为什么"类问题时，所有数字天然只在华东口径内计算，不会泄露其他大区数据。
- **归因下钻维度池**：被列策略 hide 的维度会被剔出归因下钻候选（`attribution.py:94-108`），防止通过归因侧面推断无权限的维度。

### 6.4 如何新增一条行级权限（纯数据操作，无需改代码）

```sql
-- 1. 建角色（或用现有角色）
INSERT INTO role (id, code, name) VALUES ('role_region_north', 'region_north', '华北区角色');
-- 2. 挂行级策略：仅华北区 HB
INSERT INTO data_policy (id, role_id, policy_type, dimension_id, allowed_values)
VALUES ('pol_row_north', 'role_region_north', 'row', 'dim_region', '["HB"]');
-- 3. 把角色赋给用户
INSERT INTO user_role (user_id, role_id) VALUES ('usr_xxx', 'role_region_north');
```

要点：`allowed_values` 存维度的 **tech_value**（如 `HD/HB`，即 `value_maps` 里的技术值）；`dimension_id` 必须是语义层已发布的维度，否则按 fail-closed 处理（查无数据）；策略即时生效（每次查询实时聚合，无需重启）。

## 7. 审计与留痕

| 机制 | 位置 | 内容 |
|---|---|---|
| `audit_log` 表 | `deps.py:57` | 敏感操作（管理后台等）记录操作人/动作/对象/IP |
| `query_log` 表 | `query_service.py` | 每次查询的 SemQL、最终 SQL（含注入后的权限谓词）、命中元素、耗时 |
| `llm_call_log` 表 | `llm.py` | 每次 LLM 调用的任务/模型/token 消耗 |
| 前端「留痕核验」 | `index.html` | 向用户展示匹配解释、已应用规则、**数据范围**（行权限说明） |

## 8. 安全边界与已知限制（诚实清单）

1. **token 为演示级**：无状态 HMAC 签名，无吊销机制，8 小时有效；生产应换标准 JWT/OAuth2 + 服务端会话。
2. **数据集级权限未做强校验**：目前依赖意图识别提示 + 网关表白名单兜底；接入多数据集时应在服务端补 `dataset_id ∈ 用户 dataset_ids` 的硬校验。
3. **行列权限粒度到"维度/物理列"**：行策略基于语义层维度定义（推荐，口径统一），不支持任意 SQL 表达式作为过滤条件。
4. **策略即时生效但缓存于单次请求**：用户角色变更后，下一次请求即生效，无需重启；但进程内会话上下文（如澄清记忆）不受影响。
5. **多策略并集语义**：同一用户多个角色的行策略取并集（能看到的数据变多），如需"交集"语义需改 `load_policies` 聚合逻辑。
6. **LLM 不接触权限判定**：权限全部在网关与聚合层确定性执行，LLM 只负责生成 SemQL——这是本项目的核心安全设计，改动时应保持。

## 9. 验证方式

```bash
# 单元测试（网关注入、fail-closed、列权限等）
python -m pytest tests/test_gateway.py -q

# 全链路回归（q11 为 east_mgr 行级权限用例）
python scripts/run_pipeline_test.py

# 手动对比：同一问题两个账号
#   analyst  问「2024年4月各大区的净销售额」→ 返回全部大区
#   east_mgr 问同一句话                     → 只返回华东（HD），并展示"数据范围：华东"
```
