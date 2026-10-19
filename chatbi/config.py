"""集中式配置：从项目根 .env 读取全部环境变量（键名见 IMPLEMENTATION_SPEC.md 第 1 节）。

注意：DEEPSEEK_API_KEY 等机密只允许在代码中使用，严禁打印到日志/控制台。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# 项目根 = 包目录（chatbi/chatbi/）的上一级
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

# SSL_CERT_FILE 自愈：conda 激活脚本在 Windows 上可能把它指向不存在的 <env>/ssl/cacert.pem
# （真实文件在 <env>/Library/ssl/），httpx/openai 会因此抛 FileNotFoundError 导致 LLM 调用全部失败。
# 必须在任何 httpx/openai 导入之前修正。
_ssl_cert = os.environ.get("SSL_CERT_FILE")
if _ssl_cert is not None and not Path(_ssl_cert).exists():
    try:
        import certifi

        os.environ["SSL_CERT_FILE"] = certifi.where()
    except Exception:
        os.environ.pop("SSL_CERT_FILE", None)


def _require(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量 {key}，请检查 {PROJECT_ROOT / '.env'}")
    return value


def _abs(path_str: str) -> Path:
    """把 .env 里的相对路径解析为相对项目根的绝对路径。"""
    p = Path(path_str)
    return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()


# --- DeepSeek ---
DEEPSEEK_API_KEY: str = _require("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL: str = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
LLM_MODEL_HEAVY: str = os.environ.get("LLM_MODEL_HEAVY", "deepseek-v4-pro").strip()
LLM_MODEL_LIGHT: str = os.environ.get("LLM_MODEL_LIGHT", "deepseek-flash").strip()
LLM_TIMEOUT_SEC: int = int(os.environ.get("LLM_TIMEOUT_SEC", "60"))

# --- 数据库 ---
META_DB_PATH: Path = _abs(os.environ.get("META_DB_PATH", "./data/meta.db"))
BIZ_DB_PATH: Path = _abs(os.environ.get("BIZ_DB_PATH", "./test_data/data/retail.db"))

# --- 安全 / 区域 ---
AUTH_SECRET: str = os.environ.get("AUTH_SECRET", "change-me").strip()
TZ_NAME: str = os.environ.get("TZ_NAME", "Asia/Shanghai").strip()

# --- 派生目录 ---
DATA_DIR: Path = META_DB_PATH.parent                     # ./data
DICT_DIR: Path = DATA_DIR / "dict"                       # jieba 自定义词典目录
DICT_FILE: Path = DICT_DIR / "custom_dict.txt"
TEST_DATA_DIR: Path = PROJECT_ROOT / "test_data"
OUTPUT_DIR: Path = TEST_DATA_DIR / "output"


def ensure_dirs() -> None:
    """确保数据目录存在（初始化脚本启动时调用）。"""
    for d in (DATA_DIR, DICT_DIR, BIZ_DB_PATH.parent, OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)
