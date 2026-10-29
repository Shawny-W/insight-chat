[English](IMPLEMENTATION_SPEC.md) | [简体中文](IMPLEMENTATION_SPEC.zh-CN.md)

# InsightChat 实现说明书

本文说明当前 InsightChat 代码的实际工作方式。它是一份当前实现概览，不是冻结的契约：代码、种子数据、脚本和测试才是事实来源。当旧设计说明与代码不一致时，以本文（已按代码核对）为准。

相关文档：

- [../README.zh-CN.md](../README.zh-CN.md) —— 项目概览与快速开始。
- [RUNBOOK.zh-CN.md](RUNBOOK.zh-CN.md) —— 安装、初始化、运行与故障排查。
- [MEMORY_SYSTEM.zh-CN.md](MEMORY_SYSTEM.zh-CN.md) 与 [MEMORY_SYSTEM_FEATURES.zh-CN.md](MEMORY_SYSTEM_FEATURES.zh-CN.md) —— 用户级记忆的设计与行为。
- [MEMORY_SYSTEM_TEST_SCRIPTS.zh-CN.md](MEMORY_SYSTEM_TEST_SCRIPTS.zh-CN.md) —— 记忆系统的验证场景。
- [PERMISSIONS.zh-CN.md](PERMISSIONS.zh-CN.md) —— 认证、角色与策略执行的详细说明。

## 1. 范围与系统形态

InsightChat 是一个可在本地运行的单进程对话式 BI 应用。用户用中文提问，应用先在一套小型语义层上完成链接，再让大模型输出受约束的 SemQL JSON；随后以确定性方式把 SemQL 编译为 SQLite SQL，经过安全网关检查后只读执行，返回图表与留痕信息。“为什么”类问题会先经过确定性的归因引擎，再由模型生成结论表述。

当前实现刻意保持本地化：

- 一个 FastAPI 进程、一个元数据 SQLite 库、一个演示业务 SQLite 库。
- DeepSeek 的 OpenAI 兼容 API 是后端唯一调用的外部服务。
- 不使用 PostgreSQL、ClickHouse、Redis、Celery、向量库、Embedding 模型或 React 构建流程。
- 前端是由 FastAPI 托管的静态单页。

本文不描述生产部署。第 20 节列出了本地设计带来的限制。

## 2. 技术栈

| 层次 | 技术 | 位置 |
| --- | --- | --- |
| HTTP 服务 | FastAPI、uvicorn、sse-starlette | `chatbi/api/` |
| 模型客户端 | `openai` SDK，指向 DeepSeek | `chatbi/llm.py` |
| 元数据存储 | SQLite | `data/meta.db` |
| 演示业务存储 | SQLite，只读打开 | `test_data/data/retail.db` |
| 语义层 | 由 YAML 种子加载为 SQLite 快照 | `chatbi/semantic/seed/ontology_seed.yaml` |
| 中文分词 | jieba，加自建自定义词典 | `chatbi/semantic/dictionary.py`、`data/dict/` |
| SQL 解析与改写 | sqlglot，SQLite 方言 | `chatbi/query/gateway.py` |
| API 模型 | pydantic v2 | `chatbi/query/semql.py` 及各 API 路由模块 |
| 前端 | 原生 JavaScript，ECharts 走 CDN | `chatbi/api/static/index.html` |
| 测试 | pytest | `tests/` |

要求 Python 3.10 或更高版本，推荐 3.11。

## 3. 端到端架构

一次数据提问会经过下列路径。顺序很重要：指代改写发生在意图识别**之前**，而记忆命令检查又发生在两者之前。

```text
用户提问
  -> 记忆命令检查（严格模式匹配；绕过 NL2SQL）
  -> 指代改写（light 模型，仅在有会话上下文时）
  -> 意图识别（light 模型，规则兜底）
  -> Schema Linking（词典路 + mention 路 + light 模型精排）
  -> 歧义检测，必要时生成澄清卡片
  -> SemQL 生成（heavy 模型，三级提示词注入，最多三次尝试）
  -> 确定性后修正，然后对快照做 SemQL 校验
  -> 确定性 SQL 编译（SQLite 方言）
  -> SQL 安全网关（AST 校验、行级与列级策略、限额）
  -> 只读执行（30 秒超时，最多 10,000 行）
  -> 图表规则与记忆个性化
  -> 摘要、匹配解释、留痕
  -> SSE result 事件
```

两条相关路径复用同一批基础模块：

- **洞察路径。** `INSIGHT_WHY` 与 `INSIGHT_ANOMALY_CHECK` 意图的每一次标量查询和下钻查询，都走同一套编译器、网关和执行器，因此归因数值与用户直接查询的口径一致。
- **图表切换。** `POST /memories/chart-switch` 复用该回答已保存的快照，不重新执行 SQL。

模型从不编写可执行 SQL。它只编写 SemQL 这一收窄的 JSON DSL；编译器是确定性的，网关会在执行前重新检查编译产物。

## 4. 目录结构

