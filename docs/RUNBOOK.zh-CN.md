[English](RUNBOOK.md) | [简体中文](RUNBOOK.zh-CN.md)

# InsightChat 运行手册

本手册说明如何在本机安装、初始化、运行、测试和排查 InsightChat。所有命令均在仓库根目录执行。命令按 POSIX Shell 语法书写；在 Windows 上可直接用 Git Bash 运行，语法不同处另给出 PowerShell 写法。

这些步骤背后的实现架构见 [IMPLEMENTATION_SPEC.zh-CN.md](IMPLEMENTATION_SPEC.zh-CN.md)；项目概览与面向用户的功能见 [../README.zh-CN.md](../README.zh-CN.md)。

## 1. 前置条件

- Python 3.10 或更高版本。推荐 Python 3.11；包声明为 `requires-python = ">=3.10"`。
- 一个 DeepSeek API Key，以及访问所配置 API 端点的出网 HTTPS 能力。
- 可选：用 conda 或 `venv` 创建隔离环境。
- 不需要外部数据库服务器、Redis、消息队列或向量库。元数据库和演示业务库都是由下列脚本创建的本地 SQLite 文件。

## 2. 创建 Python 环境

使用 conda：

```bash
conda create -n insightchat python=3.11 -y
conda activate insightchat
python --version
```

如果当前 Shell 中 `conda activate` 失败，可改用 `source activate insightchat`，或先执行一次 `conda init bash` 再重新打开终端。

在 Linux 或 macOS 上使用 `venv`：

```bash
python -m venv .venv
. .venv/bin/activate
```

在 Windows Git Bash 中，使用 `source .venv/Scripts/activate`；在 Windows PowerShell 中，激活脚本为 `.venv\Scripts\Activate.ps1`。

`python --version` 应显示刚创建的解释器版本。每打开一个新终端，都要重新激活环境。

## 3. 安装依赖

```bash
pip install -e .
```

推荐使用 `pip install -e .`：安装后 `chatbi` 可在任意目录导入，且修改 `chatbi/` 下的代码无需重新安装即可生效。

如果希望使用已固定的已知可用版本，可改为从依赖清单安装：

```bash
pip install -r requirements.txt
```

两种方式都会安装 FastAPI、uvicorn、`openai` SDK、sqlglot、jieba、pydantic、python-dotenv、sse-starlette、PyYAML、httpx 和 pytest。

## 4. 配置 `.env`

从模板创建本地配置文件：

```bash
cp .env.example .env
```

在 Windows PowerShell 中：

```powershell
Copy-Item .env.example .env
notepad .env
```

然后编辑 `.env`，至少填入真实的 `DEEPSEEK_API_KEY`。无论当前工作目录是什么，应用只读取仓库根目录下的 `.env`。

| 键 | 默认值 | 作用 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 无（必填） | DeepSeek API Key，只放在 `.env` 中。 |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容 API 的基础地址。 |
| `LLM_MODEL_HEAVY` | `deepseek-v4-pro` | 用于 SemQL 生成和归因结论的模型。 |
| `LLM_MODEL_LIGHT` | `deepseek-flash` | 用于改写、意图识别、精排和摘要的模型。 |
| `LLM_TIMEOUT_SEC` | `60` | 单次调用超时秒数。 |
| `META_DB_PATH` | `./data/meta.db` | 存放元数据、语义层、历史、反馈和记忆的数据库。 |
| `BIZ_DB_PATH` | `./test_data/data/retail.db` | 演示业务库，以只读方式打开。 |
| `AUTH_SECRET` | `change-me`（代码默认） | 登录令牌的 HMAC 密钥，本地演示之外必须替换。 |
| `TZ_NAME` | `Asia/Shanghai` | 解析相对时间表达式时使用的时区。 |
| `INSIGHT_SIGNIFICANT_PCT` | `5` | 判定波动显著的百分比阈值。 |
| `INSIGHT_SIGMA_K` | `2` | 判定波动显著的标准差倍数阈值。 |
| `MEMORY_SESSION_TTL_HOURS` | `72` | 会话上下文的存活时长。 |
| `MEMORY_CONTEXT_CACHE_SIZE` | `128` | 进程内会话上下文缓存容量。 |
| `MEMORY_TERM_PROMOTE_SESSIONS` | `3` | 术语选择晋升为长期偏好所需的不同会话数。 |
| `MEMORY_MIN_CONFIDENCE` | `0.35` | 使用长期记忆的有效置信度下限。 |
| `MEMORY_EXPLICIT_HALF_LIFE_DAYS` | `365` | 显式偏好的置信度半衰期。 |
| `MEMORY_INFERRED_HALF_LIFE_DAYS` | `90` | 推断偏好的置信度半衰期。 |

