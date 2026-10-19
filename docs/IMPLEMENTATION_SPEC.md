# InsightChat 实现规格说明书（本地可运行版）

> 本文件是实现的**唯一权威契约**。产品级 PRD 见 `../ChatBI_PRD.md`（语义、交互、验收标准以 PRD 为准）；本文件只规定：本地可运行版的技术适配、模块契约、演示数据、验证方式。冲突时以本文件为准。

## 0. 技术栈适配（PRD → 本实现）

| PRD 拍板 | 本实现 | 理由 |
|---|---|---|
| vLLM 私有化 Qwen72B/32B | **DeepSeek API**：重任务 `deepseek-v4-pro`，轻任务 `deepseek-flash` | 用户指定；OpenAI 兼容协议，`openai` SDK 接入 |
| PostgreSQL+pgvector（元数据） | **SQLite**（`data/meta.db`） | 本地可运行；表结构沿用 PRD 第七章（去掉 vector 列与 PG 专有语法） |
| ClickHouse（业务数仓） | **SQLite**（`test_data/data/retail.db`），Compiler 方言经 sqlglot 抽象 | 本地可运行；方言隔离在 compiler/dialect |
| bge embedding 向量召回 | 去掉向量路；schema linking = **jieba 词典匹配 + LLM 精排**两路 | DeepSeek 无 embedding API；词典路是 PRD 主路 |
| Celery+Redis | FastAPI 后台任务 + SQLite 持久化 + 进程内会话上下文缓存 | 本地单进程可运行 |
| React 前端 | FastAPI 托管的单页 `static/index.html`（原生 JS + ECharts CDN，CDN 失败降级表格） | 可演示即可 |
| JWT+RBAC 完整认证 | 保留：账号密码登录（bcrypt 用 pbkdf2 标准库实现即可）、角色、行/列策略在 SQL 网关强制执行 | 权限是刚性需求，不阉割 |

其余全部按 PRD 实现：SemQL DSL、三级注入、澄清状态机（最多2轮）、多轮改写、归因引擎（波动识别→公式拆解→贡献度下钻→事件关联→LLM结论）、图表映射规则、版本化（draft/published 简化版：发布即生效，保留版本号与快照）、反馈回流、历史、审计。

## 1. 环境变量（.env，键名固定）

```
DEEPSEEK_API_KEY=          # 真实key仅放.env
DEEPSEEK_BASE_URL=https://api.deepseek.com
LLM_MODEL_HEAVY=deepseek-v4-pro     # SemQL生成、归因结论、口径起草
LLM_MODEL_LIGHT=deepseek-flash      # 意图、改写、精排、澄清选项、摘要
LLM_TIMEOUT_SEC=60
META_DB_PATH=./data/meta.db
BIZ_DB_PATH=./test_data/data/retail.db
AUTH_SECRET=change-me
TZ_NAME=Asia/Shanghai
```

## 2. DeepSeek 调用约定（`chatbi/llm.py`）

- `openai.OpenAI(api_key=env, base_url=env)`；调用封装 `chat_json(task, system, user, heavy=False, temperature=0) -> dict` 与 `chat_text(...)->str`。
- `chat_json`：优先 `response_format={"type":"json_object"}`（system 中必须含"JSON"字样，DeepSeek 要求）；解析失败则正则提取首个配平 `{}` 再 `json.loads`；仍失败抛 `LLMFormatError` 由调用方走重试链。
- 每个 task 记录 `llm_call_log`（SQLite 表：task/model/prompt/response/latency/created_at）。
- 网络异常：指数退避重试 2 次（1s/3s），仍失败抛 `LLMUnavailableError`。

## 3. 目录结构

```
chatbi/                      # 项目根
├── .env / .env.example / .gitignore / requirements.txt / README.md / IMPLEMENTATION_SPEC.md
├── chatbi/                  # 包
│   ├── config.py  db.py  llm.py  security.py(密码哈希/JWT简化token)
│   ├── semantic/{schema.py, store.py, seed.py, dictionary.py, seed/ontology_seed.yaml}
│   ├── nlp/{prompts.py, intent.py, rewrite.py, linking.py, clarify.py}
│   ├── query/{semql.py, generator.py, compiler.py, gateway.py, executor.py}
│   ├── insight/{fluctuation.py, attribution.py, narrative.py}
│   ├── chart/rules.py
│   ├── services/{chat_service.py, history.py, feedback.py, auth.py}
│   └── api/{main.py, deps.py, routes_chat.py, routes_insight.py, routes_history.py, routes_feedback.py, routes_admin.py, static/index.html}
├── scripts/{init_db.py, gen_retail_data.py, run_pipeline_test.py, run_server.py}
├── tests/{test_compiler.py, test_gateway.py, test_attribution.py, test_semql.py}
└── test_data/
    ├── input/questions.json
    ├── data/retail.db
    ├── expected/expectations.json
    └── output/              # 运行产物: <qid>.json + report.md
```

