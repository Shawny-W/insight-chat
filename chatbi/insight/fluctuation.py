"""波动识别（契约见 spec 第 6 节）。

detect(metric_id, period_a, period_b, snapshot, policies)
  -> {V_A, V_B, delta, pct, baseline_series[<=12], mu, sigma, is_significant, baseline_limited}
显著判定（PRD 6.6.4 Step1）：|pct|>=10% 或 |pct-mu|>=2*sigma；基线不足 6 期只用 10% 规则。
指标取值复用查询链路：SemQL 构造 → compile → gateway → execute。
"""

import calendar
import re
import statistics
from datetime import date

from chatbi.query.compiler import compile as compile_semql
from chatbi.query.executor import execute
from chatbi.query.gateway import check_and_rewrite
from chatbi.query.semql import SemQL
from chatbi.semantic.store import SemanticSnapshot


def parse_period(p) -> tuple[str, str]:
    """period 归一化为闭区间 (start, end)。支持 "YYYY-MM" / "YYYY-MM-DD" / "start/end"。"""
    if isinstance(p, (tuple, list)) and len(p) == 2:
        return str(p[0]), str(p[1])
    s = str(p).strip()
    m = re.fullmatch(r"(\d{4})-(\d{2})", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return f"{y}-{mo:02d}-01", f"{y}-{mo:02d}-{calendar.monthrange(y, mo)[1]}"
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return s, s
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*/\s*(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1), m.group(2)
    raise ValueError(f"无法解析期间: {p!r}")


def _period_grain(start: str, end: str) -> str:
    """按期间长度判定粒度：≤2 天→day，≤14 天→week，否则 month。"""
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if days <= 2:
        return "day"
    if days <= 14:
        return "week"
    return "month"


def _run_scalar(snapshot: SemanticSnapshot, policies: list[dict] | None,
                metric_id: str, start: str, end: str,
                filters: list[dict] | None = None) -> float:
    """单期单值查询（复用查询链路）。"""
    semql = SemQL(dataset=next(iter(snapshot.datasets)), metrics=[metric_id],
                  dimensions=[], filters=filters or [],
                  time={"field": None, "range": f"{start}/{end}", "grain": None},
                  limit=1, query_mode="aggregate")
    compiled = compile_semql(semql, snapshot)
    sql = check_and_rewrite(compiled.sql, policies or [])
    rs = execute(sql)
    v = rs.rows[0][0] if rs.rows and rs.rows[0] else None
    return float(v) if v is not None else 0.0


def _run_monthly_series(snapshot: SemanticSnapshot, policies: list[dict] | None,
                        metric_id: str, start: str, end: str,
                        filters: list[dict] | None = None) -> list[dict]:
    """按月分组查询指标序列，返回 [{period, value}]（按月排序）。"""
    semql = SemQL(dataset=next(iter(snapshot.datasets)), metrics=[metric_id],
                  dimensions=[], filters=filters or [],
                  time={"field": None, "range": f"{start}/{end}", "grain": "month"},
                  limit=1000, query_mode="aggregate")
    compiled = compile_semql(semql, snapshot)
    sql = check_and_rewrite(compiled.sql, policies or [])
    rs = execute(sql)
    return [{"period": r[0], "value": float(r[1] or 0)} for r in rs.rows]


def _shift_month(d: date, months: int) -> date:
    total = d.year * 12 + (d.month - 1) + months
    y, m = total // 12, total % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def detect(metric_id: str, period_a, period_b, snapshot: SemanticSnapshot,
           policies: list[dict] | None = None,
           base_filters: list[dict] | None = None) -> dict:
    """波动识别主入口（契约签名固定）。"""
    a_start, a_end = parse_period(period_a)
    b_start, b_end = parse_period(period_b)
    grain = _period_grain(a_start, a_end)

    v_a = _run_scalar(snapshot, policies, metric_id, a_start, a_end, base_filters)
    v_b = _run_scalar(snapshot, policies, metric_id, b_start, b_end, base_filters)
    delta = v_a - v_b
    pct = (delta / v_b * 100) if v_b else None

    # 基线：period_a 向前 ≤12 个同粒度周期（演示数据按月；周/日退化为单值序列亦可）
    series: list[dict] = []
    if grain == "month":
        win_start = _shift_month(date.fromisoformat(a_start), -13).isoformat()
        monthly = _run_monthly_series(snapshot, policies, metric_id,
                                      win_start, a_end, base_filters)
        a_bucket = a_start[:7]
        series = [p for p in monthly if p["period"] < a_bucket][-12:]
    else:
        series = []

    # 环比序列的均值/标准差
    if len(series) >= 2:
        moms = [(series[i]["value"] - series[i - 1]["value"]) / series[i - 1]["value"] * 100
                for i in range(1, len(series)) if series[i - 1]["value"]]
        mu = statistics.mean(moms) if moms else 0.0
        sigma = statistics.pstdev(moms) if len(moms) >= 2 else 0.0
    else:
        mu, sigma = 0.0, 0.0

    baseline_limited = len(series) < 6
    if pct is None:
        is_significant = False
    elif baseline_limited:
        is_significant = abs(pct) >= 10.0
    else:
        is_significant = abs(pct) >= 10.0 or abs(pct - mu) >= 2 * sigma

    return {
        "metric_id": metric_id,
        "period_a": f"{a_start}/{a_end}",
        "period_b": f"{b_start}/{b_end}",
        "grain": grain,
        "V_A": v_a,
        "V_B": v_b,
        "delta": delta,
        "pct": round(pct, 2) if pct is not None else None,
        "baseline_series": series,
        "mu": round(mu, 2),
        "sigma": round(sigma, 2),
        "baseline_limited": baseline_limited,
        "is_significant": is_significant,
    }