```text
chatbi/
├── chatbi/
│   ├── config.py              环境加载、路径、阈值
│   ├── db.py                  读写元数据库连接、只读业务库连接
│   ├── llm.py                 chat_json / chat_text 封装、重试、llm_call_log
│   ├── security.py            密码哈希、令牌签名
│   ├── semantic/
│   │   ├── schema.py          元数据库的 CREATE TABLE IF NOT EXISTS
│   │   ├── store.py           快照加载、索引、published 版本处理
│   │   ├── seed.py            种子导入与 Evidence 执行
│   │   ├── dictionary.py      jieba 词典构建与加载
│   │   └── seed/ontology_seed.yaml
│   ├── nlp/
│   │   ├── prompts.py         提示词模板与固定文案
│   │   ├── rewrite.py         指代改写
│   │   ├── intent.py          意图识别与时间解析
│   │   ├── linking.py         Schema Linking
│   │   └── clarify.py         确定性歧义检测与卡片
│   ├── query/
│   │   ├── semql.py           SemQL 模型与校验
│   │   ├── generator.py       提示词组装与生成重试
│   │   ├── compiler.py        SemQL 到 SQLite SQL
│   │   ├── gateway.py         SQL 安全网关
│   │   └── executor.py        带超时的只读执行
│   ├── insight/
│   │   ├── fluctuation.py     两期对比与显著性
│   │   ├── attribution.py     贡献度下钻与事件关联
│   │   └── narrative.py       模型结论，失败时模板兜底
│   ├── chart/rules.py         图表类型选择
│   ├── services/
│   │   ├── chat_service.py    SSE 编排与上下文
│   │   ├── query_service.py   link -> generate -> compile -> gateway -> execute
│   │   ├── memory.py          用户级记忆
│   │   ├── history.py         会话、回答、重跑
│   │   ├── feedback.py        投票、示例问答对、审核队列
│   │   └── auth.py            登录与策略聚合
│   └── api/
│       ├── main.py            应用工厂、启动钩子、静态挂载
│       ├── deps.py            Bearer 解析、角色守卫、错误包
│       ├── routes_*.py
│       └── static/index.html
├── scripts/                   初始化、服务、连通性、开发、回归
├── tests/                     pytest 测试集
├── test_data/                 演示库、问题样例、预期样例、输出
└── data/                      运行时元数据库与生成的词典
```

稳定的入口是上述模块路径，而不是精确行号。应用工厂是 `chatbi.api.main:create_app`，模块级 ASGI 应用是 `chatbi.api.main:app`。

## 5. 语义层与快照

### 5.1 种子与发布

`scripts/init_db.py` 读取 `chatbi/semantic/seed/ontology_seed.yaml`，导入与版本无关的基础数据，并发布唯一的语义层快照 v1。`chatbi/semantic/store.py` 会把已发布版本加载为内存中的 `SemanticSnapshot`，并构建两个索引：

- 词到语义元素的别名索引，供 Schema Linking 使用；
- 词到维度技术值的值索引，供链接与归因共用。

API 请求和脚本按需读取快照。`load_snapshot(version=...)` 可以读取指定版本；`history.rerun_answer` 用它按原始语义版本重跑回答。

当前**没有草稿/审核/发布工作流**，也没有语义发布 API 或界面。数据库结构里存在版本与状态列，按版本读取也可用，但唯一的发布路径是种子脚本，它只发布 v1 且是幂等的。

### 5.2 种子对象

| 对象 | 数量 | 说明 |
| --- | --- | --- |
| 数据集 | 1 | `ds_sales`，覆盖订单、订单行、门店、客户的销售数据集；默认时间窗口为 `last_month`。 |
| 模型 | 4 | 订单、订单行、门店、客户。 |
| 列 | 19 | 供明细查询与列级策略使用。 |
| 关系 | 3 | 订单行到订单、订单到门店、订单到客户，均为多对一。 |
| 指标 | 5 | 销量、净销售额、毛销售额、有效订单数、客单价。 |
| 维度 | 7 | 大区、省份、城市、门店、产品线、渠道、订单状态。 |
| 术语 | 6 | 5 个指标术语，加业务术语 `老客户`。 |
| 实体 | 3 | 客户、门店、商品。 |
| 属性 | 1 | 门店所属大区，绑定到大区维度。 |
| 本体关系 | 1 | 客单价由净销售额与有效订单数推导。 |
| 本体到 MDL 的映射 | 16 | 术语到指标、术语到规则 SQL、属性到维度、实体到模型、术语到数据集范围。 |
| 业务规则 | 3 | 仅已完成、排除测试渠道、金额单位。 |
| Evidence | 7 | 5 个指标的复算 SQL，加状态分布与渠道分布。 |
| 业务事件 | 2 | 五一促销活动与华东物流线路检修。 |
| 角色 | 5 | `admin`、`semantic_admin`、`analyst`、`viewer`、`region_east`。 |
| 用户 | 4 | `admin`、`analyst`、`east_mgr`、`viewer1`；演示密码 `Init@123456`。 |
| 数据策略 | 4 | 3 条数据集级策略，加 1 条给 `region_east` 的行级策略。 |
| 种子示例问答对 | 6 | 问题到 SemQL 的 few-shot 样例。 |
| 词典词条 | 4 | 人工加权的业务词。 |

### 5.3 指标、规则与维度

| 指标 | 定义 | 类型 |
| --- | --- | --- |
| `metric_qty`（销量） | 已完成订单的 `SUM(order_items.quantity)` | 原子指标 |
| `metric_net_sales`（净销售额） | 已完成订单的 `SUM(quantity * unit_price * discount_rate)` | 原子指标 |
| `metric_gross_sales`（毛销售额） | 不过滤状态的 `SUM(quantity * unit_price)` | 原子指标 |
| `metric_order_cnt`（有效订单数） | 已完成订单的 `COUNT(DISTINCT orders.order_id)` | 原子指标 |
| `metric_aov`（客单价） | `metric_net_sales / metric_order_cnt` | 派生指标 |

“仅统计已完成订单”的条件写在需要该口径的指标的 `filter_sql` 中；编译器还会把适用的规则记入 `applied_rules`。当查询涉及 `orders` 且没有显式渠道过滤时，编译器会注入测试渠道排除条件。金额单位规则只作说明。

种子业务规则：

- `rule_completed`（block）：净销售额、销量、有效订单数只统计已完成订单。
- `rule_exclude_test`（block）：经营口径默认排除测试渠道订单。
- `rule_unit`（info）：金额指标单位为人民币元。

七个种子维度如下：

