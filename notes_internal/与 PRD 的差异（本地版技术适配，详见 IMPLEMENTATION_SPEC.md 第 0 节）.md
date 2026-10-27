



## 与 PRD 的差异（本地版技术适配，详见 IMPLEMENTATION_SPEC.md 第 0 节）

- vLLM 私有化模型 → DeepSeek API（OpenAI 兼容，heavy/light 双模型）
- PostgreSQL+pgvector → SQLite（元数据）；ClickHouse → SQLite（业务库，方言经 compiler 隔离）
- 向量召回路 → 去掉（DeepSeek 无 embedding API），schema linking = jieba 词典 + LLM 精排两路
- Celery+Redis → 同步执行 + 进程内会话上下文缓存（本地单进程）
- bcrypt → pbkdf2（标准库）；JWT → HMAC 签名简易 token
- React 前端 → 单页 `static/index.html`（原生 JS + ECharts CDN，失败降级表格）