说明：

- `.env.example` 中的两个模型名只是示例。请把 `LLM_MODEL_HEAVY` 和 `LLM_MODEL_LIGHT` 设置为你的 DeepSeek 账号实际可用的模型 ID；名称写错会在第一次模型调用时报错。
- 数据库相对路径按仓库根目录解析，而不是按当前目录解析。
- 严禁提交 `.env`、打印 Key 或把 Key 写进日志。`.env` 已在 git 忽略列表中。

## 5. 验证模型连通性

```bash
python scripts/check_llm.py
```

该脚本会真实调用一次 light 模型（提问 `1+1=?`），打印回答，并确认 `llm_call_log` 已写入。它会先补齐缺失的元数据表，因此可以在 `scripts/init_db.py` 之前运行。返回 401 说明 Key 有误；返回 404 或模型不存在说明账号没有所配置的模型；超时通常说明本机无法访问该端点。

## 6. 初始化数据（按此顺序）

所有脚本都会导入 `chatbi.config`，而它要求 `DEEPSEEK_API_KEY` 非空；即使数据生成本身不调用模型，也要先完成第 4 节。

### 6.1 生成演示业务库

```bash
python scripts/gen_retail_data.py
```

> **具有破坏性：** 该脚本会删除并重建 `BIZ_DB_PATH` 指向的 SQLite 文件（默认 `test_data/data/retail.db`）。切勿对需要保留内容的数据库运行它。

数据生成是确定性的（固定 seed 42），产出 32 家门店、200 个客户、约 9.1k 条订单和约 18.3k 条订单行，日期范围为 2023-06-01 至 2024-06-30。脚本会打印“月份 × 大区”的销量透视表，以及 2024-04、2024-05 华东按产品线的拆分，便于肉眼核对内置的数据故事。

### 6.2 构建元数据库并发布语义层

```bash
python scripts/init_db.py
```

如果业务库不存在，脚本会打印提示后退出。否则它会：

1. 创建所有缺失的元数据表；
2. 幂等导入基础种子数据，并且仅在没有 published 版本时发布语义层快照 v1；
3. 对业务库真实执行每条 Evidence SQL 并保存结果；任一条执行失败，脚本以非零状态退出；
4. 重建 `data/dict/` 下的 jieba 词典。

可以安全地重复运行：基础数据仅在不存在时插入，已有记录保持不变，已发布的 v1 快照保持不变，Evidence 结果重新计算，词典重新构建。如果重新生成了 `retail.db`，请重跑 `init_db.py`，让保存的 Evidence 结果与新数据一致。

应用启动**不会**导入种子。在空的元数据库上启动服务只会创建缺失的表并加载词典；用户、角色、策略和语义层快照都由 `scripts/init_db.py` 写入。

### 6.3 完全重置（可选且具有破坏性）

如需从零开始，先停止服务，再删除两个生成的数据库：

```bash
rm -f data/meta.db test_data/data/retail.db
```

在 Windows PowerShell 中：

```powershell
Remove-Item data/meta.db, test_data/data/retail.db
```

以上是默认路径；如果你修改过 `META_DB_PATH` 或 `BIZ_DB_PATH`，请改为删除这两个变量指向的文件。

然后重新执行第 6.1 步和第 6.2 步。

> **具有破坏性：** 这会删除聊天历史、回答、查询日志、归因报告、反馈、长期记忆、审计日志、语义层快照和演示业务数据。如有需要，请先备份；如果 `META_DB_PATH` 或 `BIZ_DB_PATH` 指向真实数据库，请勿执行此操作。

## 7. 启动服务

```bash
python scripts/run_server.py
```

服务监听 `http://127.0.0.1:8000`，在浏览器打开该地址即可。终端会一直占用在服务进程上，按 `Ctrl+C` 停止。

如果 8000 端口已被占用，可直接用 uvicorn 换端口启动：