| 维度 | 展示名 | 取值与说明 |
| --- | --- | --- |
| `dim_region` | 大区 | `HD`/华东、`HB`/华北、`HN`/华南、`XB`/西北；层级为大区 → 省份 → 城市 → 门店 |
| `dim_province` | 省份 | 属于“大区到门店”的层级。 |
| `dim_city` | 城市 | 属于“大区到门店”的层级。 |
| `dim_store` | 门店 | 门店名称；属于同一层级。 |
| `dim_product_line` | 产品线 | `A`/产品线A、`B`/产品线B、`C`/产品线C |
| `dim_channel` | 渠道 | `ONLINE`/线上、`OFFLINE`/线下、`TEST`/测试 |
| `dim_status` | 订单状态 | `completed`/已完成、`refunded`/已退款、`cancelled`/已取消 |

值映射在编译期把展示名和查询别名翻译为技术值。归因也使用该层级：只有上级维度下钻后，下级维度才会解锁。没有值映射的维度（省份、城市、门店）直接使用库中存储的取值。

### 5.4 Evidence

每个指标术语至少有一条 Evidence，包含真实 SQL 与标签。运行 `init_db.py` 时，每条 Evidence SQL 都会对业务库真实执行，结果写入 `ont_evidence.result_summary`。任一语句失败，初始化就会以非零状态退出。重复运行初始化会刷新已保存的结果，因此 Evidence 始终与当前 `retail.db` 对齐。

## 6. 演示数据集

### 6.1 表结构

| 表 | 列 | 说明 |
| --- | --- | --- |
| `stores` | `store_id`、`store_name`、`region_name`、`province`、`city` | 32 家门店：4 个大区 × 2 个省 × 2 个市 × 2 家店。 |
| `customers` | `customer_id`、`first_order_date`、`level` | 200 个客户；等级为 A/B/C。 |
| `orders` | `order_id`、`store_id`、`customer_id`、`status`、`order_date`、`channel` | 约 9.1k 条订单。 |
| `order_items` | `id`、`order_id`、`product_line`、`quantity`、`unit_price`、`discount_rate` | 约 18.3k 条订单行；每单 1~3 行。 |

`orders(order_date)`、`orders(store_id)`、`order_items(order_id)` 上建有索引。生成脚本使用固定种子重建文件，因此数据量与数据故事都可复现。

### 6.2 规模与分布

- 日期范围：2023-06-01 至 2024-06-30。
- 门店：32 家；客户：200 个。
- 订单：约 9,148 条；订单行：约 18,297 条。
- 状态占比：已完成约 85%，已退款约 8%，已取消约 7%。
- 渠道占比：线上约 55%，线下约 42%，`TEST` 约 3%。
- 产品线基准占比：A 40%、B 35%、C 25%；基准单价分别为 100、200、150 元。

### 6.3 内置数据故事

按大区统计的月度销量（已完成且非测试渠道）围绕 HD 1,300、HB 900、HN 800、XB 500 件生成，并叠加小幅随机噪声。其中有两处刻意设置的偏差：

- 华东（`HD`）2024-04 相比 2024-03 下滑约 35%，其中产品线 A 约占跌幅的 70%；
- 华东 2024-05 相比 2024-04 回升约 25%，主要由产品线 A 带动。

因此 `为什么2024年4月销量比5月低` 会主要归因到华东，再下钻到产品线 A。生成脚本末尾会打印“月份 × 大区”透视表和华东产品线拆分，便于肉眼核对这一故事。

### 6.4 业务事件

| 事件 | 时间窗口 | 关联维度值 |
| --- | --- | --- |
| 五一促销主会场活动 | 2024-05-01 至 2024-05-07 | 大区 `HD` |
| 华东物流线路检修 | 2024-04-10 至 2024-04-12 | 大区 `HD` |

当事件与所比较的期间或两期之间的区间相交，且事件为全局事件或命中归因路径上的维度值时，归因结果会列出该事件。事件是解释线索，不是因果证明。

## 7. 自然语言链路

### 7.1 记忆命令旁路

在任何模型调用之前，`chatbi/services/chat_service.py` 会用严格模式匹配识别记忆命令：查看记忆、忘掉全部/按类别/按术语、切换图表、记住图表偏好、记住术语口径。命中后会发出 `intent=MEMORY` 并直接处理，不进入 NL2SQL。“这次用折线图”之类的普通表达不会被当作长期记忆命令。

这些命令的具体字符串与行为见 [MEMORY_SYSTEM.zh-CN.md](MEMORY_SYSTEM.zh-CN.md)。

### 7.2 上下文与改写

会话状态分两层保存：进程内的 `OrderedDict` LRU，以及持久化的 `chat_context` 行。可持久化的白名单是 `turns`、`last_semql`、`clarify_choices` 和 `last_answer`。`clarify_pending` **不会**持久化；服务重启会丢失待处理的澄清卡片。已完成的上下文可以跨重启恢复。

`rewrite(question, turns)` 返回 `(rewritten_question, inherits, topic_changed)`。只有存在上下文时才会调用；它使用 light 模型、温度为 0，失败时按单轮问题处理。`topic_changed=true` 会清空已保存的 SemQL。改写与意图识别最多使用最近 5 轮对话。

### 7.3 意图识别

`chatbi/nlp/intent.py` 识别六类意图：

- `DATA_QUERY`
- `INSIGHT_WHY`
- `INSIGHT_ANOMALY_CHECK`
- `CHITCHAT`
- `UNSUPPORTED`
- `FORBIDDEN_DOMAIN`

先用 light 模型在温度 0.0 下识别，失败后以 0.2 重试一次，再失败则走关键词规则兜底，并把结果标记为 `degraded=True`。识别结果包含 mention、时间范围、粒度、歧义和数据集 id。

时间处理优先采用模型给出的 `YYYY-MM-DD` 区间（起止颠倒时会自动纠正），否则解析模型给出的时间表达式。兜底解析器支持具体日期、年月、不带年份的月份、年份、“上个月”、“本月”、“今年”和“去年”。不带年份的月份按 `TZ_NAME` 时区中的当前年份解析；这正是静态演示数据与相对时间问题可能对不上的原因。

