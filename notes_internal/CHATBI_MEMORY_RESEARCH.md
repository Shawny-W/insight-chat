# ChatBI 记忆系统调研报告

> 调研范围：主流开源 ChatBI 项目（SuperSonic、WrenAI，基于本地源码）、主流 Agent 记忆框架（Mem0、Zep、Letta/MemGPT、LangChain/LangGraph）、头部产品形态（ChatGPT Memory、Claude Memory）、遗忘机制的学术与工程做法。
> 所有结论均注明出处：源码结论附文件路径，外部资料附 URL。找不到的点明确写"未找到"，不做臆测。

---

## 一、调研对象与核心结论速览

| 对象 | 短期记忆（会话内） | 长期记忆（跨会话） | 用户级个性化记忆 | 遗忘机制 |
|---|---|---|---|---|
| **SuperSonic** | 上一轮解析结果回传 + LLM 多轮改写（窗口硬编码=1） | 问答对记忆（`s2_chat_memory` + 向量库 few-shot 召回） | **无**（记忆按 Agent 共享，非按用户） | **无 TTL**，仅评审启停 + 手动删除 |
| **WrenAI**（最新 main 分支） | **不实现**，外包给宿主 Agent（Claude 等） | Markdown 源真相 + LanceDB 向量索引的 NL→SQL 对 | **无**（项目级、Git 共享，无 user 维度） | **无 TTL**，显式 `forget` + 源文件同步式删除 |
| **Mem0** | 不内建（靠应用侧 `run_id` 作用域） | LLM 抽取事实 → ADD/UPDATE/DELETE/NOOP 冲突裁决 | **有**（`user_id` 强制隔离，是其核心卖点） | 无内建衰减；论文版由 LLM 判断矛盾后 DELETE，现行版需应用侧显式删 |
| **Zep (Graphiti)** | episode 摄入 | 时序知识图谱（实体+关系三元组） | 有（graph 按用户/组隔离） | **时间失效（invalidation）**：旧事实标记 `invalid_at` 而非物理删除，保留完整时间线 |
| **Letta/MemGPT** | Core memory（类比 RAM，每 prompt 可见） | Archival memory（类比磁盘，向量库分页调入） | 有（Human 块存用户事实） | Agent 通过工具调用**自我编辑**记忆；上下文将满时触发换页/压缩 |
| **LangGraph** | Checkpointer（thread 级状态持久化） | Store（跨线程，namespace 含 user_id） | 有（namespace 天然支持） | 无内建；官方模式是消息修剪/摘要 + 应用侧管理 |
| **ChatGPT / Claude** | 会话上下文 | 显式保存的记忆条目 + 历史对话引用（双轨） | **有**（产品标杆：可见、可编辑、可逐条删、项目级隔离） | 用户手动删；敏感话题默认不记；删除后 30 天内清除日志 |
| **Generative Agents**（论文） | — | 观察→反思→检索 三层 | 按角色隔离 | **检索评分衰减**：`score = recency + importance + relevance`，recency 指数衰减，不物理删除 |

**一句话结论**：开源 ChatBI 项目（SuperSonic/WrenAI）的记忆都停在"**问答对 few-shot 召回**"这一层，**没有一个做到用户级个性化记忆**；用户级记忆和遗忘机制的成熟做法在通用 Agent 记忆框架（Mem0/Zep/Letta）和论文（Generative Agents）里。ChatBI 要做"记住张三喜欢折线图"，需要把两套东西拼起来——这正是差异化机会。

---

## 二、开源 ChatBI 项目的记忆实现（源码级）

### 2.1 SuperSonic（腾讯）

源码：`project/supersonic/`（本地调研）。

#### 短期记忆：不存对话序列，两种机制替代

SuperSonic **不维护对话 message 序列**。名为 `ChatContext` 的结构（`chat/server/src/main/java/com/tencent/supersonic/chat/server/pojo/ChatContext.java`）只有 4 个字段（chatId、queryText、上一轮 SemanticParseInfo、user），持久化在 `s2_chat_context` 表，但全仓 grep 证实它**基本是"只写不读"的审计结构**——没有任何解析器读它。

