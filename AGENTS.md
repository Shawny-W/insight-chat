# AGENTS.md — InsightChat（企业级 ChatBI · 本地可运行版）

> 本文件供 AI 编码代理阅读，帮助其在不了解项目的情况下快速上手。
> 权威文档：`README.md`（概览）、`docs/IMPLEMENTATION_SPEC.md`（**实现的唯一权威契约**，冲突时以它为准）、`docs/ChatBI_PRD.md`（产品需求）、`docs/RUNBOOK.md`（保姆级启动手册）。

## 1. 项目概览

InsightChat 是一个企业级对话式 BI（ChatBI）演示系统：用户用**中文自然语言**提问，系统基于 **Ontology 本体 + 语义层（MDL）** 将问题翻译为可信 SQL 并从业务库取数，自动选图呈现；对"为什么"类问题提供**自动归因洞察**（波动识别 → 贡献度下钻 → 事件关联 → LLM 结论）。

核心设计原则：

- **LLM 不直接写 SQL**：LLM 生成受限 SemQL（JSON DSL，定义见 `chatbi/query/semql.py`），由编译器确定性翻译成物理 SQL。
- **语义层版本化**：draft/published 双态 + 指标口径 Evidence（真实 SQL 复算）背书。
- **SQL 安全网关**：sqlglot AST 白名单（仅 SELECT）、行/列级权限注入、LIMIT 兜底（10000）。
- **归因引擎**：确定性贡献度分解 + 贪心下钻 + z_score 低基数校验，LLM 只负责文字表述。

处理链路（简）：

```
用户提问
  └→ 意图识别(LLM light) → 指代改写 → Schema Linking(jieba词典+LLM精排) → 澄清?
       └→ SemQL 生成(LLM heavy, 三级重试) → 编译器(SemQL→SQLite SQL, 规则注入)
            └→ SQL 安全网关(白名单+行/列权限+限额) → 执行器(biz SQLite)
                 └→ 图表自动选择 → 结果 + 摘要(LLM light)
"为什么"类问题 → 波动识别 → 归因引擎(贡献度下钻/事件关联) → LLM 结论(heavy)
```

## 2. 技术栈

- **语言**：Python ≥ 3.10（开发环境用 3.11 + conda，环境名 `insightchat`）
- **Web 框架**：FastAPI + uvicorn；SSE 用 sse-starlette；前端为 FastAPI 托管的单页 `chatbi/api/static/index.html`（原生 JS + ECharts CDN，CDN 失败自动降级表格）
- **LLM**：DeepSeek API（OpenAI 兼容协议，`openai` SDK 接入），双模型路由：重任务 `LLM_MODEL_HEAVY`（SemQL 生成、归因结论），轻任务 `LLM_MODEL_LIGHT`（意图、改写、精排、摘要）
- **存储**：SQLite × 2 —— 元数据库 `data/meta.db`（语义层、用户、权限、历史、反馈、llm_call_log），演示业务库 `test_data/data/retail.db`
- **关键库**：sqlglot（SQL AST 解析/网关）、jieba（中文分词 + 自定义词典做 schema linking）、pydantic v2、httpx、python-dotenv、PyYAML（语义层种子文件）

构建/打包：setuptools（见 `pyproject.toml`），`pip install -e .` 可编辑安装；`requirements.txt` 为锁定版本的真实依赖。

## 3. 目录结构与模块划分

```
chatbi/                      # 项目根（本文件所在处）
├── chatbi/                  # Python 包
│   ├── config.py            # 集中式配置：从项目根 .env 读取全部环境变量
│   ├── db.py                # SQLite 连接
│   ├── llm.py               # DeepSeek 调用封装：chat_json/chat_text、重试、llm_call_log
│   ├── security.py          # 密码哈希（pbkdf2 标准库）、HMAC 签名简易 token
│   ├── semantic/            # 语义层：schema.py(建表) store.py(快照/版本) seed.py dictionary.py(jieba词典)
│   │   └── seed/ontology_seed.yaml   # 本体种子（打包为 package-data）
│   ├── nlp/                 # prompts.py intent.py(意图识别) rewrite.py(多轮指代改写) linking.py(schema linking) clarify.py(澄清卡片)
│   ├── query/               # semql.py(DSL定义) generator.py(LLM生成+三级重试) compiler.py(SemQL→SQL) gateway.py(安全网关) executor.py
│   ├── insight/             # fluctuation.py(波动识别) attribution.py(归因引擎) narrative.py(LLM结论)
│   ├── chart/rules.py       # 图表自动选择规则
│   ├── services/            # chat_service.py(对话编排) query_service.py auth.py history.py feedback.py
│   └── api/                 # main.py(create_app) deps.py(统一错误包/trace_id) routes_*.py static/index.html
│       # 路由统一前缀 /api/v1：auth / chat / insight / history / feedback / admin
├── scripts/                 # 可执行脚本（详见第 5 节）
├── tests/                   # pytest 单元测试（不依赖 LLM）
├── test_data/               # input/questions.json(q01-q12) expected/expectations.json data/retail.db output/(回归产物)
├── data/                    # meta.db + dict/custom_dict.txt（jieba 词典，发布时重建的派生物）
└── docs/                    # PRD / IMPLEMENTATION_SPEC / RUNBOOK
```

## 4. 环境搭建与配置

```bash
conda create -n insightchat python=3.11 -y
conda activate insightchat
pip install -e .            # 或 pip install -r requirements.txt
```

`.env` 配置：对照 `.env.example` 逐项填写（**没有 .env 时先 `cp .env.example .env`**）。关键键：