mention 是模型给出的 mention 与词典命中词的并集，词典命中要求 jieba 词性以 `_` 开头。未知数据集会回退到快照中的第一个数据集。

### 7.4 Schema Linking

`chatbi/nlp/linking.py` 采用三个阶段：

1. **词典路。** jieba 带词性分词；整词命中直接映射到术语、维度、维度值、实体、属性、数据集或模型。
2. **mention 路。** 意图给出的每个 mention 会在维度值索引和别名索引中查找。
3. **模型精排。** 只有仍未命中的 mention 才送给 light 模型，候选限定为同一数据集内的术语与维度。维度值从不经过模型。模型可以返回候选 id 或 `NEED_CLARIFY`；未知 id 会被记为幻觉并丢弃。

链接本身不消解冲突。一个 mention 命中多个术语时会全部保留，由澄清层决定是否追问。

### 7.5 澄清

`chatbi/nlp/clarify.py` 用确定性规则检测歧义，不调用模型：

| 歧义 | 触发条件 |
| --- | --- |
| `TERM_CONFLICT` | 同一 mention 命中至少两个指标术语，且它们绑定至少两个不同指标。 |
| `VALUE_CONFLICT` | 同一 mention 解析出至少两个维度技术值。 |
| `DATASET_CONFLICT` | mention 的解析结果跨数据集；在单数据集场景下基本不会触发。 |
| `TIME_MISSING` | 没有时间范围，且数据集没有默认时间窗口。 |
| `GRAIN_MISSING` | 问题包含趋势/变化类措辞，但未提取到粒度。 |

每轮最多发一张卡片，按上述优先级选择，最多允许两轮澄清。卡片选项由检测出的候选生成：术语名加简短定义、近期时间窗口、日/周/月粒度或维度值。用户用自由文本回答时，服务会把文本以“补充：…”的形式附加后重新提问；用户点选选项时，所选键必须属于当前卡片。

待处理状态包含原问题、意图、链接结果、轮次和候选，并绑定提问用户与会话。由于它只存在于进程内，服务重启后卡片即过期。会话内的选择与仍有效的长期术语偏好，只有在当前快照中候选仍然存在时才会被复用。

## 8. SemQL 与生成

### 8.1 SemQL 结构

`chatbi/query/semql.py` 定义该 DSL。一次查询包含：

- `dataset`
- `metrics[]`
- `dimensions[]`
- `time`，含 `field`、`range` 和可选的 `grain`（`day`、`week`、`month`）
- `filters[]`，含 `dimension` 或 `metric`、操作符（`in`、`not_in`、`eq`、`gt`、`lt`、`ge`、`le`）和取值
- `order_by[]`
- `limit`
- `compare`（`mom`、`yoy` 或 `period_vs_period`），可带基期
- `query_mode`（`aggregate` 或 `detail`）
- `business_terms[]`

聚合查询上限为 10,000，明细查询上限为 1,000。`validate_against(snapshot)` 会在编译前校验数据集、指标、维度、列、过滤值、业务术语、限额与时间表达式。

### 8.2 提示词组装与重试

`chatbi/query/generator.py` 构建三级提示词：

- **L1：** 数据集的指标与维度清单；
- **L2：** 命中元素的详情，包括定义、单位、方向、必须遵守的 block 规则、推导关系和值映射；
- **L3：** 未命中元素的候选扩展。

提示词还会加入最多 3 条示例问答对和最近 5 轮上下文。示例来自种子数据、用户本人点赞过的问答对，以及全局生效的反馈示例。

生成最多尝试三次：原提示词温度 0.0、错误回显重试温度 0.0、候选扩展重试温度 0.3。全部失败会抛 `LLMGenerateFailedError`。

### 8.3 确定性后修正

生成之后、校验之前，`chatbi/services/query_service.py` 会应用基于规则的修正：

- 意图给出的时间范围覆盖 SemQL 时间范围。对于对比查询，保留模型划分的“当前期”，仅按需向前延展起点以覆盖对比桶。
- 如果命中的术语只绑定一个指标，就把该指标强制为唯一或首位指标，避免示例问答把查询带偏到其他指标。
- 命中业务术语时，强制把其 id 写入 `business_terms`。

如果随后编译报出 `SEMQL_INVALID_REF` 或 `SEMQL_INVALID_VALUE`，服务会带编译器错误回显再生成一次。其他编译错误会直接使查询失败。

## 9. 编译器

`chatbi/query/compiler.py` 是确定性的，从不调用模型。

- **主模型。** 从第一个指标出发；派生指标则从其组成指标出发；否则回退到第一个维度或列。
- **Join。** 沿关系图做广度优先遍历。多对多关系抛 `COMPILE_FANTRAP_RISK`；不可达模型抛 `COMPILE_NO_JOIN_PATH`。时间字段所属模型一定会被 join。
- **原子指标。** 展开为 `AGG(CASE WHEN <condition> THEN <expression> ELSE 0 END)`。`COUNT_DISTINCT` 单独处理。
- **派生指标。** 只支持 `metric_a / metric_b`，渲染为 `ROUND(num * 1.0 / NULLIF(den, 0), 2)`。其他公式抛 `COMPILE_UNSUPPORTED_FORMULA`。
- **值翻译。** 展示值、查询别名和技术值经 `value_maps` 翻译为技术值。未知值抛 `SEMQL_INVALID_VALUE`。
- **业务术语。** `term_rule_sql` 模板会作为附加 WHERE 谓词内联，`{t_start}` 替换为查询起始日。该谓词会记入 `applied_rules`。
- **规则注入。** 涉及 `orders` 且没有显式渠道过滤的查询会追加 `orders.channel != 'TEST'`。带“仅已完成”过滤的指标会贡献相应的规则说明。
- **分组与排序。** 粒度用 `strftime` 表达（月为 `%Y-%m`，周为 `%Y-W%W`，日为 `date()`）。有粒度但没有 `order_by` 时按时间升序。默认限额为 100。
- **对比。** 有粒度时，环比和同比在请求的时间桶上用 `LAG` 窗口；无粒度时，用两个 `CASE WHEN` 列比较两期。带粒度的 `period_vs_period` 会被明确拒绝。