真正生效的多轮上下文是两条：

1. **上一轮解析结果由前端回传（确定性沿用）**：前端把上一轮选中的 `SemanticParseInfo` 作为 `ChatParseReq.selectedParse` 回传（`chat/api/.../request/ChatParseReq.java:25`）；`ChatQueryContext` 构造函数（`headless/chat/.../ChatQueryContext.java:39-45`）用它直接种子化 `mapInfo.matchedElements`——即 schema 映射阶段直接继承上一轮识别到的指标/维度/维度值，无需重新匹配。
2. **LLM 多轮改写**：`NL2SQLParser.rewriteMultiTurn()`（`chat/server/.../parser/NL2SQLParser.java:169-212`）从 `s2_chat_query` 表取**最近 1 条**成功查询（窗口=1，硬编码），连同本轮问题一起交给 LLM 改写成独立完整问题，替换原问句后再走解析。该功能由 Agent 级 ChatApp 开关 `REWRITE_MULTI_TURN` 控制，**默认关闭**。

> 对照我们项目：我们的"追问改写 + last_semql 上下文继承"与 SuperSonic 机制 2 同构；它把窗口压到 1 并用改写代替历史注入，是工程上克制且有效的选择。

#### 长期记忆：ChatMemory 问答对体系（核心机制）

- **存什么**：`ChatMemory` POJO（`chat/server/.../pojo/ChatMemory.java`）= agentId + question + dbSchema + s2sql + status（PENDING/ENABLED/DISABLED）+ LLM 评审结果 + 人工评审结果。持久表 `s2_chat_memory`。
- **如何产生**（三通道）：
  1. LLM 路径查询成功后自动落一条 PENDING 记忆（`SqlExecutor.execute`，`SqlExecutor.java:43-59`）；
  2. 人工通过 `/api/chat/memory/createMemory` 添加，直接标人工评审 POSITIVE；
  3. Agent 配置的示例问题在开启记忆评审时自动执行入库。
- **如何评审启用**：`MemoryReviewTask` 定时任务（每 60 秒）用 LLM 审核未评审记忆，POSITIVE → ENABLED；用户反馈评分 ≥5 → ENABLED、≤1 → DISABLED（`ChatManageServiceImpl.updateFeedback:70-93`）。
- **如何检索复用**：ENABLED 记忆的 question 做 embedding，存入向量库 collection `memory_<agentId>`；`NL2SQLParser.addDynamicExemplars`（第 252-264 行）按当前问句召回 top-N（默认 10）作为 few-shot exemplar 拼入 S2SQL 生成 prompt 的 `#Exemplars:` 段（`OnePassSCSqlGenStrategy.generatePrompt:124-146`）。相似度 >0.989 的样本强制保留，其余按 few-shot 数量（默认 3~5）截取。

#### 关键短板

- **记忆是 Agent 级共享，不是用户级**：`ChatMemory` 无 user 维度（createdBy 仅审计），向量 collection 按 agentId 划分——张三记住的东西李四也会命中。全仓 grep `preference/Preference` 无任何匹配，**没有用户偏好记忆**。
- **无遗忘机制**：生命周期只有 PENDING → ENABLED/DISABLED 评审流和手动 `batchDelete`，无 TTL、无衰减。
- 多轮历史窗口硬编码为 1，无法配置。

### 2.2 WrenAI（Canner，最新 main 分支）

源码：`project/WrenAI/`。注意：该仓库已重构为"给外部 AI Agent 提供工具"的形态（Python CLI + MCP server），经典 wren-ai-service（Hamilton pipeline、Qdrant）已不存在。以下针对当前代码。

#### 短期记忆：不实现，外包给宿主 Agent

