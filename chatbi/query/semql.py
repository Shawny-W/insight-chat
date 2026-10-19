"""SemQL pydantic 模型（字段契约见 spec 第 6 节 / PRD 6.3.2）+ 对快照的合法性校验。

time.range 接受闭区间字面量 "YYYY-MM-DD/YYYY-MM-DD" 或 12 个受控相对词，
展开为字面量由 compiler 完成（本模块提供 resolve_time_range）。
"""

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

from chatbi.config import TZ_NAME
from chatbi.semantic.store import SemanticSnapshot

GRAINS = ("day", "week", "month")
COMPARE_TYPES = ("mom", "yoy", "period_vs_period")
FILTER_OPS = ("in", "not_in", "eq", "gt", "lt", "ge", "le")

MAX_LIMIT = 10000
MAX_DETAIL_LIMIT = 1000


class TimeSpec(BaseModel):
    field: str | None = None          # 默认用主指标 default_time_field / 主模型 time_field
    range: str                        # "YYYY-MM-DD/YYYY-MM-DD" 或受控词
    grain: Literal["day", "week", "month"] | None = None


class Filter(BaseModel):
    dimension: str | None = None
    metric: str | None = None
    op: Literal["in", "not_in", "eq", "gt", "lt", "ge", "le"]
    values: list = Field(min_length=1)


class OrderBy(BaseModel):
    metric: str | None = None
    dimension: str | None = None
    direction: Literal["asc", "desc"] = "desc"


class Compare(BaseModel):
    type: Literal["mom", "yoy", "period_vs_period"]
    offset: str | None = None
    # period_vs_period 专用：基期闭区间 "YYYY-MM-DD/YYYY-MM-DD"（归因两期对比用）
    period_b: str | None = None


class SemQL(BaseModel):
    semql_version: str = "1.0"
    dataset: str
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    time: TimeSpec | None = None
    filters: list[Filter] = Field(default_factory=list)
    order_by: list[OrderBy] = Field(default_factory=list)
    limit: int | None = 100
    compare: Compare | None = None
    query_mode: Literal["aggregate", "detail"] = "aggregate"
    # 扩展字段：linking 命中的 business_term id 列表，编译器据此内联 rule_sql（{t_start} 代入 time.start）
    business_terms: list[str] = Field(default_factory=list)

    @field_validator("metrics", "dimensions", mode="before")
    @classmethod
    def _none_to_empty(cls, v):
        return v if v is not None else []

    # ---------- 对快照的合法性校验 ----------

    def validate_against(self, snapshot: SemanticSnapshot) -> list[str]:
        """返回错误列表（空=合法）。错误码前缀供 generator 重试链判断。"""
        errors: list[str] = []

        ds = snapshot.datasets.get(self.dataset)
        if ds is None:
            errors.append(f"SEMQL_INVALID_REF: 数据集 {self.dataset} 不存在")
            return errors  # 后续校验都依赖数据集，直接返回

        ds_metrics = set(ds.get("metric_ids", []))
        ds_dims = set(ds.get("dimension_ids", []))
        col_ids = {c["id"] for c in snapshot.columns}

        for mid in self.metrics:
            if mid not in snapshot.metrics:
                errors.append(f"SEMQL_INVALID_REF: 指标 {mid} 不存在")
            elif mid not in ds_metrics:
                errors.append(f"SEMQL_INVALID_REF: 指标 {mid} 不在数据集 {self.dataset} 内")

        for did in self.dimensions:
            if did in snapshot.dimensions:
                if did not in ds_dims:
                    errors.append(f"SEMQL_INVALID_REF: 维度 {did} 不在数据集 {self.dataset} 内")
            elif did not in col_ids:
                errors.append(f"SEMQL_INVALID_REF: 维度/列 {did} 不存在")

        if self.query_mode == "aggregate" and not self.metrics:
            errors.append("SEMQL_INVALID_REF: aggregate 模式至少需要一个指标")
        if self.query_mode == "detail" and not self.dimensions:
            errors.append("SEMQL_INVALID_REF: detail 模式至少需要一个输出列")

        for f in self.filters:
            if f.dimension:
                dim = snapshot.dimensions.get(f.dimension)
                if dim is None:
                    errors.append(f"SEMQL_INVALID_REF: 过滤维度 {f.dimension} 不存在")
                    continue
                for v in f.values:
                    if translate_dim_value(dim, v) is None:
                        errors.append(
                            f"SEMQL_INVALID_VALUE: 维度 {f.dimension} 的值 {v!r} 无法翻译为 tech_value")
            elif f.metric:
                if f.metric not in snapshot.metrics:
                    errors.append(f"SEMQL_INVALID_REF: 过滤指标 {f.metric} 不存在")
            else:
                errors.append("SEMQL_INVALID_REF: filter 必须含 dimension 或 metric")

        for tid in self.business_terms:
            t = snapshot.terms.get(tid)
            if t is None:
                errors.append(f"SEMQL_INVALID_REF: 业务术语 {tid} 不存在")
            elif t.get("term_type") != "business_term":
                errors.append(f"SEMQL_INVALID_REF: {tid} 不是 business_term")
            elif not any(m["mapping_type"] == "term_rule_sql" and m["ont_id"] == tid
                         for m in snapshot.mappings):
                errors.append(f"SEMQL_INVALID_REF: 业务术语 {tid} 缺少 rule_sql 映射")

        if self.limit is not None:
            if self.limit > MAX_LIMIT:
                errors.append(f"SEMQL_INVALID_LIMIT: limit {self.limit} 超过上限 {MAX_LIMIT}")
            if self.query_mode == "detail" and self.limit > MAX_DETAIL_LIMIT:
                errors.append(
                    f"SEMQL_INVALID_LIMIT: detail 模式 limit {self.limit} 超过上限 {MAX_DETAIL_LIMIT}")

        if self.time is not None:
            try:
                resolve_time_range(self.time.range, today())
            except ValueError as exc:
                errors.append(f"SEMQL_INVALID_TIME: {exc}")

        return errors


