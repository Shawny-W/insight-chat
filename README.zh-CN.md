[English](README.md) | [简体中文](README.zh-CN.md)

# InsightChat：具备用户级记忆的可审计 ChatBI

InsightChat 是一个可在本地运行的对话式 BI 应用，将自然语言分析与持久化、按用户隔离的记忆系统结合起来。它把问题转换为经过校验的 SemQL，而不是轻信 LLM 直接编写的 SQL；随后确定性编译 SQL，通过安全网关执行数据控制，并解释答案以及塑造答案时实际使用的记忆。

项目最具特色的能力是 **BI 感知的记忆闭环**：已完成的会话上下文可以跨服务重启恢复，系统可以按用户学习图表与指标偏好，重复的澄清选择可以晋升为长期映射，点赞的回答可以成为个人 few-shot 示例。置信度衰减、逻辑删除、历史记录与 `memory_trace` 让这种个性化过程可检查、可撤销。

其他已实现的亮点包括：

- **受限的 NL2SQL：**Schema Linking、确定性澄清、SemQL 校验、规则化后处理，以及确定性的 SQLite 编译器。
- **独立于 LLM 的安全控制：**仅允许 SELECT 的 AST 检查、表/函数限制、行/列策略处理、失败即关闭的行过滤、超时与行数限制。
- **可解释的洞察归因：**波动识别、基于贡献度的下钻、层级约束、低基数提示、事件关联，以及带模板降级的 LLM 结论表述。
- **语义证据与留痕：**已发布的种子快照、初始化时真实执行的 Evidence SQL、已应用规则、最终 SQL、语义版本和记忆使用记录。
- **轻量完整的演示：**FastAPI、两个 SQLite 数据库，以及带 ECharts 和表格降级的原生 JavaScript 界面，不需要向量数据库或独立前端构建。

## 用户级记忆系统

记忆系统包含两层，均按 `user_id` 隔离：

| 层级 | 保存内容 | 使用方式 |
| --- | --- | --- |
| 短期会话上下文 | 最近对话轮次、上一轮 SemQL 快照、术语/维度值澄清选择，以及最近回答引用 | 上下文追问、会话内消歧和图表切换 |
| 长期用户记忆 | `chart_preference`、`term_mapping` 和 `query_exemplar` | 分场景图表个性化、自动术语消歧和个人 few-shot 示例 |

核心行为：

- **跨重启上下文：**已完成的会话状态持久化到 SQLite，并使用进程内 LRU 加速。挂起的澄清卡片仅存在于进程内，重启后失效。
- **分场景图表偏好：**趋势分析偏好不会变成无关的全局图表设置。当次图表措辞只影响当前回答，兼容的长期偏好随后才会应用。
- **切图不重新查数：**通过对话或工具栏切图时，复用属于当前用户的结果快照。快照最多包含 200 行，并保留截断信息。
- **澄清学习：**术语选择会先记住在当前会话中。默认在三个不同会话中做出相同选择后，候选映射才晋升为生效的长期映射；候选记忆不会被应用。
- **个人示例与全局治理分离：**点赞成功回答会创建个人示例，点踩会撤销。个人示例采用字面重合度召回，不使用向量。独立的全局示例链路仍要求三个不同用户确认。
- **避免自我续期的衰减：**有效置信度以 `last_confirmed_at` 为基准衰减；仅使用记忆只会更新访问统计，不会延长其寿命。
- **用户控制与可审计性：**用户可以查看和删除自己的记忆。取代与删除均为逻辑操作，历史值和事件仍保留用于审计。“遗忘”不是物理擦除。
- **使用过程可见：**实际应用的长期偏好和个人示例会记录到 `memory_trace`，并显示在界面的留痕区域。

登录后可以尝试以下对话命令：

```text
以后趋势都用折线图
记住销售额指净销售额
换成柱状图
你记得我什么
忘掉销售额口径
忘掉全部记忆
```

界面的清空按钮只移除长期记忆；对话命令 `忘掉全部记忆` 还会重置当前会话上下文。记忆不会绕过 SemQL 校验、SQL 编译、安全网关或数据权限。

详细能力与边界见[记忆系统指南](docs/MEMORY_SYSTEM.zh-CN.md)、[功能与设计亮点](docs/MEMORY_SYSTEM_FEATURES.zh-CN.md)和[手工验收清单](docs/MEMORY_SYSTEM_TEST_SCRIPTS.zh-CN.md)。

## 工作原理

```text
用户问题
  -> 严格的记忆命令处理
  -> 上下文改写
  -> 意图识别
  -> 词典/索引匹配 + 受限的 LLM 精排
  -> 必要时进行确定性澄清
  -> 带重试的 SemQL 生成与校验
  -> 确定性的 SemQL 到 SQLite 编译
  -> SQL 安全网关与策略改写
  -> 只读执行
  -> 图表规则 + 记忆个性化
  -> 回答、摘要、证据与留痕

为什么/异常分析问题
  -> 波动识别
  -> 确定性贡献归因与事件关联
  -> LLM 表述或模板降级
```

LLM 不生成可执行的业务 SQL。洞察查询与普通数据问题复用相同的编译器、网关和执行器，因此指标规则和行/列控制始终位于计算链路中。

当前语义层由种子发布的 v1 快照、按版本读取快照、指标/维度/术语定义、业务规则和初始化时的 Evidence 检查组成，不包含草稿、审核或批准工作流。

## 快速开始

### 1. 前置条件