所有接口（CLI 命令、MCP tools）都是**无状态单次调用**，无 history/thread 参数。多轮衔接靠给宿主 Agent 的 Skill 文档约定：会话上下文由 Agent 自己的聊天历史维持，follow-up 由 Agent 改写/拆解问题后再调 Wren 的无状态工具（`core/wren/src/wren/skills_content/usage/SKILL.md`）。

> 对我们的启示：这是"框架不背锅"的极端做法。我们是自建服务端对话系统，无法外包，必须自己实现 thread 级记忆——这点 Wren 无可借鉴。

#### 长期记忆：Markdown 源真相 + 向量索引的双写架构

这是 WrenAI 最值得借鉴的设计：

- **源真相**：项目内 `knowledge/sql/<slug>.md`，每条问答对一个文件（YAML frontmatter：nl/sql/datasource/tags/source/created_at），**可 Git 评审、可共享、可 diff**（`core/wren/src/wren/memory/markdown.py`）。
- **派生索引**：LanceDB（默认 `<project>/.wren/memory/`，gitignored，可随时重建），`query_history` 表存 384 维向量（`memory/store.py:84-95`）。Embedding 模型 `paraphrase-multilingual-MiniLM-L12-v2`，支持 sentence-transformers / onnxruntime 双后端，无依赖时降级为 Grep 文本匹配（`memory/index_backend.py`）。
- **正样本准入**：查询成功 + 有明确 NL 问题 + 非探索性 SQL（裸 SELECT 无 WHERE/GROUP BY 判为 peek 不入库，`sql_classify.py`）+ **用户未否定**——"User says wrong → Do NOT store"（`usage/SKILL.md` 步骤 4）。只有满足全部条件才提示存储。
- **种子样本冷启动**：`memory/seed_queries.py` 从 MDL 用模板自动生成 NL→SQL 对（列表/聚合/分组/枚举过滤/JOIN 五类），打 `source:seed` 标签。
- **召回**：`MemoryStore.recall_queries()` 向量检索 top-3（可按 datasource 过滤），作为 few-shot 示例给 Agent 拼 prompt。

#### 遗忘机制：同步式删除，无时间衰减

- 无 TTL、无衰减、无容量上限（全库 grep `ttl|expir|decay|retention` 无业务逻辑命中）。
- `wren memory forget`：按 ID/按来源/交互勾选显式删除。
- **同步式遗忘**：以 Markdown 文件为准，索引中带 `origin:markdown-sync` 溯源标签、但文件已删除的行，下次 index 时自动从向量库清除（`MemoryStore.sync_markdown_queries()`，`store.py:686-719`）。
- 新鲜度靠内容指纹 watch（`memory/watch.py`）和 schema 的 `mdl_hash` 校验，而非时间。

#### 个性化：无

无 per-user 维度（query_history 表无 user 字段），知识是项目级、Git 共享的。

---

## 三、通用 Agent 记忆框架：用户级记忆与遗忘的成熟做法

开源 ChatBI 在用户级记忆上是空白，补齐它要参考通用框架。本节来源均为官方文档/论文，URL 见文末参考资料。

### 3.1 记忆分层：业界事实标准

LangChain 官方文档的定义最清晰，可直接照搬到 ChatBI：

| 层级 | 作用域 | 存什么 | 对应 ChatBI 场景 |
|---|---|---|---|
| **短期记忆** | thread（单次会话） | 对话状态、中间结果 | 本会话的澄清选择、上一轮 SemQL、指代改写所需上下文 |
| **长期记忆** | 跨 thread，namespace 含 user_id | 语义/情景/程序三类（见下） | 用户的图表偏好、口径偏好、常用维度、纠错过的问题对 |

长期记忆内部沿用认知心理学三分法（LangChain 官方文档、CoALA 论文）：

- **语义记忆（Semantic）**：事实——"张三看趋势图偏好折线图"、"张三说的'销售额'指净销售额"。**用户个性化靠这一层。**
- **情景记忆（Episodic）**：经历——历史问答对，以 few-shot 检索方式复用（SuperSonic/WrenAI 做的就是这一层）。
- **程序记忆（Procedural）**：规则——系统 prompt 本身；通过反思让 Agent 改写自己的指令。BI 场景对应"该用户提问时默认排除测试渠道"这类行为规则。

