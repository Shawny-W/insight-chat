"""DeepSeek LLM 封装（OpenAI 兼容协议），按 IMPLEMENTATION_SPEC.md 第 2 节实现。

- chat_json(task, system, user, heavy=False, temperature=0) -> dict
- chat_text(...) -> str
- 网络异常指数退避重试 2 次（1s/3s），仍失败抛 LLMUnavailableError
- chat_json 解析失败抛 LLMFormatError，由调用方走重试链
- 每次调用落 llm_call_log（meta 库）；API Key 绝不打印
"""

import json
import logging
import re
import time

from openai import OpenAI, APIConnectionError, APITimeoutError, InternalServerError

from chatbi.config import (
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    LLM_MODEL_HEAVY,
    LLM_MODEL_LIGHT,
    LLM_TIMEOUT_SEC,
)

logger = logging.getLogger(__name__)


class LLMUnavailableError(Exception):
    """LLM 服务网络层不可用（已重试 2 次仍失败）。"""


class LLMFormatError(Exception):
    """LLM 返回内容无法解析为 JSON（由调用方走重试链）。"""


_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url=DEEPSEEK_BASE_URL,
            timeout=LLM_TIMEOUT_SEC,
            max_retries=0,  # 重试由本模块自行控制（指数退避 1s/3s）
        )
    return _client


def _log_call(task: str, model: str, system: str, user: str,
              response: str, latency_ms: int, error: str | None = None) -> None:
    """每次调用落 llm_call_log；日志表不存在时静默跳过（初始化前的探测调用）。"""
    try:
        from chatbi.db import get_meta_conn

        conn = get_meta_conn()
        try:
            conn.execute(
                "INSERT INTO llm_call_log (task, model, prompt, response, latency_ms, error) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task, model, f"[system]\n{system}\n[user]\n{user}", response, latency_ms, error),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - 日志失败绝不能阻断主链路
        logger.warning("llm_call_log 写入失败（忽略）: %s", exc)


def _extract_balanced_json(text: str) -> str | None:
    """从文本中提取首个配平 {} 的 JSON 子串（兼容 ```json 包裹与前后废话）。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


# 需要重试的网络层异常
_RETRYABLE = (APIConnectionError, APITimeoutError, InternalServerError, ConnectionError, TimeoutError)
_BACKOFF_SEC = (1, 3)  # 重试 2 次


def _call(task: str, system: str, user: str, heavy: bool, temperature: float,
          json_mode: bool) -> str:
    """底层调用：含网络重试与日志落库，返回原始文本。"""
    model = LLM_MODEL_HEAVY if heavy else LLM_MODEL_LIGHT
    client = _get_client()
    kwargs = {}
    if json_mode:
        # DeepSeek 要求：使用 JSON 输出时 system 中必须含 "JSON" 字样
        if "JSON" not in system.upper():
            system = system + "\n请仅以合法 JSON 对象输出。"
        kwargs["response_format"] = {"type": "json_object"}

    last_exc: Exception | None = None
    for attempt in range(len(_BACKOFF_SEC) + 1):
        t0 = time.monotonic()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                **kwargs,
            )
            text = resp.choices[0].message.content or ""
            latency_ms = int((time.monotonic() - t0) * 1000)
            _log_call(task, model, system, user, text, latency_ms)
            return text
        except _RETRYABLE as exc:
            latency_ms = int((time.monotonic() - t0) * 1000)
            last_exc = exc
            _log_call(task, model, system, user, "", latency_ms, error=f"{type(exc).__name__}: {exc}")
            if attempt < len(_BACKOFF_SEC):
                time.sleep(_BACKOFF_SEC[attempt])
        except Exception as exc:  # noqa: BLE001 - 鉴权/参数等错误不重试
            latency_ms = int((time.monotonic() - t0) * 1000)
            _log_call(task, model, system, user, "", latency_ms, error=f"{type(exc).__name__}: {exc}")
            raise LLMUnavailableError(f"LLM 调用失败（非网络类，不重试）: {type(exc).__name__}: {exc}") from exc
    raise LLMUnavailableError(f"LLM 服务不可用（已重试 {len(_BACKOFF_SEC)} 次）: {last_exc}") from last_exc


def chat_text(task: str, system: str, user: str, heavy: bool = False,
              temperature: float = 0.0) -> str:
    """自由文本调用。"""
    return _call(task, system, user, heavy, temperature, json_mode=False).strip()


def chat_json(task: str, system: str, user: str, heavy: bool = False,
              temperature: float = 0.0) -> dict:
    """JSON 调用：优先 response_format=json_object；失败则提取首个配平 {} 再解析；
    仍失败抛 LLMFormatError（调用方负责重试）。"""
    text = _call(task, system, user, heavy, temperature, json_mode=True)
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
        raise LLMFormatError(f"JSON 顶层不是对象: task={task}")
    except json.JSONDecodeError:
        fragment = _extract_balanced_json(text)
        if fragment is not None:
            try:
                result = json.loads(fragment)
                if isinstance(result, dict):
                    return result
            except json.JSONDecodeError:
                pass
        preview = re.sub(r"\s+", " ", text)[:200]
        raise LLMFormatError(f"无法解析 LLM 返回为 JSON: task={task}, 内容预览: {preview}")