# ---------- 时间范围解析（受控词 + 字面量 → 闭区间字面量） ----------

def today() -> date:
    return datetime.now(ZoneInfo(TZ_NAME)).date()


def _month_last_day(y: int, m: int) -> int:
    return calendar.monthrange(y, m)[1]


def resolve_time_range(range_expr: str, ref: date) -> tuple[str, str]:
    """受控词或 "start/end" 字面量 → (start, end)。无法解析抛 ValueError。"""
    s = (range_expr or "").strip()
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})\s*/\s*(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1), m.group(2)

    if s == "today":
        return ref.isoformat(), ref.isoformat()
    if s == "yesterday":
        d = ref - timedelta(days=1)
        return d.isoformat(), d.isoformat()
    if s in ("this_week", "last_week"):
        monday = ref - timedelta(days=ref.weekday())
        if s == "last_week":
            monday -= timedelta(days=7)
        sunday = monday + timedelta(days=6)
        return monday.isoformat(), sunday.isoformat()
    if s in ("this_month", "last_month"):
        y, mo = ref.year, ref.month
        if s == "last_month":
            mo -= 1
            if mo == 0:
                y, mo = y - 1, 12
        return f"{y}-{mo:02d}-01", f"{y}-{mo:02d}-{_month_last_day(y, mo)}"
    if s in ("this_quarter", "last_quarter"):
        q_first_month = (ref.month - 1) // 3 * 3 + 1
        if s == "last_quarter":
            q_first_month -= 3
            if q_first_month <= 0:
                q_first_month += 12
                y = ref.year - 1
            else:
                y = ref.year
        else:
            y = ref.year
        q_last_month = q_first_month + 2
        return (f"{y}-{q_first_month:02d}-01",
                f"{y}-{q_last_month:02d}-{_month_last_day(y, q_last_month)}")
    if s == "this_year":
        return f"{ref.year}-01-01", f"{ref.year}-12-31"
    if s == "last_year":
        return f"{ref.year - 1}-01-01", f"{ref.year - 1}-12-31"
    if s == "last_7d":
        return (ref - timedelta(days=6)).isoformat(), ref.isoformat()
    if s == "last_30d":
        return (ref - timedelta(days=29)).isoformat(), ref.isoformat()
    raise ValueError(f"无法解析时间范围: {range_expr!r}（支持 12 个受控词或 YYYY-MM-DD/YYYY-MM-DD）")


# ---------- 维度值翻译 ----------

def translate_dim_value(dim: dict, value) -> str | None:
    """把展示名/查询别名/技术值翻译为 tech_value；无法翻译返回 None。
    无 value_maps 的维度（省份/城市/门店）原样通过（视为技术值）。"""
    maps = dim.get("value_maps") or []
    if not maps:
        return str(value)
    sv = str(value)
    for vm in maps:
        if sv == str(vm.get("tech_value")):
            return str(vm["tech_value"])
    for vm in maps:
        if sv == str(vm.get("display_value")) or sv in [str(a) for a in vm.get("query_aliases", [])]:
            return str(vm["tech_value"])
    return None