```bash
python -m uvicorn chatbi.api.main:app --host 127.0.0.1 --port 8001
```

前端通过相对路径 `/api/v1` 调用接口，因此换任何端口都能正常工作。

启动时会创建缺失的元数据表并热加载 jieba 词典。启动不会创建用户或导入语义对象，因此全新的元数据库仍需先运行 `scripts/init_db.py`，登录和数据问答才能成功。

## 8. 登录

打开 `http://127.0.0.1:8000`，使用下列演示账号之一。四个账号的演示密码均为 `Init@123456`；登录页已预填 `analyst`。

| 账号 | 角色 | 数据范围 |
| --- | --- | --- |
| `admin` | `admin` | 全部数据；可访问管理与语义浏览接口。 |
| `analyst` | `analyst` | 全部数据；可正常使用聊天、归因、反馈和记忆。 |
| `east_mgr` | `analyst`、`region_east` | 行策略把所有销售查询限定在华东大区（`dim_region = HD`）。 |
| `viewer1` | `viewer` | 演示只读账号。 |

`viewer1` 的限制需要特别注意：对于拥有 `viewer` 角色、但没有 `analyst` 或 `admin` 角色的用户，`POST /chat/ask` 会返回 `403`，因此 `viewer1` 无法在聊天界面提问。该账号仍可读取自己的历史和记忆，也可直接调用同步的 `POST /insight/attribute` 接口。聊天演示请使用 `analyst` 或 `east_mgr`。

这些账号和密码仅为演示数据。请更换 `AUTH_SECRET`，不要把演示登录暴露到本地环境之外。

## 9. 演示路径

请使用演示期内的明确日期。静态演示数据覆盖 2023-06-01 至 2024-06-30。

| 问题（在聊天框输入） | 预期表现 |
| --- | --- |
| `2024年4月各大区的净销售额` | 柱状图展示四个大区的数据；已应用规则包含“仅已完成订单”和“排除测试渠道”。 |
| `那华北呢` | 继承上一轮的时间和指标，区域过滤切换为华北（`HB`）。 |
| `2024年4月华东区各产品线的销量` | 华东三个产品线的数据行。 |
| `2024年3月到5月每月净销售额` | 2024 年 3 月至 5 月的月度折线图。 |
| `2024年4月销量最高的前5个门店` | 按降序展示的前 5 家门店。 |
| `为什么2024年4月销量比5月低` | 归因：波动显著，华东是主要贡献者，下钻指向产品线 A，并列出 4 月、5 月的业务事件。 |
| `2024年3月销量正常吗` | 基于历史基线的异常检查。 |
| `列出2024年4月华东的10条订单明细` | 明细模式，以表格展示。 |
| `2024年4月的销售额怎么样` | 弹出澄清卡片，因为 `销售额` 同时对应净销售额和毛销售额。 |
| `2024年4月老客户的净销售额` | `老客户` 业务术语被展开为“首单满一年”的规则 SQL。 |
| `把订单表删了` | 拒绝执行，不会产生任何写操作。 |

> **日期注意事项：** 演示数据截止到 2024-06-30，而“上个月”“本月”以及不带年份的月份等相对表达会按机器当前日期解析。数据集的默认时间窗口是 `last_month`，因此当机器日期不在演示期内时，这些问题返回零行是正常结果。演示时请使用 2024 年的明确日期；只有在机器日期位于演示期内时，相对时间演示才有意义。

## 10. 运行自动化测试

```bash
python -m pytest tests/ -q
```

单元测试不会调用模型，但并非没有依赖。运行前需要：

- `.env` 存在且 `DEEPSEEK_API_KEY` 非空；即使测试离线运行，导入项目时也会校验 Key；
- 已通过 `pip install -e .` 安装包（或在仓库根目录运行 pytest）；
- `scripts/init_db.py` 至少成功运行过一次，因为多个测试模块会加载已发布的语义层快照。

测试覆盖 SemQL 校验、SQL 编译、安全网关、归因、图表规则、澄清和记忆。测试数量和通过数会随用例增加而变化，请把一次全绿视为本地信号，而不是固定的交付保证。

## 11. 可选开发脚本

