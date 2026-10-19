"""安全工具：pbkdf2 密码哈希/校验 + HMAC 签名的简易 token（演示级）。

token 格式：base64url(JSON{"user_id","exp"}) + "." + base64url(hmac_sha256签名)
"""

import base64
import hashlib
import hmac
import json
import os
import time

from chatbi.config import AUTH_SECRET

_PBKDF2_ITERATIONS = 200_000
TOKEN_TTL_SEC = 8 * 3600  # token 有效期 8 小时


def hash_password(password: str) -> str:
    """pbkdf2_sha256 哈希，格式：pbkdf2_sha256$迭代次数$salt_b64$hash_b64"""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """校验密码与哈希是否匹配。"""
    try:
        scheme, iterations, salt_b64, hash_b64 = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(payload_b64: str) -> str:
    sig = hmac.new(AUTH_SECRET.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256)
    return _b64url_encode(sig.digest())


def make_token(user_id: str, ttl_sec: int = TOKEN_TTL_SEC) -> str:
    """签发 token：payload 含 user_id 与过期时间戳，HMAC-SHA256 签名。"""
    payload = {"user_id": user_id, "exp": int(time.time()) + ttl_sec}
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_token(token: str) -> str | None:
    """校验 token，通过则返回 user_id；签名不符或已过期返回 None。"""
    try:
        payload_b64, sig = token.split(".")
    except ValueError:
        return None
    if not hmac.compare_digest(_sign(payload_b64), sig):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    user_id = payload.get("user_id")
    return user_id if isinstance(user_id, str) and user_id else None