编译器返回 SQL 文本、`applied_rules` 和供后续推断列角色的 trace。

## 10. 安全网关与执行

### 10.1 网关检查

`chatbi/query/gateway.py` 按固定顺序处理 SQL：

1. 用 sqlglot 的 SQLite 方言解析单条语句。
2. 只允许 `SELECT` 或 `WITH ... SELECT`；拒绝写操作、DDL、命令、attach、pragma 和 merge 节点。
3. 拒绝文件与网络函数：`load_extension`、`readfile`、`writefile`、`file`、`url`。
4. 只允许物理表 `orders`、`order_items`、`stores`、`customers`；CTE 名称豁免。
5. 注入行级策略。
6. 对输出列应用列级策略。
7. 按需补上或收紧 `LIMIT 10000`。

任何被禁止的构造都会抛 `SqlForbiddenError`，不会重试。

### 10.2 行级与列级策略

行级策略来自 `data_policy` 中类型为 `row` 的记录。对每个策略维度，允许值取并集后注入为谓词。如果策略没有可用值，或其维度、模型已失效，网关会注入 `1 = 0`，即失败时拒绝。如果维度所在表没有直接出现在查询中，网关会沿关系图找到路径，并注入嵌套子查询谓词。

列级策略按**输出列名**匹配。`hide` 策略抛 `COLUMN_FORBIDDEN`；`mask` 策略用 `substr` 改写选中的表达式做脱敏。

数据集级策略**没有**在这里或查询链路的任何位置强制执行。它们会被保存，并通过 `/auth/me` 的 `policies.dataset_ids` 返回，除此之外仅作信息展示。实际的表访问边界是网关的物理表白名单。

网关返回的是重新渲染后的 SQL：sqlglot 会归一化函数大小写、别名和格式，因此最终执行的文本可能与编译器原始产物不完全一致。`applied_rules` 同时包含编译器规则和网关说明（例如实际生效的数据范围）。

### 10.3 执行

`chatbi/query/executor.py` 以只读方式打开业务库，通过 SQLite progress handler 每 1,000 步检查一次墙钟超时（30 秒），结果上限为 10,000 行，并多取一行用于设置 `truncated`。超时抛 `QueryTimeoutError`。

## 11. 洞察链路

### 11.1 触发、指标与期间

`INSIGHT_WHY` 与 `INSIGHT_ANOMALY_CHECK` 由 `chat_service._run_insight` 处理。指标取链接命中的第一个指标术语；没有命中时使用 `metric_qty`。

期间按确定性规则选择：

- 问题中出现至少两个月份时，取前两个；
- 只出现一个月份且意图带时间范围时，用该范围的起点月份作为 A 期；
- 否则比较最近一个完整月与其前一个月，并记录该假设。

基础过滤固定排除 `TEST` 渠道，并合并问题中命中的维度值，使洞察与等价的数据查询保持同一口径。

### 11.2 显著性

`chatbi/insight/fluctuation.py` 通过完整的 SemQL、编译器、网关和执行器链路计算两期数值。它用 A 期之前最多 12 个完整月构建基线，计算环比序列的均值与总体标准差；当环比绝对值至少达到 `INSIGHT_SIGNIFICANT_PCT`，或偏离历史均值至少 `INSIGHT_SIGMA_K` 个标准差时，判定为显著。基线少于 6 期时只使用百分比规则，并标记 `baseline_limited`。任一期没有数据时，链路终止并告知用户无法归因。

### 11.3 归因

`chatbi/insight/attribution.py` 是确定性的：

- 对派生指标先做对数分解：`ln(A) - ln(B) = ln(num_A/num_B) - ln(den_A/den_B)`，给出分子与分母的贡献占比，并选择贡献更大的一侧继续下钻。
- 候选维度来自指标的关联维度，并剔除列策略隐藏的维度。
- 每一层对候选维度各跑两次分组查询，选择 coverage 最高的维度。coverage 定义为前三个值的贡献绝对值之和除以局部增量。coverage 相同时，比较最大单项贡献的绝对值。
- 只有 coverage 不低于 50% 才继续展开；第一层低于该阈值时返回 `diffuse=True` 的空态，不再下钻。节点贡献不低于局部增量的 20% 且不低于全局增量的 10%、且路径不超过三层时，才继续向下一层展开。
- 层级维度只有在其上级维度都已下钻后才解锁；产品线、渠道等层级外维度始终可参选。
- 维度值至少有三个月度点时计算 z 分数；小基数且 `|z| < 1`、占基期总量不足 1% 的值只给出低基数提示，不作为主因。
- 瀑布图的构成为基期总量、第一层 top 因子、“其他”、现期总量。
- 与 A 期或 A 到 B 区间相交、且为全局事件或命中归因路径维度值的事件会被关联。

### 11.4 结论与持久化

`chatbi/insight/narrative.py` 让 heavy 模型生成简短结论（结论、主要原因、事件佐证、低基数提示、下一步建议，最多 250 字）。模型失败或输出不可用时，用确定性模板拼接文本，并标记为降级。`POST /insight/attribute` 同步执行、写入 `insight_report`，返回报告与结论。`GET /insight/{report_id}` 允许报告所有者或管理员读取。聊天前端直接从聊天结果渲染结论与瀑布图，不调用这两个接口。

## 12. 图表选择

`chatbi/chart/rules.py` 根据列角色、数据形态和 SemQL 选择一种图表类型。规则按优先级依次判断，命中即停止。