- Python 3.10 或更高版本，推荐 Python 3.11。
- 以下命令使用 Conda，也可以使用等效的 Python 环境管理工具。
- 自然语言查询需要 DeepSeek API Key 和网络连接。离线单元测试虽不调用 API，但导入配置时仍要求该配置值存在。

从仓库根目录执行：

```bash
conda create -n insightchat python=3.11 -y
conda activate insightchat
pip install -e .
```

也可以使用 `pip install -r requirements.txt` 代替开发模式安装。

### 2. 配置应用

```bash
cp .env.example .env
```

在 Windows 命令提示符中改用 `copy .env.example .env`。编辑 `.env`，至少设置：

| 配置键 | 用途 |
| --- | --- |
| `DEEPSEEK_API_KEY` | 必需的 API 凭据，必须保密 |
| `DEEPSEEK_BASE_URL` | OpenAI 兼容端点，默认 `https://api.deepseek.com` |
| `LLM_MODEL_HEAVY` | 用于 SemQL 生成和洞察表述的模型 |
| `LLM_MODEL_LIGHT` | 用于意图识别、改写、精排和摘要的模型 |
| `AUTH_SECRET` | HMAC token 密钥；除本地演示外，应更换默认值 |

`.env.example` 中的模型名称是配置示例，不代表账号一定可用。请设置为你的 DeepSeek 账号已开通的模型。数据库路径、时区、洞察阈值和记忆默认值也在 `.env.example` 中说明；相对数据库路径从项目根目录解析。

### 3. 初始化演示

```bash
python scripts/gen_retail_data.py
python scripts/init_db.py
python scripts/check_llm.py
```

- `gen_retail_data.py` 会使用确定性数据**重建演示业务库**。不要对需要保留的数据运行此脚本。
- `init_db.py` 创建元数据表，在没有发布版本时导入种子并发布 v1，对业务库执行 Evidence SQL，并重建 jieba 词典。正常的幂等初始化流程可以重复运行。
- `check_llm.py` 会实际调用一次 light 模型并验证调用日志。只有不希望发起该 API 调用时才省略此步骤。

零售演示数据覆盖 **2023-06 至 2024-06**。提问时请使用明确日期：“上个月”等相对表达按机器当前日期展开，可能落在演示数据范围之外。

### 4. 启动并使用应用

```bash
python scripts/run_server.py
```

打开 <http://127.0.0.1:8000>。服务仅监听回环地址，按 `Ctrl+C` 停止。

所有演示账号的密码均为 `Init@123456`：

| 账号 | 角色与行为 |
| --- | --- |
| `analyst` | 可查询全部演示数据，推荐用于主要演示 |
| `east_mgr` | analyst 角色加上行策略，仅能查看华东（`HD`）结果 |
| `admin` | 可通过管理角色守卫并访问全部演示数据 |
| `viewer1` | viewer 角色；`/chat/ask` 会被拒绝，适合检查角色边界 |

可以尝试：

```text
2024年4月各大区的净销售额
那华北呢
2024年4月的销售额怎么样
为什么2024年4月销量比5月低
```

第三个问题演示指标术语澄清。界面展示已应用规则、SQL/SemQL、语义版本、数据范围解释和记忆留痕。如果 ECharts CDN 无法加载，结果会降级为表格。

可移植的分步操作与故障排查见[运行手册](docs/RUNBOOK.zh-CN.md)。

## 测试与验证

完成配置和元数据初始化后，运行标准测试：

```bash
python -m pytest tests/ -q
```

单元测试不会调用 LLM。部分测试会加载已初始化的语义快照，导入项目配置时也要求存在 `DEEPSEEK_API_KEY`。

其他检查：

```bash
python scripts/dev_query.py           # No LLM: compile, gateway, permissions, execution
python scripts/dev_e2e_llm.py         # Real LLM: query and clarification
python scripts/dev_insight.py         # Real LLM narrative plus deterministic attribution
python scripts/dev_chat.py            # Real LLM: query, insight, and follow-up orchestration
python scripts/run_pipeline_test.py   # Real LLM: q01-q12 regression; writes test_data/output/
```

全链路回归和多数开发脚本会产生模型 API 调用。手工记忆清单覆盖单元测试未完整覆盖的界面行为。

## 文档

- [运行手册](docs/RUNBOOK.zh-CN.md)：安装、启动、重置注意事项和故障排查。
- [实现概览](docs/IMPLEMENTATION_SPEC.zh-CN.md)：实际架构、模块、API、数据和限制。
- [记忆系统](docs/MEMORY_SYSTEM.zh-CN.md)：存储、学习、衰减、API、安全与降级。
- [记忆功能与设计亮点](docs/MEMORY_SYSTEM_FEATURES.zh-CN.md)：面向使用者的简明导览。
- [记忆手工验收清单](docs/MEMORY_SYSTEM_TEST_SCRIPTS.zh-CN.md)：包含副作用与清理步骤的可复现界面场景。
- [权限控制](docs/PERMISSIONS.zh-CN.md)：认证、角色守卫、行/列控制、网关强制执行和已知缺口。

## 当前边界

InsightChat 是本地演示项目，不是生产部署包。当前使用单进程、单 SQLite 业务库和以种子为中心的语义发布流程。挂起澄清与上下文缓存均为进程本地状态。HMAC token 不支持服务端吊销，查询链路没有强制校验数据集策略，前端在选择历史会话时也不会恢复既往消息记录。实现与权限指南详细说明了这些限制。