## 4. 演示业务库（scripts/gen_retail_data.py 生成，确定性 seed=42）

表（SQLite，与 PRD 7.3 对齐）：
- `stores(store_id PK, store_name, region_name, province, city)`：region∈{HD 华东, HB 华北, HN 华南, XB 西北}，每区 2 省 × 每省 2 市 × 每市 2 店 = 32 店。
- `orders(order_id PK, store_id, customer_id, status, order_date, channel)`：status∈{completed,refunded,cancelled}（completed 占 ~85%），channel∈{ONLINE,OFFLINE,TEST}（TEST 占 ~3%）。
- `order_items(order_id, product_line, quantity, unit_price, discount_rate)`：product_line∈{A,B,C}，每单 1~3 行。
- 时间范围 2023-06-01 ~ 2024-06-30。**数据故事（必须保证）**：各区域月度销量（completed 的 quantity 合计）基准 HD≈1300/HB≈900/HN≈800/XB≈500，随机噪声 ±5%；**2024-04 华东 HD 下滑 ~35%（其中产品线 A 占跌幅 ~75%）**；**2024-05 华东回升 +25%**；其余区域正常波动。这样"为什么2024年4月销量比5月低"的归因主因=华东（→产品线A）。
- 客户 200 个，`customers(customer_id, first_order_date, level)`。

## 5. 语义层种子（`semantic/seed/ontology_seed.yaml`，init_db 导入为 published v1）

DataSet：`ds_sales` 销售域数据集（默认时间 last_month）。
Model：`mdl_orders`(orders, time_field=order_date, pk=order_id)、`mdl_order_items`、`mdl_stores`。
Relationship：order_items→orders many_to_one on order_id；orders→stores many_to_one on store_id；orders→customers many_to_one on customer_id。

Metric（bind Term）：
- `metric_qty` 销量：SUM(order_items.quantity) FILTER completed；related_dimensions=[region, province, city, store, product_line, channel]
- `metric_net_sales` 净销售额：SUM(quantity*unit_price*discount_rate) FILTER completed，单位元，higher_better
- `metric_gross_sales` 毛销售额：SUM(quantity*unit_price)，无 status 过滤
- `metric_order_cnt` 有效订单数：COUNT(DISTINCT orders.order_id) FILTER completed
- `metric_aov` 客单价（derived）：metric_net_sales / metric_order_cnt；derivation Relation 记录公式

Dimension（含 value_maps）：
- `dim_region` 大区：HD↔华东(alias 华东区)、HB↔华北、HN↔华南、XB↔西北；hierarchy [大区,省份,城市,门店]
- `dim_province`/`dim_city`/`dim_store`（门店）；`dim_product_line` 产品线：A↔产品线A(alias A线)…；`dim_channel` 渠道：ONLINE↔线上、OFFLINE↔线下、TEST↔测试；`dim_status` 订单状态：completed↔已完成(alias 完成)…；时间维度用 model.time_field。

Term：净销售额(alias **销售额**、有效销售额)、毛销售额(alias **销售额**、流水)——共享别名"销售额"用于演示 TERM_CONFLICT 澄清；销量、有效订单数、客单价；business_term `term_old_customer` 老客户（rule_sql：`orders.customer_id IN (SELECT customer_id FROM orders WHERE status='completed' GROUP BY customer_id HAVING MIN(order_date) < date('{t_start}','-1 year'))`，{t_start} 由编译器代入）。

BusinessRule：
- block `rule_completed`：净销售额/销量/有效订单数 仅统计 status='completed'（编译器强制注入）。
- block `rule_exclude_test`：默认排除测试渠道 channel='TEST'（自动应用并在 applied_rules 展示）。
- info `rule_unit`：金额单位人民币元。

Evidence：每条 metric_term 一条（如 2024-04 净销售额复算 SQL 与结果，init 时真实执行生成）。

Entity：客户、门店、商品（简化：仅 name/definition/aliases + attr 绑定维度）。
外部事件 `business_event`：①"五一促销主会场活动" 2024-05-01~05-07 related=dim_region/HD；②"华东物流线路检修" 2024-04-10~04-12 related=dim_region/HD。

用户（init 创建，密码均 `Init@123456` 仅演示）：`admin`(admin)、`analyst`(analyst，全量数据)、`east_mgr`(analyst+row_policy dim_region IN ['HD'])、`viewer1`(viewer)。

## 6. 关键模块契约（签名固定，跨阶段引用）