| 规则 | 条件 | 结果 |
| --- | --- | --- |
| 11 | 列为 label/value/type | `waterfall`（归因） |
| 9 | `query_mode=detail` | `table` |
| 1 | 1 行、1 个指标、无维度 | `metric_card` |
| 2 | 时间 × 1 个指标、无分类维度，至少 3 行 | `line` |
| 3 | 时间 × 1 个指标 × 1 个分类 | `multi_line` |
| 4 | 分类 × 1 个指标，最多 12 行 | `bar`；名称平均长度超过 6 时用 `bar_horizontal` |
| 5 | 分类 × 1 个指标，超过 12 行 | `top10_bar_horizontal` |
| 6 | 分类 × 1 个指标，且指标名含占比/份额/比例/百分比等措辞 | 7 个分类以内用 `pie`，否则用 `treemap` |
| 10 | 其他情况 | `pivot_table`，再兜底为 `table` |

列角色来自编译器 trace：时间别名为 `time`，维度别名为 `dimension`，指标别名或以 `_基期`/`_差值` 结尾、或含 `%` 的列名为 `metric`，其余为 `column`。

随后应用长期图表偏好。当次明确指定图型时优先采用，且不会写成长期偏好；否则，同一数据形态下兼容的长期偏好会被应用，但目标类型必须仍在 `available_types` 中。

这一部分的已知实现缺口见第 20 节。

## 13. 历史与反馈

### 13.1 历史

会话、消息、查询日志和回答分别保存在 `chat_session`、`chat_message`、`query_log` 和 `answer`。会话列表按 `updated_at` 排序并支持游标分页。会话详情向所有者返回最近 100 条消息。回答快照保存图表类型、图表元数据、列角色、前 200 行结果、行数与摘要。

`POST /history/answers/{answer_id}/rerun` 仅允许所有者调用。它从关联的查询日志中取出原问题，默认按当前 published 快照重跑；`use_original_version=true` 时按原始语义版本重跑。响应包含 `rerun_of` 和原始版本号，版本不同时还附版本提示。重跑会重新走生成链路，不产出图表或摘要对象。

前端没有调用重跑接口，也没有调用会话详情接口。

### 13.2 反馈

`POST /feedback` 接受 `up` 或 `down`。点踩必须提供 `wrong_data`、`wrong_intent`、`bad_chart`、`untrusted_insight` 或 `other` 之一，且 `answer_id` 与 `insight_id` 至少提供一个。同一用户对同一对象重复提交会覆盖原投票，并在 `feedback_history` 追加一行。

对回答点赞会立即为该用户写入个人问答对；同时把“问题 + SemQL”记为全局 `query_exemplar` 候选，只有 3 个不同用户对同一问题点赞后候选才会生效。点踩会撤销该用户的个人示例。`GET /feedback/queue` 向管理员和语义管理员列出待处理的点踩记录，并附带问题与最终 SQL。

前端只对数据类回答显示反馈按钮。点踩固定发送 `reason_tag=other`，没有原因选择或评论输入框。洞察类回答在界面上没有反馈按钮，尽管 API 接受 `insight_id`。

## 14. 记忆系统接缝

记忆系统见 [MEMORY_SYSTEM.zh-CN.md](MEMORY_SYSTEM.zh-CN.md)。它与查询链路的结合点仅限于：

- 会话上下文持久化第 7.2 节所述的白名单；待澄清状态只存在于进程内。
- 长期记忆类别为 `chart_preference`、`term_mapping`、`query_exemplar`，按 `user_id` 隔离。
- 置信度衰减以 `last_confirmed_at` 为锚点；使用记忆不会续期。
- 澄清选择可以来自当前会话，也可以来自仍有效的长期术语偏好，但前提是候选在当前快照中仍然存在。
- 图表个性化只在目标类型与当前回答兼容时才应用长期偏好。
- 点赞过的回答会成为个人示例问答对，并可作为 few-shot 示例被召回；模型或记忆故障会回退到普通查询链路。

## 15. 权限

认证与策略执行的细节见 [PERMISSIONS.zh-CN.md](PERMISSIONS.zh-CN.md)。当前实现可概括如下：

- 密码使用 PBKDF2-HMAC-SHA256，迭代 200,000 次。
- 令牌为 base64url 负载 `{user_id, exp}` 加 HMAC-SHA256 签名，有效期 8 小时。没有刷新，也没有服务端吊销。
- 登录只接受状态为 active 的用户。令牌校验只检查签名和用户是否存在，不检查用户当前状态，因此被禁用用户的存量令牌在过期前仍然有效。
- `admin` 绕过角色守卫。`analyst` 可以浏览语义摘要；`semantic_admin` 与 `admin` 可以浏览语义对象、使用链接调试接口、读取反馈队列；`admin` 可以列出用户和审计日志。
- 任意已登录用户都可以读取自己的会话、历史、记忆和本人洞察报告，也可以调用 `POST /insight/attribute`。
- 聊天要求非 viewer 角色。因此 `viewer1` 无法通过 `/chat/ask` 提问，但仍可调用归因接口。
- 行级与列级策略由网关强制执行。数据集级策略未强制执行，只通过 `/auth/me` 暴露。
- 审计记录覆盖登录成功与失败、记忆 REST 写入/删除/清空和 REST 图表切换。对话记忆命令写入 `user_memory_event`，不写入 `audit_log`。被拒绝的查询和策略命中不会写审计。

## 16. HTTP API

所有 API 路由都挂在 `/api/v1` 前缀下；静态前端由 `/` 提供。REST 成功响应为 `{"code": 0, "data": ...}`。REST 错误响应为 `{"code": <int>, "message": ..., "trace_id": ...}`。SSE 错误事件使用字符串 `code` 和 `retryable` 标志，与 REST 的约定不同。

