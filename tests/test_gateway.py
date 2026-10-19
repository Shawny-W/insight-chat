"""SQL 安全网关测试（不依赖 LLM）。依赖已初始化的 meta.db（行策略需要快照读维度定义）。"""

import pytest

from chatbi.query.gateway import (SqlForbiddenError, check_and_rewrite,
                                  check_and_rewrite_detail)

GOOD_SQL = ("SELECT stores.region_name AS \"大区\", SUM(order_items.quantity) AS \"销量\" "
            "FROM order_items JOIN orders ON order_items.order_id = orders.order_id "
            "JOIN stores ON orders.store_id = stores.store_id "
            "WHERE orders.status = 'completed' GROUP BY stores.region_name LIMIT 100")

ROW_POLICY_HD = [{"policy_type": "row", "model_id": "mdl_stores",
                  "dimension_id": "dim_region", "allowed_values": ["HD"]}]


@pytest.mark.parametrize("bad_sql", [
    "DROP TABLE orders",
    "DELETE FROM orders WHERE 1=1",
    "UPDATE orders SET status='x'",
    "ATTACH DATABASE 'x.db' AS x",
    "PRAGMA table_info(orders)",
    "INSERT INTO orders VALUES (1,2,3,4,5,6)",
])
def test_write_and_command_rejected(bad_sql):
    with pytest.raises(SqlForbiddenError):
        check_and_rewrite(bad_sql, [])


def test_meta_table_rejected():
    with pytest.raises(SqlForbiddenError) as ei:
        check_and_rewrite("SELECT * FROM app_user", [])
    assert ei.value.code == "SQL_FORBIDDEN"


def test_parse_failure_rejected():
    with pytest.raises(SqlForbiddenError) as ei:
        check_and_rewrite("SELEC 这是中文不是 SQL", [])
    assert ei.value.code == "SQL_PARSE"


def test_normal_select_passes():
    out = check_and_rewrite(GOOD_SQL, [])
    assert "orders" in out


def test_limit_enforced():
    out = check_and_rewrite(
        "SELECT order_id FROM orders WHERE status = 'completed'", [])
    assert "LIMIT 10000" in out                              # 无 LIMIT → 兜底
    out2 = check_and_rewrite(
        "SELECT order_id FROM orders WHERE status = 'completed' LIMIT 999999", [])
    assert "LIMIT 10000" in out2                             # 超限 → 改小
    out3 = check_and_rewrite(
        "SELECT order_id FROM orders WHERE status = 'completed' LIMIT 5", [])
    assert "LIMIT 5" in out3                                 # 合法保持


def test_row_policy_injected_on_referenced_table():
    out, notes = check_and_rewrite_detail(GOOD_SQL, ROW_POLICY_HD)
    assert "region_name IN ('HD')" in out
    assert any("数据范围" in n for n in notes)


def test_row_policy_nested_when_table_absent():
    sql = "SELECT COUNT(DISTINCT orders.order_id) AS c FROM orders WHERE orders.status = 'completed'"
    out, _ = check_and_rewrite_detail(sql, ROW_POLICY_HD)
    assert "orders.store_id IN" in out                       # 子查询嵌套注入
    assert "region_name IN ('HD')" in out


def test_row_policy_empty_values_fail_closed():
    policy = [{"policy_type": "row", "model_id": "mdl_stores",
               "dimension_id": "dim_region", "allowed_values": []}]
    out, _ = check_and_rewrite_detail(GOOD_SQL, policy)
    assert "1 = 0" in out


def test_row_policy_multi_values_union():
    policy = [
        {"policy_type": "row", "model_id": "mdl_stores",
         "dimension_id": "dim_region", "allowed_values": ["HD"]},
        {"policy_type": "row", "model_id": "mdl_stores",
         "dimension_id": "dim_region", "allowed_values": ["HB"]},
    ]
    out, _ = check_and_rewrite_detail(GOOD_SQL, policy)
    assert "'HD'" in out and "'HB'" in out                   # 并集


def test_column_hide_forbidden():
    sql = "SELECT customers.customer_id, customers.level FROM customers"
    policy = [{"policy_type": "column", "model_id": "mdl_customers",
               "column_name": "level", "column_action": "hide"}]
    with pytest.raises(SqlForbiddenError) as ei:
        check_and_rewrite(sql, policy)
    assert ei.value.code == "COLUMN_FORBIDDEN"


def test_column_mask_rewritten():
    sql = "SELECT customers.customer_id, customers.level FROM customers"
    policy = [{"policy_type": "column", "model_id": "mdl_customers",
               "column_name": "level", "column_action": "mask", "mask_rule": "phone"}]
    out, notes = check_and_rewrite_detail(sql, policy)
    assert "substring" in out.lower() and "****" in out
    assert any("脱敏" in n for n in notes)