| 键 | 说明 |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek API Key（**机密，只放 .env**） |
| `DEEPSEEK_BASE_URL` | 默认 `https://api.deepseek.com` |
| `LLM_MODEL_HEAVY` / `LLM_MODEL_LIGHT` | 重/轻任务模型，如 `deepseek-v4-pro` / `deepseek-flash` |
| `LLM_TIMEOUT_SEC` | LLM 调用超时秒数，默认 60 |
| `META_DB_PATH` / `BIZ_DB_PATH` | 元数据库/业务库路径（相对项目根解析为绝对路径） |
| `AUTH_SECRET` | token 签名密钥，生产必须改 |
| `TZ_NAME` | 业务时区，默认 `Asia/Shanghai`（相对时间词按此时区展开） |

初始化（只做一次；重置则删库重来）：

```bash
python scripts/gen_retail_data.py   # 生成演示业务库（确定性 seed=42）
python scripts/init_db.py           # 建 meta schema → 导种子 → 发布语义层 v1 → Evidence 复算 → 建词典
python scripts/check_llm.py         # 验证 DeepSeek 连通
```

运行：

```bash
python scripts/run_server.py        # FastAPI，浏览器打开 http://127.0.0.1:8000
```

演示账号（密码均为 `Init@123456`，仅演示）：`admin`（admin）、`analyst`（全量）、`east_mgr`（analyst + 行策略，仅华东大区 dim_region=HD，用于演示行级权限）、`viewer1`（仅查看）。

## 5. 测试与验证

```bash
python -m pytest tests/ -q            # 单元测试（不依赖 LLM）：semql/compiler/gateway/attribution/chart，预期 56 passed
python scripts/dev_query.py           # 不走 LLM：手工 SemQL 跑 compile→gateway→execute
python scripts/dev_e2e_llm.py         # 真实 LLM：q01 端到端 + q04 澄清
python scripts/dev_insight.py         # 归因引擎全量 JSON + narrative
python scripts/dev_chat.py            # 对话编排：q01/q06/q09 两轮
python scripts/run_pipeline_test.py   # q01-q12 全链路回归（真实调 DeepSeek，约2分钟），输出 test_data/output/report.md
```

测试约定：

- `tests/` 下单元测试**不调用 LLM**，秒级完成；部分用例（compiler/gateway）依赖**已初始化的 `data/meta.db`**（需先跑 `scripts/init_db.py`）。
- 全链路回归以 `test_data/input/questions.json`（q01-q12，含多轮、澄清、拒绝写操作、行级权限等场景）为输入，预期见 `test_data/expected/expectations.json`，逐题结果写入 `test_data/output/<qid>.json` 与 `report.md`。
- 项目无 lint/格式化工具配置（无 ruff/black/mypy），以现有代码风格为准。

## 6. 代码风格与开发约定

- **注释、docstring、日志、提交说明一律使用中文**；模块 docstring 常引用 spec 章节号（如"契约见 spec 第 6 节"）。
- 修改行为时优先核对 `docs/IMPLEMENTATION_SPEC.md` 的对应章节；该文件是模块契约的唯一权威来源。
- LLM 交互一律走 `chatbi/llm.py` 的 `chat_json(task, system, user, heavy=...)` / `chat_text(...)`，不要绕过（它负责重试、JSON 兜底解析、调用日志）。
- 业务 SQL 方言隔离在 compiler/gateway（sqlglot，方言 `sqlite`）；新增物理表需同步 `chatbi/query/gateway.py` 的 `BIZ_TABLES` 白名单。
- 语义层对象（指标/维度/模型/术语）变更通过 meta 库 + `semantic/store.py` 快照完成，不要硬编码到 NLP 逻辑里；词典 `data/dict/custom_dict.txt` 是发布派生物，由 `init_db.py`/发布流程重建，服务启动时热加载。
- 会话上下文为进程内缓存（本地单进程设计），不要引入 Celery/Redis 等外部依赖。

## 7. 安全注意事项（刚性要求）

- `DEEPSEEK_API_KEY`、`AUTH_SECRET` 等机密**只放 `.env`，严禁提交 git、严禁打印到日志/控制台**（`.gitignore` 已排除 `.env`、`*.pem`、`*.key`）。
- 所有业务查询 SQL 必须经 `chatbi/query/gateway.py`：sqlglot AST 白名单仅放行 SELECT；拒绝写操作/DDL/命令（INSERT/UPDATE/DELETE/DROP/ATTACH/PRAGMA 等）、危险函数（readfile/writefile/load_extension 等）、meta 库表；行/列权限与 LIMIT 兜底在网关注入，**不得绕过网关直接执行 SQL**。
- 密码哈希用标准库 pbkdf2，token 为 HMAC 签名简易 token（本地演示级实现；`AUTH_SECRET` 生产必须更换）。
- 不要引入未声明的第三方依赖；新依赖需同时更新 `pyproject.toml` 与 `requirements.txt`。

## 8. 平台与环境注意事项

- 开发环境为 **Windows + Git Bash**（终端用 Git Bash，不用 cmd/PowerShell 语法）；依赖管理用 conda 环境 `insightchat`。
- conda 在 Windows 上可能把 `SSL_CERT_FILE` 指向不存在的证书文件，导致 httpx/openai 全部失败——`chatbi/config.py` 已内置自愈逻辑（在任何 httpx/openai 导入前修正），**导入顺序上 config 必须最先加载**。
- 端口 8000 被占用时可用 `python -m uvicorn chatbi.api.main:app --host 127.0.0.1 --port 8001`。