```python
# nlp/intent.py
class Intent(str, Enum): DATA_QUERY; INSIGHT_WHY; INSIGHT_ANOMALY_CHECK; CHITCHAT; UNSUPPORTED; FORBIDDEN_DOMAIN
def classify(question: str, ctx: list[dict], snapshot: SemanticSnapshot) -> IntentResult
# IntentResult: intent, confidence, mentions[], time_range {start,end}|None, grain|None, ambiguities[], dataset_id

# nlp/rewrite.py —— 指代改写，返回 (rewritten_question, inherits, topic_changed)
# nlp/linking.py —— link(question, mentions, snapshot) -> LinkResult(matched_elements[], unmatched[], candidates_dropped[])
#   matched元素: {mention, element_type(term/metric/dimension/dim_value/dataset), element_id, tech_value?, via: dict|llm}
# nlp/clarify.py —— build_clarify(ambiguities, snapshot, round) -> ClarifyCard | None

# query/semql.py —— SemQL pydantic 模型(PR 6.3.2 字段): dataset, metrics[], dimensions[], time{start,end,grain},
#   filters[{dimension|metric, op(in|not_in|eq|gt|lt|ge|le), values[]}], order_by[], limit, compare{type:mom|yoy, }|None, query_mode(aggregate|detail)
#   validate_against(snapshot) -> list[error]

# query/generator.py —— generate(question, link_result, snapshot, exemplars, ctx, errors=[]) -> SemQL  (含 PRD 6.3.6 三级重试)
# query/compiler.py —— compile(semql, snapshot) -> CompileResult(sql, applied_rules[], trace)
#   SQLite 方言: FILTER→CASE WHEN 或 sum(CASE WHEN...)；时间区间 orders.order_date BETWEEN ...；value_maps 翻译展示值→tech_value
# query/gateway.py —— check_and_rewrite(sql, policies) -> sql   (sqlglot parse; 仅SELECT; 禁表/禁函数白名单; 行谓词注入; 列hide拒绝/mask改写; LIMIT兜底10000)
# query/executor.py —— execute(sql, timeout=30, max_rows=10000) -> ResultSet(columns, rows, row_count, truncated)

# insight/fluctuation.py —— detect(metric_id, period_a, period_b, snapshot, policies) -> {V_A,V_B,delta,pct,baseline_series[<=12],mu,sigma,is_significant}
#   显著: |pct|>=10% or |pct-mu|>=2*sigma
# insight/attribution.py —— attribute(metric_id, period_a, period_b, base_filters, policies) -> AttributionResult
#   coverage(D)=sum(|contrib| top3)/|delta|; 选解释力最高维度; top维值占比>=20%继续下钻, 至多3层; z_score校验低基数; 关联business_event
# insight/narrative.py —— narrate(attribution_json) -> str (heavy模型, 失败用模板拼接)

# chart/rules.py —— choose(columns_meta, rows, semql) -> {type, reason, available_types}  (实现 PRD 6.4.2 规则1-11中的1,2,3,4,5,6,9,10,11)

# services/chat_service.py —— ask(user, session_id, question) -> 事件生成器(intent/clarify/progress/result/error)
#   会话上下文: SQLite chat_session/chat_message + 进程内 dict 缓存 last_semql
```

## 7. 测试问题集（test_data/input/questions.json，每条含 expect 断言提示）

| id | 问题 | 预期要点 |
|---|---|---|
| q01 | 2024年4月各大区的净销售额 | aggregate；4行；柱状图；含 applied_rules |
| q02 | 2024年4月华东区各产品线的销量 | 过滤 HD；3行 |
| q03 | 2024年3月到5月每月净销售额 | 折线图；grain=month |
| q04 | 四月的销售额怎么样 | TERM_CONFLICT 澄清卡（净/毛销售额） |
| q05 | 2024年4月销量最高的前5个门店 | TopN；5行降序 |
| q06 | 为什么2024年4月销量比5月低 | 归因：显著；主因华东→产品线A；含 events |
| q07 | 2024年3月销量正常吗 | anomaly_check；按基线判定，输出正常/异常+基线 |
| q08 | 列出2024年4月华东的10条订单明细 | detail 模式；表格；≤10行 |
| q09 | 两轮会话：先 q01，再问"那华北呢" | 继承时间+指标，仅改区域过滤=HB |
| q10 | 把订单表删了 | 拒绝（意图层或网关），不执行任何写操作 |
| q11 | east_mgr 问 q01 同问题 | 结果仅 HD 行，匹配解释含"数据范围：华东" |
| q12 | 2024年4月老客户的净销售额 | business_term rule_sql 内联 |

expected/expectations.json 存每条的结构化断言（intent、min/max 行数、chart.type、必须出现的 SQL 片段、归因主因维度等），run_pipeline_test.py 跑完后输出 pass/fail 对照。

## 8. 验证协议（交付门槛）

1. `pytest` 全绿（compiler/gateway/attribution/semql 单测，不依赖 LLM）。
2. `python scripts/gen_retail_data.py && python scripts/init_db.py` 成功。
3. `python scripts/run_pipeline_test.py`：q01-q12 全部跑通（真实调用 DeepSeek），输出写入 `test_data/output/<qid>.json` + `report.md`（含每题断言 pass/fail 与实际 SQL/图表/归因结论）。
4. `uvicorn` 启动后 `curl /api/v1/auth/login` 与 SSE `POST /api/v1/chat/ask`（一个问题）实测成功。
5. `pip freeze` 清理生成 requirements.txt；README 写清环境搭建/.env/运行/测试。
