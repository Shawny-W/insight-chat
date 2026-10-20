# InsightChat（企业级对话式 BI）

用户用自然语言提问，系统基于 **Ontology 本体 + 语义层（MDL）** 把问题翻译为可信 SQL 并从业务库取数，自动选图呈现；对"为什么"类问题提供**自动归因洞察**（波动识别 → 贡献度下钻 → 事件关联 → LLM 结论）。

特性：

- 语义层版本化（draft/published）+ 指标口径 Evidence（真实 SQL 复算）背书
- LLM 不直接写 SQL：生成受限 SemQL（JSON DSL），编译器确定性翻译成物理 SQL
- Schema linking 两路：jieba 自定义词典精确匹配 + LLM 精排；澄清卡片消歧（术语多义等）
- SQL 安全网关：sqlglot AST 白名单（仅 SELECT）、行/列级权限注入、限额兜底
- 归因引擎：确定性贡献度分解 + 贪心下钻 + z_score 低基数校验，LLM 只负责表述
- 多轮对话（指代改写继承上下文）、查询历史、反馈回流（👍 沉淀 few-shot）
- 单页演示前端（原生 JS + ECharts，CDN 失败自动降级表格）

## 架构（简）

```
用户提问
  └→ 意图识别(LLM light) → 指代改写 → Schema Linking(jieba词典+LLM精排) → 澄清?
       └→ SemQL 生成(LLM heavy, 三级重试) → 编译器(SemQL→SQLite SQL, 规则注入)
            └→ SQL 安全网关(白名单+行/列权限+限额) → 执行器(biz SQLite)
                 └→ 图表自动选择 → 结果 + 摘要(LLM light)
"为什么"类问题 → 波动识别 → 归因引擎(贡献度下钻/事件关联) → LLM 结论(heavy)
```

存储：元数据 SQLite（`data/meta.db`）；演示业务库 SQLite（`test_data/data/retail.db`）；jieba 词典（`data/dict/custom_dict.txt`，发布时重建的派生物）。

## 环境搭建

```bash
conda create -n insightchat python=3.11 -y
conda activate insightchat
pip install -e .            # 或 pip install -r requirements.txt
```

## .env 配置

对照 `.env.example` 逐项填写（**DeepSeek Key 等机密只放 .env，严禁提交 git / 打印日志**）：

| 键                    | 说明                                                       |
| --------------------- | ---------------------------------------------------------- |
| `DEEPSEEK_API_KEY`  | DeepSeek API Key（机密）                                   |
| `DEEPSEEK_BASE_URL` | 默认 `https://api.deepseek.com`                          |
| `LLM_MODEL_HEAVY`   | 重任务模型（SemQL 生成、归因结论），如 `deepseek-v4-pro` |
| `LLM_MODEL_LIGHT`   | 轻任务模型（意图/改写/精排/摘要），如 `deepseek-flash`   |
| `LLM_TIMEOUT_SEC`   | LLM 调用超时秒数，默认 60                                  |
| `META_DB_PATH`      | 元数据库路径，默认 `./data/meta.db`                      |
| `BIZ_DB_PATH`       | 业务库路径，默认 `./test_data/data/retail.db`            |
| `AUTH_SECRET`       | token 签名密钥，生产必须改                                 |
| `TZ_NAME`           | 业务时区，默认 `Asia/Shanghai`                           |

## 初始化

```bash
python scripts/gen_retail_data.py   # 生成演示业务库（确定性 seed=42，打印月度×区域透视表）
python scripts/init_db.py           # 建 meta schema → 导种子 → 发布语义层 v1 → Evidence 真实复算 → 建词典
python scripts/check_llm.py         # 验证 DeepSeek 连通（light 模型问 1+1=?）
```

## 运行

```bash
python scripts/run_server.py        # 启动 FastAPI
# 浏览器打开 http://127.0.0.1:8000
```

演示账号（密码均为 `Init@123456`，仅演示）：

| 账号         | 角色             | 数据范围                    |
| ------------ | ---------------- | --------------------------- |
| `admin`    | admin            | 全部                        |
| `analyst`  | analyst          | 全部                        |
| `east_mgr` | analyst + 行策略 | 仅华东大区（dim_region=HD） |
| `viewer1`  | viewer           | 仅查看                      |

## 测试

```bash
python -m pytest tests/ -q            # 单元测试（不依赖 LLM）：semql/compiler/gateway/attribution/chart
python scripts/dev_query.py           # 不走 LLM：手工 SemQL 跑 compile→gateway→execute
python scripts/dev_e2e_llm.py         # 真实 LLM：q01 端到端 + q04 澄清
python scripts/dev_insight.py         # 归因引擎全量 JSON + narrative
python scripts/dev_chat.py            # 对话编排：q01/q06/q09 两轮
python scripts/run_pipeline_test.py   # q01-q12 全链路回归，输出 test_data/output/report.md
```

## 目录结构

```
chatbi/
├── chatbi/                 # 包
│   ├── config.py / db.py / llm.py / security.py
│   ├── semantic/           # schema.py(建表) store.py(快照/版本) seed.py dictionary.py seed/ontology_seed.yaml
│   ├── nlp/                # prompts.py intent.py rewrite.py linking.py clarify.py
│   ├── query/              # semql.py generator.py compiler.py gateway.py executor.py
│   ├── insight/            # fluctuation.py attribution.py narrative.py
│   ├── chart/rules.py
│   ├── services/           # chat_service.py query_service.py auth.py history.py feedback.py
│   └── api/                # main.py deps.py routes_*.py static/index.html
├── scripts/                # init_db.py gen_retail_data.py check_llm.py run_server.py dev_*.py run_pipeline_test.py
├── tests/                  # pytest（不依赖 LLM）
└── test_data/              # input/questions.json expected/expectations.json data/retail.db output/
```