| 脚本 | 真实模型调用 | 作用 |
| --- | --- | --- |
| `python scripts/dev_query.py` | 否 | 手工构造四个 SemQL 示例，执行编译、网关和执行器，包含 `east_mgr` 的行策略场景。 |
| `python scripts/dev_e2e_llm.py` | 是 | 单题端到端（`q01`），以及 `q04` 的澄清卡片。 |
| `python scripts/dev_insight.py` | 是 | 输出 2024-04 与 2024-05 对比的完整归因 JSON 和结论，包含华东、产品线 A 的预期。 |
| `python scripts/dev_chat.py` | 是 | 对话编排：一个数据问题、一个“为什么”问题，以及一次两轮追问。 |
| `python scripts/run_pipeline_test.py` | 是 | 通过聊天服务跑 `q01`–`q12`，在 `test_data/output/` 下为每题写入一个 JSON 文件，并生成 `report.md`。 |

`dev_query.py` 是唯一不调用模型的开发脚本。其余脚本和全链路回归会真实调用 DeepSeek，需要网络、消耗 API 配额，耗时以分钟计而不是秒级。它们同样需要 `.env` 和已初始化的元数据库。

## 12. 故障排查

| 现象 | 可能原因 | 处理 |
| --- | --- | --- |
| `conda: command not found` | 未安装 conda，或当前 Shell 未为 conda 初始化 | 使用 Anaconda Prompt，或执行 `conda init bash` 后重新打开终端。 |
| `pip install` 超时 | 默认软件源不可达 | 换用可访问的镜像重试，例如 `pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`。 |
| `RuntimeError: 缺少环境变量 DEEPSEEK_API_KEY` | 仓库根目录缺少 `.env`，或 Key 为空 | 从 `.env.example` 创建 `.env` 并填入 Key；应用只读取根目录的 `.env`。 |
| `check_llm.py` 报 401 | Key 有误，或前后多了空格、引号 | 重写 `DEEPSEEK_API_KEY` 行，不要加引号，然后重开终端。 |
| API 返回模型不存在或 404 | `LLM_MODEL_HEAVY` 或 `LLM_MODEL_LIGHT` 未对账号开通 | 改成 DeepSeek 控制台中实际可用的模型 ID。 |
| `check_llm.py` 报超时 | 无法访问 `DEEPSEEK_BASE_URL` | 检查网络、代理、DNS 和基础地址。 |
| 登录返回 401 | 未导入用户种子，或应用连到了另一个元数据库 | 运行 `python scripts/init_db.py`，并检查 `META_DB_PATH`。 |
| `当前没有 published 的语义层版本` | 元数据库中没有已发布的语义层快照 | 运行 `python scripts/init_db.py`。 |
| 数据问题返回零行 | 日期不在 2023-06-01 至 2024-06-30 内，或相对时间按今天解析 | 改用 2024 年的明确日期；参见第 9 节的日期注意事项。 |
| `init_db.py` 提示业务库不存在并退出 | 尚未生成 `retail.db` | 先运行 `python scripts/gen_retail_data.py`。 |
| 8000 端口已被占用 | 其他进程占用了该端口 | 使用第 7 节的 `--port 8001` uvicorn 命令。 |
| 页面一直停在“正在理解您的问题…” | 模型调用失败或挂起 | 运行 `scripts/check_llm.py`，再查看服务日志。历史上 conda 的 `SSL_CERT_FILE` 问题已在 `chatbi/config.py` 内自愈；若环境仍失败，确认 `$CONDA_PREFIX/ssl/cacert.pem` 存在后重启服务。 |
| 图表只显示为普通表格 | ECharts CDN 未能加载 | 这是有意的表格降级。恢复网络后刷新页面。 |
| 澄清卡片提示已过期 | 服务重启过；待澄清状态只存在于进程内 | 重新提问。 |
| `/chat/ask` 返回 `403` | 当前登录的是 `viewer1` | 改用 `analyst` 或 `east_mgr` 登录。 |
| `ModuleNotFoundError: No module named 'chatbi'` | 未安装包，或命令不在仓库目录下运行 | 在仓库根目录执行 `pip install -e .`。 |

## 13. 清理

按 `Ctrl+C` 停止服务，然后可选地退出环境：

```bash
conda deactivate
```

删除环境（`conda env remove -n insightchat`）是可选项。删除生成的数据库具有破坏性；只有在可以接受丢失历史、反馈和记忆时，才执行第 6.3 节的重置流程。
