# InsightChat 启动与执行手册（保姆级）

> 从装好 Anaconda 的 Windows 机器开始，逐条复制执行即可。
> 终端一律使用 **Git Bash**（开始菜单搜 "Git Bash"）。文中 `【变体】` 为可选替代命令，按需选用。

---

## 第 0 步：进入项目目录

```bash
cd "e:/WorkInE/Intern/Deloitte/Chatbi/scheme/project/chatbi"
```

## 第 1 步：创建并激活 Python 环境

```bash
conda create -n insightchat python=3.11 -y
conda activate insightchat
```

【变体】激活失败时改用：`source activate insightchat`
【变体】想换环境名：把两处 `insightchat` 替换成你的名字（如 `chatbi_dev`）
【变体】环境已建好（重装/换机后第一次跳过创建）：直接 `conda activate insightchat`
✔ 验证：`python --version` 应显示 `Python 3.11.x`

## 第 2 步：安装依赖

```bash
pip install -r requirements.txt
```

【变体】开发模式安装（改代码即时生效，推荐）：`pip install -e .`

## 第 3 步：配置 .env（机密文件，严禁提交/外传）

项目根目录已有 `.env` 则**跳过本步**。没有则：

```bash
cp .env.example .env
```

然后用记事本打开编辑：`notepad .env`，把 `DEEPSEEK_API_KEY=` 后面填上真实 Key，保存。

## 第 4 步：验证 DeepSeek 连通（可选但强烈建议）

```bash
python scripts/check_llm.py
```

✔ 看到 `DeepSeek 连通性自检通过` 即成功；失败说明 Key 或网络有问题，先解决再继续。

## 第 5 步：初始化数据（只做一次；想重置就重做本步）

```bash
python scripts/gen_retail_data.py
python scripts/init_db.py
```

✔ 第一条打印"月度×区域销量透视表"；第二条末尾显示语义层对象计数，无报错即成功。
【变体】重置全部数据从零再来：`rm -f data/meta.db test_data/data/retail.db`，然后重跑上面两条。

## 第 6 步：启动服务并使用

```bash
python scripts/run_server.py
```

✔ 看到 `Uvicorn running on http://127.0.0.1:8000` 后，浏览器打开 **http://127.0.0.1:8000**

登录账号（密码统一 `Init@123456`）：

| 账号         | 能看到的数据               |
| ------------ | -------------------------- |
| `analyst`  | 全部（推荐演示用）         |
| `east_mgr` | 仅华东大区（演示行级权限） |
| `admin`    | 全部 + 管理接口            |
| `viewer1`  | 仅查看                     |

页面里可直接试：`2024年4月各大区的净销售额` → `那华北呢`（多轮）→ `为什么2024年4月销量比5月低`（归因）→ `四月的销售额怎么样`（澄清卡片）。

【变体】8000 端口被占用：`python -m uvicorn chatbi.api.main:app --host 127.0.0.1 --port 8001`，浏览器改开 8001
【变体】后台运行（关终端不退出）：`nohup python scripts/run_server.py > server.log 2>&1 &`，停止用 `taskkill //F //IM python.exe` 或 `ps aux | grep run_server` 后 `kill <PID>`
**停止服务**：在服务终端按 `Ctrl + C`。

## 第 7 步：跑测试（验证效果，不产生任何修改）

```bash
python -m pytest tests/ -q
```

✔ 显示 `56 passed` 即单元测试全绿（不调 LLM，秒级完成）。

【变体】全链路回归（真实调 DeepSeek，12 题约 2 分钟，结果写入 `test_data/output/`）：

```bash
python scripts/run_pipeline_test.py
notepad test_data/output/report.md   # 查看逐题 pass/fail 报告
```

【变体】只跑某个专项调试脚本（任选）：

```bash
python scripts/dev_query.py       # 不走 LLM 的 SQL 编译/权限/执行演示
python scripts/dev_e2e_llm.py     # 单题端到端（含澄清演示）
python scripts/dev_insight.py     # 归因引擎完整 JSON + 结论
python scripts/dev_chat.py        # 对话编排（含多轮"那华北呢"）
```

---

## 常见问题

| 现象                             | 处理                                                                                                                                                                                                                                                 |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `conda: command not found`     | 没用 Git Bash，或 Anaconda 未装/未配 PATH；改用 Anaconda Prompt 也可                                                                                                                                                                                 |
| `pip install` 报超时           | 换源：`pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple`                                                                                                                                                                |
| check_llm 报 401                 | `.env` 里 Key 填错或多了空格/引号                                                                                                                                                                                                                  |
| check_llm 报超时                 | 当前网络无法访问 api.deepseek.com，换网络重试                                                                                                                                                                                                        |
| 浏览器打不开 8000                | 确认启动窗口没关、无报错；或按第 6 步变体换端口                                                                                                                                                                                                      |
| 页面图表不显示只有表格           | ECharts CDN 加载失败的自动降级，属正常行为，联网后刷新即可                                                                                                                                                                                           |
| 页面一直卡在"正在理解您的问题…" | 旧版本曾因 conda 环境的 `SSL_CERT_FILE` 指向不存在的证书文件导致 LLM 调用全部失败。已在 `config.py` 内自愈；若仍遇到：`mkdir -p "$CONDA_PREFIX/ssl" && cp "$CONDA_PREFIX/Library/ssl/cacert.pem" "$CONDA_PREFIX/ssl/"`，然后重启服务并刷新页面 |

## 收尾

```bash
conda deactivate     # 退出环境（可选）
```

> 重置口诀：停服务（Ctrl+C）→ 第 5 步变体删库 → 第 5 步重建 → 第 6 步启动。