语义记忆的两种管理模式（LangChain 官方）：**Profile**（单一 JSON 档案，每次全量重写——简单但易丢信息）vs **Collection**（文档集合增量追加——召回率高但增删裁决复杂）。用户偏好画像适合 Profile 模式（字段少、结构稳定），问答对适合 Collection 模式。

### 3.2 Mem0：用户级记忆 + LLM 冲突裁决

来源：官方文档 docs.mem0.ai、论文 arXiv 2504.19413。

- **分层靠作用域 ID**：`user_id`（跨会话用户级）/ `agent_id` / `run_id`（单次会话），检索时强制按 ID 过滤隔离。
- **抽取**：LLM 从"滚动摘要 + 最近 m 条消息 + 新消息对"中抽取候选事实集，默认存提炼后的事实而非原文。
- **冲突裁决（论文版）**：每个候选事实先取向量库 top-10 最相似历史记忆，交给 LLM function-calling 四选一：**ADD**（无等价记忆新建）/ **UPDATE**（补充增强）/ **DELETE**（矛盾删除）/ **NOOP**。不需要单独分类器。
- **注意**：Mem0 官方博客称现行线上版本已退化为**单趟 ADD-only**，变更只新增不改写，纠错需应用侧显式调用。
- **无内建遗忘**：Mem0 官方有专文讨论遗忘（Ebbinghaus、LLM 判断式遗忘），但承认这是开放问题，产品未内置。

### 3.3 Zep (Graphiti)：时序知识图谱与"失效而非删除"

来源：论文 arXiv 2501.13956、Graphiti 文档。

- 记忆建模为**时序知识图谱**：节点=实体，边=事实三元组，以 episode（一段对话/数据）为单位摄入。
- **双时态建模**：每条事实边同时带"现实世界生效时间"（valid_from/valid_to）和"系统得知时间"（created_at/invalid_at）。
- **失效机制（最有价值的设计）**：事实被取代时**不删除不覆盖**，旧边标记 `invalid_at` 失效，新事实作为新边写入，保留完整时间线。天然支持"查询某个时间点什么为真"。
- 对照 Mem0 的物理 DELETE，Zep 的做法**可审计、可追溯**——对 BI 场景意义重大：指标口径会演进，"上季度你说的销售额口径是毛销售额，这季度改为净销售额"这类历史必须留痕。
- 代价：写入放大严重（每条消息触发实体抽取/消解/事实抽取/时间解析/失效判断一串 LLM 调用）。

### 3.4 Letta/MemGPT：记忆分层 + 自我编辑

来源：论文 arXiv 2310.08560。

- 把 LLM 上下文窗口类比 OS 的 RAM：**Core memory**（每 prompt 必带的小块：Human 块=用户事实、Persona 块=人设）/ **Recall memory**（完整对话历史，可检索）/ **Archival memory**（无限向量库存储，按需分页调入）。
- **自我编辑**：Agent 通过工具调用自己决定写什么、改什么、取什么——Core memory 写满时主动把旧内容换出到 Archival。
- 上下文将满时触发中断，执行压缩/摘要/换页。后续演进出 sleep-time compute（对话空闲期后台整理记忆）。

### 3.5 产品标杆：ChatGPT Memory / Claude Memory

来源：OpenAI 帮助中心、Anthropic 支持文档。这是用户对"记忆"产品形态的心智锚点：

- **双轨制**：显式保存的记忆条目（独立于聊天记录，删对话不删记忆）+ 从历史对话动态引用（RAG 式，带原文出处）。
- **完全用户可控**：查看/编辑/逐条删除/清空；对话中说"记住这个/忘掉这个"直接生效；临时/隐身聊天不产生记忆。
- **项目/工作区级隔离**：Claude 每个 Project 独立记忆空间，可关闭跨项目共享——对应企业 BI 的**租户/部门隔离**需求。
- **敏感话题默认不记**，需显式 opt-in。
- 引用记忆时展示来源（Sources），可追溯——对应 BI 场景"本次查询沿用了您的偏好：折线图"的留痕。

