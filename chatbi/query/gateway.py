"""SQL 安全网关（契约见 spec 第 6 节：check_and_rewrite(sql, policies) -> sql）。

顺序固定：sqlglot AST 白名单（仅 SELECT）→ 权限改写（行谓词注入/列 hide 拒绝/mask 改写）→
LIMIT 兜底 10000。命中禁止项抛 SqlForbiddenError，不重试。
"""

import sqlglot
from sqlglot import exp

from chatbi.semantic.store import SemanticSnapshot, load_snapshot

# 业务库允许的物理表白名单（meta 库表/系统表一律拒绝）
BIZ_TABLES = {"orders", "order_items", "stores", "customers"}

# 禁止的 SQL 节点（写操作/DDL/命令）
_FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Alter, exp.Create,
    exp.TruncateTable, exp.Grant, exp.Set, exp.Merge, exp.Pragma,
    exp.Attach, exp.Detach, exp.Command,
)

# 禁止的函数（文件/网络/信息探测）
_FORBIDDEN_FUNCS = {"load_extension", "readfile", "writefile", "file", "url"}


class SqlForbiddenError(Exception):
    """命中禁止项；code ∈ SQL_FORBIDDEN / SQL_PARSE / COLUMN_FORBIDDEN。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def _parse(sql: str) -> exp.Expression:
    try:
        ast = sqlglot.parse_one(sql, dialect="sqlite")
    except Exception as exc:
        raise SqlForbiddenError("SQL_PARSE", f"SQL 解析失败: {exc}") from exc
    if ast is None:
        raise SqlForbiddenError("SQL_PARSE", "SQL 为空或无法解析")
    return ast


def _check_whitelist(ast: exp.Expression) -> None:
    """仅允许单条 SELECT（含 WITH…SELECT）；禁节点/禁函数/禁表白名单。"""
    root = ast
    if isinstance(root, exp.With):
        inner = root.this
        if not isinstance(inner, exp.Select):
            raise SqlForbiddenError("SQL_FORBIDDEN", "仅允许 SELECT 查询")
    elif not isinstance(root, exp.Select):
        raise SqlForbiddenError("SQL_FORBIDDEN",
                                f"仅允许 SELECT 查询，收到 {type(root).__name__}")

    for node in ast.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SqlForbiddenError("SQL_FORBIDDEN",
                                    f"命中禁止的 SQL 节点: {type(node).__name__}")
        if isinstance(node, exp.Func):
            name = (node.sql_name() or "").lower()
            if name in _FORBIDDEN_FUNCS:
                raise SqlForbiddenError("SQL_FORBIDDEN", f"命中禁止的函数: {name}")

    cte_names = {c.alias for c in ast.find_all(exp.CTE)}
    for table in ast.find_all(exp.Table):
        name = table.name
        if name in cte_names:
            continue
        if name not in BIZ_TABLES:
            raise SqlForbiddenError(
                "SQL_FORBIDDEN",
                f"访问了不允许的表: {name}（仅允许 {sorted(BIZ_TABLES)}）")


# ---------- 行级策略注入 ----------

def _table_graph(snapshot: SemanticSnapshot) -> dict[str, list[tuple[str, str]]]:
    """table → [(table, condition)]（relationship 无向图，节点用物理表名）。"""
    graph: dict[str, list[tuple[str, str]]] = {}
    for r in snapshot.relationships:
        f = snapshot.models.get(r["from_model_id"])
        t = snapshot.models.get(r["to_model_id"])
        if not f or not t:
            continue
        graph.setdefault(f["table_ref"], []).append((t["table_ref"], r["condition"]))
        graph.setdefault(t["table_ref"], []).append((f["table_ref"], r["condition"]))
    return graph


def _path_between(graph: dict, src: str, dst: str) -> list[tuple[str, str, str]] | None:
    """BFS 找 src→dst 的表路径，返回 [(from_table, to_table, condition)]。"""
    if src == dst:
        return []
    visited = {src}
    queue: list[tuple[str, list]] = [(src, [])]
    while queue:
        cur, path = queue.pop(0)
        for nxt, cond in graph.get(cur, []):
            if nxt in visited:
                continue
            if nxt == dst:
                return path + [(cur, nxt, cond)]
            visited.add(nxt)
            queue.append((nxt, path + [(cur, nxt, cond)]))
    return None


def _join_key(condition: str, table: str) -> str | None:
    """从等值 join 条件中取指定表的列，如 'orders.store_id = stores.store_id' 对 orders → orders.store_id。"""
    parts = [p.strip() for p in condition.split("=")]
    if len(parts) != 2:
        return None
    for p in parts:
        if p.startswith(f"{table}."):
            return p
    return None


def _row_predicate(snapshot: SemanticSnapshot, dim: dict, values: list[str],
                   referenced_tables: set[str]) -> str:
    """生成行过滤谓词；维度表未被直接引用时沿关系链嵌套子查询。"""
    dim_table = snapshot.models[dim["model_id"]]["table_ref"]
    lit = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    base_cond = f"{dim['expression']} IN ({lit})"
    if dim_table in referenced_tables:
        return base_cond

    # 选第一个被引用的业务表作为锚点，沿关系链嵌套
    graph = _table_graph(snapshot)
    anchor = next((t for t in ("orders", "order_items", "customers")
                   if t in referenced_tables), None)
    if anchor is None:
        return "1 = 0"  # 没有可锚定的业务表，fail-closed
    path = _path_between(graph, anchor, dim_table)
    if path is None:
        return "1 = 0"

    # 从受控维度表向外层锚点逐层嵌套：stores→orders→order_items
    inner_sql = None
    for i in range(len(path) - 1, -1, -1):
        from_t, to_t, cond = path[i]
        if inner_sql is None:
            # 最内层：SELECT <受控表key> FROM <受控表> WHERE <expr> IN (...)
            key = _join_key(cond, to_t)
            inner_sql = f"SELECT {key} FROM {to_t} WHERE {base_cond}"
        else:
            # 中间层：SELECT <本边在 to_t 的键> FROM <to_t>
            #        WHERE <下一条边在 to_t 的键> IN (<上一层>)
            select_key = _join_key(cond, to_t)
            link_key = _join_key(path[i + 1][2], to_t)
            inner_sql = f"SELECT {select_key} FROM {to_t} WHERE {link_key} IN ({inner_sql})"
    # 外层锚点谓词
    outer_key = _join_key(path[0][2], path[0][0])
    return f"{outer_key} IN ({inner_sql})"


def _inject_row_policies(ast: exp.Expression, policies: list[dict],
                         snapshot: SemanticSnapshot,
                         notes: list[str]) -> exp.Expression:
    """行策略 → WHERE 谓词注入；同维度多策略取并集；空 allowed_values 注入 1=0（fail-closed）。

    注意：sqlglot 的 where()/limit() 默认 copy=True 返回新节点，必须由调用方接返回值。"""
    by_dim: dict[str, set[str]] = {}
    for p in policies:
        if p.get("policy_type") != "row":
            continue
        dim_id = p.get("dimension_id")
        values = p.get("allowed_values") or []
        by_dim.setdefault(dim_id, set()).update(values)

    referenced = {t.name for t in ast.find_all(exp.Table)}
    for dim_id, values in by_dim.items():
        dim = snapshot.dimensions.get(dim_id)
        if dim is None:
            ast = ast.where("1 = 0", dialect="sqlite")  # 策略引用失效维度 → fail-closed
            notes.append(f"行策略引用的维度 {dim_id} 已失效，已按无数据权限处理")
            continue
        if not values:
            ast = ast.where("1 = 0", dialect="sqlite")
            notes.append(f"行策略维度 {dim['display_name']} 未配置允许值，已按无数据权限处理")
            continue
        pred = _row_predicate(snapshot, dim, sorted(values), referenced)
        ast = ast.where(pred, dialect="sqlite")
        disp = dim["display_name"]
        labels = [vm["display_value"] for vm in dim.get("value_maps", [])
                  if vm["tech_value"] in values]
        notes.append(f"数据范围：{'、'.join(labels) or sorted(values)}（{disp}）")
    return ast


# ---------- 列级策略 ----------

def _apply_column_policies(ast: exp.Expression, policies: list[dict],
                           notes: list[str]) -> exp.Expression:
    for p in policies:
        if p.get("policy_type") != "column":
            continue
        col_name = p.get("column_name")
        action = p.get("column_action")
        if not col_name or not action:
            continue
        hits = [c for c in ast.find_all(exp.Column) if c.name == col_name]
        if not hits:
            continue
        if action == "hide":
            raise SqlForbiddenError(
                "COLUMN_FORBIDDEN", f"您没有查看“{col_name}”的权限")
        if action == "mask":
            rule = p.get("mask_rule") or "phone"
            for col in hits:
                ref = col.sql(dialect="sqlite")
                if rule == "phone":
                    masked = f"(substr({ref},1,3) || '****' || substr({ref},-4))"
                else:
                    masked = f"('**' || substr({ref},-2))"
                col.replace(sqlglot.parse_one(masked, dialect="sqlite"))
            notes.append(f"列 {col_name} 已按 {rule} 规则脱敏")
    return ast


# ---------- LIMIT 兜底 ----------

def _enforce_limit(ast: exp.Expression, max_limit: int = 10000) -> exp.Expression:
    limit = ast.args.get("limit")
    if limit is None:
        return ast.limit(max_limit, dialect="sqlite")
    try:
        current = int(limit.expression.name)
    except (AttributeError, ValueError):
        current = None
    if current is None or current > max_limit:
        ast = ast.copy()
        ast.set("limit", exp.Limit(expression=exp.Literal.number(max_limit)))
    return ast


# ---------- 主入口 ----------

def check_and_rewrite_detail(sql: str, policies: list[dict] | None,
                             snapshot: SemanticSnapshot | None = None) -> tuple[str, list[str]]:
    """返回 (最终SQL, applied 说明列表)。命中禁止项抛 SqlForbiddenError。"""
    policies = policies or []
    notes: list[str] = []

    ast = _parse(sql)
    _check_whitelist(ast)

    row_policies = [p for p in policies if p.get("policy_type") == "row"]
    col_policies = [p for p in policies if p.get("policy_type") == "column"]
    if row_policies or col_policies:
        if snapshot is None:
            snapshot = load_snapshot()
        if row_policies:
            ast = _inject_row_policies(ast, row_policies, snapshot, notes)
        if col_policies:
            ast = _apply_column_policies(ast, col_policies, notes)

    ast = _enforce_limit(ast)
    return ast.sql(dialect="sqlite"), notes


def check_and_rewrite(sql: str, policies: list[dict] | None = None) -> str:
    """spec 契约签名：返回最终 SQL 文本。"""
    return check_and_rewrite_detail(sql, policies)[0]