| 方法与路径 | 权限 | 作用 |
| --- | --- | --- |
| `POST /auth/login` | 匿名 | 登录，返回令牌与用户信息。 |
| `GET /auth/me` | 已登录 | 当前用户、角色与聚合策略。 |
| `POST /chat/sessions` | 已登录 | 显式创建会话。 |
| `GET /chat/sessions` | 已登录 | 列出本人会话；与历史列表同一实现。 |
| `GET /chat/sessions/{id}` | 所有者 | 会话及消息。 |
| `DELETE /chat/sessions/{id}` | 所有者或管理员 | 删除消息、会话与短期上下文。 |
| `POST /chat/ask` | 非 viewer | 提问；以 SSE 返回 `intent`、`clarify`、`progress`、`result`、`error` 事件。 |
| `POST /chat/clarify` | 已登录 | 继续待处理的澄清；以 SSE 返回。 |
| `POST /insight/attribute` | 已登录 | 同步执行归因并保存报告。 |
| `GET /insight/{report_id}` | 所有者或管理员 | 读取已保存的洞察报告。 |
| `GET /history/sessions` | 已登录 | 游标分页的会话列表。 |
| `POST /history/answers/{answer_id}/rerun` | 所有者 | 重跑已保存的回答；可选按原始语义版本。 |
| `POST /feedback` | 已登录 | 提交或覆盖投票。 |
| `GET /feedback/queue` | `admin`、`semantic_admin` | 待处理的点踩记录，含问题与 SQL。 |
| `GET /memories` | 已登录 | 列出本人记忆。 |
| `POST /memories` | 已登录 | 显式保存图表偏好或术语映射。 |
| `DELETE /memories/{memory_id}` | 所有者 | 逻辑删除一条记忆。 |
| `DELETE /memories` | 已登录 | 清空本人记忆，可按类别过滤。 |
| `POST /memories/chart-switch` | 回答所有者 | 切换已保存回答的图表，不重跑 SQL。 |
| `GET /semantic/summary` | `admin`、`semantic_admin`、`analyst` | 语义版本与对象计数。 |
| `GET /semantic/terms`、`/metrics`、`/dimensions`、`/rules`、`/events` | `admin`、`semantic_admin` | 浏览语义对象。 |
| `GET` 或 `POST /semantic/debug/link` | `admin`、`semantic_admin` | 调试基于词典的链接；不传入 mention。 |
| `GET /sys/users` | `admin` | 列出用户及其角色。 |
| `GET /sys/audit-logs` | `admin` | 最近的审计记录。 |
| `GET /` | 匿名 | 静态演示前端。 |

常见错误码：`1001` 未认证或登录失败，`1003` 无权限，`1004` 不存在，`1400` 参数错误，`3001` 与 `3004` 归因错误，`5000` 未捕获异常。

## 17. 前端

`chatbi/api/static/index.html` 是一个静态单页，使用原生 JavaScript 和从 CDN 加载的 ECharts。它只调用以下服务端接口：

- `POST /auth/login`
- `GET /history/sessions`
- `POST /feedback`
- `GET /memories`、`DELETE /memories/{id}`、`DELETE /memories`
- `POST /memories/chart-switch`
- `POST /chat/ask` 与 `POST /chat/clarify`（SSE）

令牌与用户对象存放在 `localStorage`。SSE 用 `fetch` 加手工 reader 读取：先把 CRLF 归一化，再按空行切分事件；服务端为配合它显式使用 LF 分隔。界面展示分析时间线，以及可折叠的留痕面板（SQL、SemQL、匹配解释、已应用规则、记忆使用、语义版本），并分别渲染数据类、洞察类或文本类回答。

图表渲染支持 `bar`、`bar_horizontal`、`top10_bar_horizontal`、`line`、`multi_line`、`pie`、`waterfall`、`metric_card`。`table` 与 `pivot_table`，或 ECharts 不可用时的任何图表，都会降级为 HTML 表格。切换图表的按钮调用记忆接口，并用返回的图表重绘。

前端限制：

- 历史抽屉只列出会话标题；选中会话时不会拉取并回显消息。服务端上下文仍会恢复，因此追问能正确继承，但旧消息气泡不可见。
- 界面上没有会话删除、回答重跑、洞察报告读取或语义浏览的入口。
- 反馈只对数据类回答开放，点踩固定使用 `other`。
- 两个示例 chip：`今年上半年各品类的毛利率趋势` 与 `销售额 Top10 的商品`，引用了种子语义层中不存在的指标或维度。
- 页面本身公开可访问；认证保护的是数据接口。

## 18. 存储

### 18.1 元数据库

`data/meta.db` 由 `init_schema` 创建，包含以下表组：

- 语义层：`datasource`、`mdl_*`、`ont_*`、`ont_mdl_mapping`、`semantic_version`。
- 身份与策略：`app_user`、`role`、`user_role`、`data_policy`、`audit_log`。
- 对话：`chat_session`、`chat_message`、`chat_context`、`query_log`、`answer`。
- 洞察与反馈：`insight_report`、`feedback`、`feedback_history`、`query_exemplar`、`business_event`、`dict_entry`。
- 记忆：`user_memory`、`user_memory_event`。
- 模型调用遥测：`llm_call_log`。

`query_log` 保存问题、命中元素、SemQL、最终 SQL、已应用规则、语义版本、重试次数、状态、错误码与耗时。部分列虽然存在，但当前代码从未写入：`query_log` 的 `rewritten_question`、`intent`、`assumptions`、`policy_hash`、`degraded`，以及 `insight_report.elapsed_ms`。`datasource` 记录不用于打开连接，`mdl_measure` 不被编译器使用。这些字段应视为预留。

### 18.2 业务库与生成文件

- `test_data/data/retail.db` 是只读的演示业务库，由 `scripts/gen_retail_data.py` 重建。
- `data/dict/custom_dict.txt` 与 `data/dict/word_nature.json` 由已发布快照生成。文本文件只存简码，因为 jieba 用户词典的第三列只接受小写 ASCII；完整词性在分词时从 JSON 文件还原。
- `test_data/output/` 接收全链路回归脚本为每题生成的 JSON 与 `report.md`。