---

## 四、遗忘机制：学术与工程做法

### 4.1 经典公式：Generative Agents 检索评分

来源：论文 arXiv 2304.03442。这是最广为引用的记忆检索模型：

```
score = α_rec · recency + α_imp · importance + α_rel · relevance
```

- **recency（新近度）**：指数衰减，衰减因子 0.995/沙盒小时；**基于"最近访问时间"而非创建时间**——被召回会刷新它，类似 LRU 老化（TMLS 论文论证该评分本质是 LRU-K 的推广）。
- **importance（重要性）**：写入时由 LLM 打 1–10 分。
- **relevance（相关度）**：查询与记忆的 embedding 余弦相似度。
- 配套 **reflection**：周期性把低层观察合成为高层洞见记忆。

**核心思想：不做物理删除，只让低分记忆在检索中自然沉底。** 这是业界主流——调研中发现没有任何主流框架内建 TTL/LRU 物理清除，普遍靠"显式删除 + 检索评分衰减"替代。

### 4.2 艾宾浩斯遗忘曲线的应用

- 标准指数模型：`R = e^(−t/S)`（R=保持率，t=间隔，S=稳定度）；重复"复习"（再次访问）增大 S、拉平曲线——Duolingo 的 Half-Life Regression（ACL'16）在工业界验证过该模型。
- 应用于 Agent 记忆：检索评分中引入 Ebbinghaus 衰减项；有开发者报告在 LoCoMo 基准上，加时间衰减后"陈旧记忆精度"显著提升（dev.to 工程博客）。
- 学术研究：FSFM 框架（arXiv 2604.20300）基于 Ebbinghaus 做选择性遗忘；SLM 4.0（arXiv 2608.08253）对被取代的事实施加"非破坏式降级"（demotion 而非删除）——与 Zep 的失效机制异曲同工。

### 4.3 其他工程做法

- **记忆压缩/摘要归档**：对话超阈值（如 15 轮）触发 LLM 摘要，裁剪旧轮次，摘要作为 "as-of" 状态保留——LangGraph 短期记忆管理的标准模式。
- **同步式遗忘**：WrenAI 的做法——以可评审的源文件为准，源删除则索引同步清除。比时间衰减更可控。
- **混合分层**：近期上下文（全量）+ 滚动摘要 + 向量检索三层混合，是长对话的推荐实践（Oracle 工程博客）。

---

## 五、对本项目（InsightChat）的设计映射

结合调研结论，回答开篇的四个问题在我们产品上应如何落地。

### 5.1 用户级个性化记忆——"张三偏好折线图"怎么记

开源 ChatBI 无人做到这一层，方案需组合通用框架的做法：

**记忆数据模型（Collection 模式，按 user_id 隔离）**：

```
user_memory (
  id, user_id,                    -- 强制按用户隔离（学 Mem0 的 user_id 作用域）
  memory_type,                    -- semantic(偏好事实) / episodic(问答对) / procedural(行为规则)
  category,                       -- chart_preference / term_mapping / metric_default / ...
  key,                            -- 如 "chart:trend"（趋势类问题的图表偏好）
  value,                          -- 如 "line"（折线图）
  confidence,                     -- 置信度 0~1（见下）
  source,                         -- explicit(用户明说) / inferred(行为推断) / feedback(点赞点踩)
  valid_from, valid_to,           -- 学 Zep：被取代时标 valid_to 而非删除，留痕可审计
  created_at, last_accessed_at, access_count
)
```

**"折线图"例子的完整生命周期**：

1. **写入**：张三首次问趋势问题 → 出柱状图 → 张三说"换成折线图"。系统检测到图表切换事件，写一条 `category=chart_preference, key=chart:trend, value=line, source=explicit（用户显式要求）, confidence=1.0`。
2. **消费**：下次张三问趋势类问题，图表选择规则引擎先查用户偏好——`chart:trend` 有偏好 `line` 且置信度高于阈值 → 直接用折线图，并在留痕中标注"已沿用您的偏好：折线图"（学 ChatGPT 的 Sources 可追溯）。
3. **更新**：张三某次又把折线图切回柱状图 → 不覆盖旧记忆，旧记忆标 `valid_to=now`，写入新记忆（Zep 式失效）。同一 key 只取 valid_to 为空的当前值。
4. **推断类偏好降级**：若偏好是系统从行为推断的（用户连续 3 次把柱状图手动切成折线图，但从没明说过），`source=inferred`，初始置信度低（如 0.5），每被验证一次（用户没改回去）confidence 增加，被推翻一次立即失效。
5. **用户可管**：设置页可见/可删自己的记忆条目（学 ChatGPT/Claude）；对话中说"以后都用柱状图"直接改写（explicit，confidence=1.0）。

**澄清选择的长期化**：我们刚实现的"澄清记忆"（会话内记住"销售额=净销售额"）是典型的 thread 级短期记忆。若某用户多次会话都选择同一口径，应按 Mem0 模式把这条选择**晋升**为长期记忆：`category=term_mapping, key="销售额", value=metric_net_sales, source=inferred`——跨会话生效，新会话直接沿用并在假设中留痕。晋升门槛建议：≥3 个不同会话中做出同一选择。

### 5.2 短期记忆（会话内）

现状与建议：

| 机制 | 现状 | 调研依据的改进方向 |
|---|---|---|
| 指代改写 | 已实现（历史问句+LLM 改写） | SuperSonic 验证同构；窗口目前够用 |
| last_semql 上下文继承 | 已实现 | 对应 SuperSonic 的 selectedParse 回传 |
| 澄清记忆 | 已实现（进程内 dict） | 重启丢失；若需持久化，落库到 session 表即可，无需外部组件 |
| 会话过长 | 未处理 | 学 LangGraph：超 N 轮触发摘要压缩，摘要作为会话状态保留 |

### 5.3 长期记忆（跨会话）

分层设计（全部按 user_id 隔离）：

1. **语义记忆——用户偏好画像**（5.1 的 user_memory 表）：图表偏好、口径映射、常用维度。数据治理归属：**用户私有**。
2. **情景记忆——验证过的问答对**（学 SuperSonic ChatMemory + WrenAI 双写）：用户点赞/确认的问答对入库，embedding 检索作为 few-shot 注入 NL2SQL prompt。准入条件照抄 WrenAI 的正样本门槛：执行成功 + 用户未否定 + 非探索性查询。**注意隔离**：SuperSonic 的教训是 Agent 级共享导致记忆串味，我们的实现必须按 user_id（或按部门/角色共享，可配置）过滤召回。
3. **程序记忆——行为规则**：高置信偏好可升级为默认行为（"该用户提问时默认按净销售额口径"），与语义层的全局业务规则（rule_completed 等）分开存放，用户规则优先级高于全局默认、低于当次显式指定。

### 5.4 遗忘机制（时间序列）

组合四种做法，按记忆类型区别处理：

| 记忆类型 | 遗忘策略 | 依据 |
|---|---|---|
| 偏好事实（semantic） | **Zep 式失效**：被取代标 valid_to 不删除；**检索评分衰减**：`score = recency·w1 + confidence·w2 + relevance·w3`（Generative Agents 公式），90 天未被访问的 inferred 类记忆置信度自动衰减，低于阈值后不再生效但仍保留可查 | 口径/偏好会演进，历史必须可审计，不能物理删 |
| 问答对（episodic） | **WrenAI 式**：显式删除 + 点踩即禁用（SuperSonic 的 ≤1 分 DISABLED）；检索评分加 recency 项让陈旧问答对自然沉底；**不做物理 TTL** | 业界无人做物理 TTL；沉底即可 |
| 会话上下文（短期） | 会话结束即归档；进程内缓存按 LRU 淘汰，容量上限可配 | 对应 Letta 的换页思想 |
| 审计留痕 | **永不遗忘** | 企业 BI 合规要求 |