元数据库与输出目录在 git 忽略列表中。演示业务库已纳入版本管理，随仓库一起提供演示数据，且重新生成是确定性的。

## 19. 配置

`chatbi/config.py` 只加载项目根目录的 `.env`，相对路径按项目根解析，在导入 httpx 或 openai 之前修复无效的 `SSL_CERT_FILE`，并在 `DEEPSEEK_API_KEY` 缺失时于导入期直接失败。

| 键 | 默认值 | 作用 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 必填 | 模型认证。 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点。 |
| `LLM_MODEL_HEAVY` | `deepseek-v4-pro`（示例） | SemQL 生成与结论表述。 |
| `LLM_MODEL_LIGHT` | `deepseek-flash`（示例） | 改写、意图、精排、摘要。 |
| `LLM_TIMEOUT_SEC` | 60 | 单次调用超时。 |
| `META_DB_PATH` | `./data/meta.db` | 元数据库。 |
| `BIZ_DB_PATH` | `./test_data/data/retail.db` | 只读业务库。 |
| `AUTH_SECRET` | `change-me`（代码默认） | 令牌 HMAC 密钥。 |
| `TZ_NAME` | `Asia/Shanghai` | 相对时间的参考时区。 |
| `INSIGHT_SIGNIFICANT_PCT` | 5 | 百分比显著性阈值。 |
| `INSIGHT_SIGMA_K` | 2 | 标准差显著性阈值。 |
| `MEMORY_SESSION_TTL_HOURS` | 72 | 会话上下文存活时长。 |
| `MEMORY_CONTEXT_CACHE_SIZE` | 128 | 进程内缓存容量。 |
| `MEMORY_TERM_PROMOTE_SESSIONS` | 3 | 术语选择晋升阈值。 |
| `MEMORY_MIN_CONFIDENCE` | 0.35 | 最低有效置信度。 |
| `MEMORY_EXPLICIT_HALF_LIFE_DAYS` | 365 | 显式偏好半衰期。 |
| `MEMORY_INFERRED_HALF_LIFE_DAYS` | 90 | 推断偏好半衰期。 |

派生目录包括 `DATA_DIR`（元数据库所在目录）、`DICT_DIR`、`DICT_FILE` 与 `OUTPUT_DIR`。`ensure_dirs()` 会在打开数据库前创建它们。

`.env.example` 中的模型名只是占位示例，必须替换为所配置账号实际可用的模型。

## 20. 已知限制

1. **相对时间与静态演示数据错位。** 演示数据截止 2024-06-30，而相对时间按机器时钟解析，数据集默认窗口为 `last_month`。在演示期之外，相对时间问题可能返回零行。请使用 2024 年的明确日期。归因中的低基数 z 分数历史同样以演示数据起点 2023-06-01 为锚点。
2. **数据集级策略未强制执行。** 类型为 `dataset` 的 `data_policy` 只通过 `/auth/me` 暴露，查询链路与网关都不校验。
3. **viewer 权限范围不完整。** `viewer1` 不能调用 `/chat/ask`，但可以调用 `POST /insight/attribute`，后者会通过同一链路发起真实查询。
4. **待澄清状态只存在于进程内。** 服务重启会丢弃待处理卡片，后续澄清请求返回 `CLARIFY_EXPIRED`。
5. **单数据集、单进程。** 没有多数据集路由、多租户、并发控制、迁移框架或水平扩展。CORS 同时允许 `*` 和携带凭据，浏览器会视其为非法组合；前端实际使用 Bearer 头。
6. **语义版本化很有限。** 只发布 v1 种子；按版本读取可用，但没有草稿、审核、批准或发布流程，也没有版本管理界面。
7. **历史与重跑仅在后端可用。** API 支持会话详情、会话删除和回答重跑，但前端均未提供入口。
8. **反馈闭环不完整。** API 接受洞察类反馈，但界面没有入口。界面点踩固定使用 `other`，无法附带原因或评论。
9. **图表规则文案夸大了行为。** 规则 3 与规则 5 描述了“Top N 合并其他”的行为，但并未实现；行数由 SQL 限额决定。规则 6 实际上不可达，因为“分类 × 指标”的形态会先命中规则 4 或规则 5。
10. **令牌生命周期很基础。** 令牌有效期 8 小时，没有刷新或吊销，校验时也不会重新检查用户状态。
11. **错误约定不一致。** REST 错误使用整数错误码和统一错误包；SSE 错误使用字符串错误码，兜底路径还可能带出内部异常文本。
12. **部分字段为预留。** `query_log.rewritten_question`、`intent`、`assumptions`、`policy_hash`、`degraded` 与 `insight_report.elapsed_ms` 从未写入；`datasource` 与 `mdl_measure` 在运行时未被使用。
13. **前端示例超出语义层。** 两个内置示例 chip 引用了种子中不存在的指标或维度，会触发澄清或失败。
14. **静态页公开可访问。** 认证保护的是 API，而不是 HTML 页面本身。

## 21. 验证

仓库在 `tests/` 下提供 pytest 测试集。测试不调用模型，但导入项目要求 `DEEPSEEK_API_KEY`，且多个测试模块会加载已发布的语义层快照，因此需要已初始化的 `data/meta.db`。

```bash
python -m pytest tests/ -q
```

`python scripts/dev_query.py` 在不调用模型的情况下覆盖编译、网关检查与执行。其他 `dev_*` 脚本和 `python scripts/run_pipeline_test.py` 会真实调用模型；后者会在 `test_data/output/` 下为每题写入 JSON 并生成 `report.md`。测试数量和通过结果不是固定保证；请以当前实际输出为准，不要依赖历史数字。