不推荐内建 Ebbinghaus 物理删除：调研证实主流框架均不这么做（Mem0 官方称其为开放问题），且 BI 场景的"旧口径记忆"有审计价值，降级沉底优于删除。

---

## 六、对比总结：各项目短板与我们的机会

1. **SuperSonic**：记忆体系最完整（评审流+向量召回），但共享不隔离、无偏好记忆、无遗忘——企业内多人共用会互相污染。
2. **WrenAI**：双写架构（可 Git 评审的源真相 + 可重建向量索引）和正样本准入最优雅，但完全放弃会话管理，无个性化。
3. **Mem0/Letta**：用户级记忆成熟，但不懂 BI 语义（不知道什么是口径、维度、图表）。
4. **Zep**：失效机制最契合口径演进场景，但写放大成本对 BI 高频查询过重——只取"失效不删除+双时间戳"的思想即可，不必上知识图谱。
5. **我们的机会**：**"BI 语义感知 + 用户级隔离 + 失效留痕"三者结合的记忆系统是空白点**——开源项目没有一个做到。最小可行路径：user_memory 表（5.1 模型）+ 澄清记忆晋升机制 + 偏好注入图表选择/口径匹配两个消费点，即可覆盖用户举例的折线图场景。

---

## 参考资料

**源码（本地仓库）**
- SuperSonic：`project/supersonic/`（关键文件已在 2.1 节逐条标注路径）
- WrenAI：`project/WrenAI/`（关键文件已在 2.2 节逐条标注路径）

**论文**
- Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory — https://arxiv.org/html/2504.19413v1
- Zep: A Temporal Knowledge Graph Architecture for Agent Memory — https://arxiv.org/abs/2501.13956
- MemGPT: Towards LLMs as Operating Systems — https://arxiv.org/abs/2310.08560
- Generative Agents: Interactive Simulacra of Human Behavior — https://arxiv.org/html/2304.03442v2
- Duolingo Half-Life Regression — https://research.duolingo.com/papers/settles.acl16.pdf
- FSFM（选择性遗忘框架）— https://arxiv.org/html/2604.20300v2
- SLM 4.0（非破坏式降级）— https://arxiv.org/html/2608.08253v1

**官方文档**
- Mem0 Memory Types — https://docs.mem0.ai/core-concepts/memory-types
- LangChain Memory Overview — https://docs.langchain.com/oss/python/concepts/memory
- Memory in ChatGPT — https://help.openai.com/en/articles/8590148-memory-faq
- Claude chat search and memory — https://support.claude.com/en/articles/11817273-use-claude-s-chat-search-and-memory-to-build-on-previous-context
- DB-GPT Long-term Memory — https://github.com/eosphoros-ai/DB-GPT/blob/main/docs/docs/agents/modules/memory/long_term_memory.md

**工程博客**
- Mem0: Memory eviction and forgetting in AI agents — https://mem0.ai/library/agent-memory/memory-eviction-and-forgetting-in-ai-agents
- Ebbinghaus 衰减实现 — https://dev.to/sachit_mishra_686a94d1bb5/i-built-memory-decay-for-ai-agents-using-the-ebbinghaus-forgetting-curve-1b0e
- TMLS: Hierarchical Memory for Agents — https://www.tmls.nyc/research/hierarchical-agent-memory-paging
- Oracle: Which Agent Memory Approach Is Best — https://blogs.oracle.com/developers/which-agent-memory-approach-is-best-for-long-conversations
- Text2SQL + 长期记忆探索 — https://github.com/MKcodeshere/Text2SQl-Agent-with-Long-Term-Memory
