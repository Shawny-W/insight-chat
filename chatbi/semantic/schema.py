"""元数据库 schema（SQLite 版），按 PRD 7.2 适配：

- JSONB        → TEXT（JSON 字符串）
- TIMESTAMPTZ  → TEXT（ISO8601，默认当前时间）
- BIGSERIAL    → INTEGER PRIMARY KEY AUTOINCREMENT
- BOOLEAN      → INTEGER（0/1）
- pgvector 扩展与 vector 列 → 去除（本地版不走向量路）
- 部分唯一索引 SQLite 原生支持（保证有且仅有一个 published 版本）
"""

# 所有建表语句按依赖顺序排列
SCHEMA_STATEMENTS: list[str] = [
    # ========== 1. 用户与权限 ==========
    """
    CREATE TABLE IF NOT EXISTS app_user (
        id            TEXT PRIMARY KEY,                -- usr_xxx
        username      TEXT UNIQUE NOT NULL,
        display_name  TEXT NOT NULL,
        password_hash TEXT NOT NULL,                   -- pbkdf2_sha256
        email         TEXT,
        dept          TEXT,
        status        TEXT NOT NULL DEFAULT 'active',  -- active/disabled
        created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS role (
        id          TEXT PRIMARY KEY,                  -- role_xxx
        code        TEXT UNIQUE NOT NULL,              -- admin/semantic_admin/analyst/viewer/custom
        name        TEXT NOT NULL,
        description TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS user_role (
        user_id TEXT NOT NULL REFERENCES app_user(id),
        role_id TEXT NOT NULL REFERENCES role(id),
        PRIMARY KEY (user_id, role_id)
    )
    """,
    # 三类策略统一存一张表, policy_type 区分
    """
    CREATE TABLE IF NOT EXISTS data_policy (
        id             TEXT PRIMARY KEY,               -- pol_xxx
        role_id        TEXT NOT NULL REFERENCES role(id),
        policy_type    TEXT NOT NULL,                  -- dataset/row/column
        dataset_id     TEXT,                           -- policy_type=dataset 时必填
        model_id       TEXT,                           -- row/column 时必填
        dimension_id   TEXT,                           -- row 时必填
        allowed_values TEXT,                           -- row 时必填: JSON数组 ["HD","HB"] (tech_value)
        column_name    TEXT,                           -- column 时必填
        column_action  TEXT,                           -- hide/mask
        mask_rule      TEXT,                           -- phone/name/custom_regex
        status         TEXT NOT NULL DEFAULT 'active', -- active/invalid
        created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_policy_role ON data_policy(role_id, policy_type, status)",
    # ========== 2. 数据源 ==========
    """
    CREATE TABLE IF NOT EXISTS datasource (
        id           TEXT PRIMARY KEY,                 -- ds_xxx
        name         TEXT NOT NULL,
        db_type      TEXT NOT NULL DEFAULT 'sqlite',   -- 本地版业务库为 SQLite
        host         TEXT,
        port         INTEGER,
        database     TEXT NOT NULL,                    -- 本地版存库文件路径
        username     TEXT,
        password_enc TEXT,
        options      TEXT,                             -- JSON
        status       TEXT NOT NULL DEFAULT 'active',
        created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    # ========== 3. 语义层版本 ==========
    """
    CREATE TABLE IF NOT EXISTS semantic_version (
        version        INTEGER PRIMARY KEY,
        status         TEXT NOT NULL,                  -- draft/validating/pending_review/published/archived
        parent_version INTEGER,
        changelog      TEXT,
        submitted_by   TEXT REFERENCES app_user(id),
        approved_by    TEXT REFERENCES app_user(id),
        check_report   TEXT,                           -- JSON
        published_at   TEXT,
        created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    # 有且仅有一个 published（SQLite 支持部分唯一索引）
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_semver_published ON semantic_version(status) WHERE status='published'",
    # ========== 4. Ontology 层 ==========
    """
    CREATE TABLE IF NOT EXISTS ont_term (
        id            TEXT NOT NULL,
        version       INTEGER NOT NULL REFERENCES semantic_version(version),
        name          TEXT NOT NULL,
        term_type     TEXT NOT NULL,                   -- business_term/metric_term
        definition    TEXT NOT NULL,
        scope         TEXT,
        aliases       TEXT NOT NULL DEFAULT '[]',
        unit          TEXT,
        direction     TEXT,                            -- higher_better/lower_better/neutral
        related_terms TEXT DEFAULT '[]',
        owner         TEXT NOT NULL REFERENCES app_user(id),
        status        TEXT NOT NULL DEFAULT 'draft',
        created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY (id, version)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_term_name_version ON ont_term(name, version)",
    """
    CREATE TABLE IF NOT EXISTS ont_entity (
        id          TEXT NOT NULL,
        version     INTEGER NOT NULL REFERENCES semantic_version(version),
        name        TEXT NOT NULL,
        entity_type TEXT NOT NULL,                     -- subject/classifier
        definition  TEXT NOT NULL,
        aliases     TEXT NOT NULL DEFAULT '[]',
        owner       TEXT NOT NULL REFERENCES app_user(id),
        status      TEXT NOT NULL DEFAULT 'draft',
        created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY (id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ont_attribute (
        id           TEXT NOT NULL,
        version      INTEGER NOT NULL REFERENCES semantic_version(version),
        entity_id    TEXT NOT NULL,
        name         TEXT NOT NULL,
        attr_kind    TEXT NOT NULL,                    -- identifier/descriptor/time
        definition   TEXT,
        value_domain TEXT,                             -- JSON: {"enum":[{"v":"A","label":"高价值"}]}
        aliases      TEXT NOT NULL DEFAULT '[]',
        status       TEXT NOT NULL DEFAULT 'draft',
        created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY (id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ont_relation (
        id            TEXT NOT NULL,
        version       INTEGER NOT NULL REFERENCES semantic_version(version),
        relation_type TEXT NOT NULL,                   -- hierarchy/composition/derivation/equivalence/association
        from_id       TEXT NOT NULL,                   -- Term/Entity/Attribute id
        to_ids        TEXT NOT NULL,                   -- JSON: 目标id数组
        formula       TEXT,                            -- derivation 专用
        note          TEXT,
        status        TEXT NOT NULL DEFAULT 'draft',
        created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY (id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ont_business_rule (
        id               TEXT NOT NULL,
        version          INTEGER NOT NULL REFERENCES semantic_version(version),
        target_id        TEXT NOT NULL,
        rule_type        TEXT NOT NULL,                -- enum_semantics/business_rule/unit/scope/data_quality
        severity         TEXT NOT NULL,                -- block/warning/info
        trigger_keywords TEXT NOT NULL DEFAULT '[]',
        description      TEXT NOT NULL,
        evidence_ids     TEXT DEFAULT '[]',
        status           TEXT NOT NULL DEFAULT 'draft',
        created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY (id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ont_evidence (
        id             TEXT NOT NULL,
        version        INTEGER NOT NULL REFERENCES semantic_version(version),
        source_desc    TEXT,
        query_sql      TEXT NOT NULL,
        result_summary TEXT NOT NULL,
        validated_at   TEXT NOT NULL,
        validated_by   TEXT NOT NULL REFERENCES app_user(id),
        created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY (id, version)
    )
    """,
    # ========== 5. MDL 层 ==========
    """
    CREATE TABLE IF NOT EXISTS mdl_model (
        id            TEXT NOT NULL,
        version       INTEGER NOT NULL REFERENCES semantic_version(version),
        name          TEXT NOT NULL,
        datasource_id TEXT NOT NULL REFERENCES datasource(id),
        source_type   TEXT NOT NULL,                   -- table/sql_query
        table_ref     TEXT,
        sql_query     TEXT,
        primary_key   TEXT NOT NULL DEFAULT '[]',
        time_field    TEXT,
        description   TEXT,
        status        TEXT NOT NULL DEFAULT 'draft',
        created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        PRIMARY KEY (id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mdl_column (
        id           TEXT NOT NULL,
        version      INTEGER NOT NULL REFERENCES semantic_version(version),
        model_id     TEXT NOT NULL,
        name         TEXT NOT NULL,                    -- 物理列名
        display_name TEXT NOT NULL,
        data_type    TEXT NOT NULL,
        expression   TEXT,                             -- 计算列表达式, 普通列为NULL
        is_hidden    INTEGER NOT NULL DEFAULT 0,
        description  TEXT,
        aliases      TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mdl_measure (
        id           TEXT NOT NULL,
        version      INTEGER NOT NULL REFERENCES semantic_version(version),
        model_id     TEXT NOT NULL,
        name         TEXT NOT NULL,
        display_name TEXT NOT NULL,
        agg          TEXT NOT NULL,                    -- SUM/COUNT/AVG/MIN/MAX/COUNT_DISTINCT
        expression   TEXT NOT NULL,
        filter_sql   TEXT,
        format       TEXT,                             -- JSON: {"type":"currency","unit":"元","decimals":2}
        aliases      TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mdl_dimension (
        id             TEXT NOT NULL,
        version        INTEGER NOT NULL REFERENCES semantic_version(version),
        model_id       TEXT NOT NULL,
        name           TEXT NOT NULL,
        display_name   TEXT NOT NULL,
        dim_type       TEXT NOT NULL,                  -- category/date/id
        expression     TEXT NOT NULL,
        aliases        TEXT NOT NULL DEFAULT '[]',
        value_maps     TEXT NOT NULL DEFAULT '[]',     -- JSON: [{"tech_value":"HD","display_value":"华东","query_aliases":["华东区"]}]
        hierarchy_path TEXT,
        drill_enabled  INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mdl_relationship (
        id            TEXT NOT NULL,
        version       INTEGER NOT NULL REFERENCES semantic_version(version),
        from_model_id TEXT NOT NULL,
        to_model_id   TEXT NOT NULL,
        join_type     TEXT NOT NULL,                   -- one_to_one/one_to_many/many_to_one/many_to_many
        condition     TEXT NOT NULL,                   -- "orders.store_id = stores.store_id"
        verified      INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mdl_metric (
        id                 TEXT NOT NULL,
        version            INTEGER NOT NULL REFERENCES semantic_version(version),
        bind_term_id       TEXT,                       -- 1:1绑定 ont_term(metric_term)
        metric_kind        TEXT NOT NULL,              -- atomic/derived
        model_id           TEXT,                       -- atomic 必填
        measure_expression TEXT,                       -- atomic 必填
        agg                TEXT,                       -- atomic 必填
        filter_sql         TEXT,
        joins_needed       TEXT DEFAULT '[]',
        formula            TEXT,                       -- derived 必填: "metric_a / metric_b"
        grain              TEXT DEFAULT '[]',
        default_time_field TEXT,
        related_dimensions TEXT NOT NULL DEFAULT '[]', -- 归因候选维度池
        data_format        TEXT,
        status             TEXT NOT NULL DEFAULT 'draft',
        PRIMARY KEY (id, version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mdl_dataset (
        id                 TEXT NOT NULL,
        version            INTEGER NOT NULL REFERENCES semantic_version(version),
        name               TEXT NOT NULL,
        description        TEXT,
        model_ids          TEXT NOT NULL,
        metric_ids         TEXT NOT NULL,
        dimension_ids      TEXT NOT NULL,
        default_time_range TEXT,                       -- last_month/last_7d/...
        status             TEXT NOT NULL DEFAULT 'draft',
        PRIMARY KEY (id, version)
    )
    """,
    # Ontology ↔ MDL 绑定
    """
    CREATE TABLE IF NOT EXISTS ont_mdl_mapping (
        id           TEXT NOT NULL,
        version      INTEGER NOT NULL REFERENCES semantic_version(version),
        mapping_type TEXT NOT NULL,  -- term_metric/term_rule_sql/attr_dimension/entity_model/term_dataset_scope
        ont_id       TEXT NOT NULL,
        mdl_id       TEXT NOT NULL,  -- term_rule_sql时存SQL谓词片段
        extra        TEXT,
        PRIMARY KEY (id, version)
    )
    """,
    # ========== 6. 索引层 ==========
    # 手工补充词条表（自动词条由发布流水线从语义对象生成, 不落此表）
    """
    CREATE TABLE IF NOT EXISTS dict_entry (
        id         TEXT PRIMARY KEY,
        word       TEXT NOT NULL,
        nature     TEXT NOT NULL,      -- _term_xxx / _dimval_dim_region:HD / _custom
        freq       INTEGER NOT NULL DEFAULT 10000,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        UNIQUE (word, nature)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS query_exemplar (
        id            TEXT PRIMARY KEY,
        dataset_id    TEXT NOT NULL,
        nl_question   TEXT NOT NULL,
        semql_json    TEXT NOT NULL,
        source        TEXT NOT NULL,   -- seed/user_feedback/admin
        quality_score INTEGER NOT NULL DEFAULT 0,
        hit_count     INTEGER NOT NULL DEFAULT 0,
        is_active     INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    # ========== 7. 对话与查询历史 ==========
    """
    CREATE TABLE IF NOT EXISTS chat_session (
        id         TEXT PRIMARY KEY,   -- sess_xxx
        user_id    TEXT NOT NULL REFERENCES app_user(id),
        title      TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_session_user ON chat_session(user_id, updated_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS chat_message (
        id           TEXT PRIMARY KEY, -- msg_xxx
        session_id   TEXT NOT NULL REFERENCES chat_session(id),
        role         TEXT NOT NULL,    -- user/assistant
        content      TEXT NOT NULL,
        intent       TEXT,
        clarify_info TEXT,
        created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_msg_session ON chat_message(session_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS query_log (
        id                 TEXT PRIMARY KEY,   -- qlog_xxx
        session_id         TEXT NOT NULL,
        user_id            TEXT NOT NULL,
        question           TEXT NOT NULL,
        rewritten_question TEXT,
        intent             TEXT,
        matched_elements   TEXT,
        semql_json         TEXT,
        final_sql          TEXT,
        applied_rules      TEXT,
        assumptions        TEXT,
        semantic_version   INTEGER,
        policy_hash        TEXT,
        retry_count        INTEGER NOT NULL DEFAULT 0,
        degraded           TEXT,               -- vector/narrative/intent 逗号分隔
        status             TEXT NOT NULL,      -- success/failed/timeout/forbidden
        error_code         TEXT,
        elapsed_ms         INTEGER,
        created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_qlog_user ON query_log(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_qlog_status ON query_log(status, error_code)",
    """
    CREATE TABLE IF NOT EXISTS answer (
        id              TEXT PRIMARY KEY,    -- ans_xxx
        query_log_id    TEXT NOT NULL REFERENCES query_log(id),
        chart_type      TEXT,
        chart_meta      TEXT,
        columns_meta    TEXT,
        result_snapshot TEXT,                -- JSON: 前200行
        row_count       INTEGER,
        summary_text    TEXT,
        created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS insight_report (
        id               TEXT PRIMARY KEY,     -- attr_xxx (task_id即report_id)
        user_id          TEXT NOT NULL,
        session_id       TEXT,
        metric_id        TEXT NOT NULL,
        period_a         TEXT NOT NULL,
        period_b         TEXT NOT NULL,
        status           TEXT NOT NULL,        -- running/completed/partial/failed
        report_json      TEXT,
        narrative        TEXT,
        semantic_version INTEGER,
        elapsed_ms       INTEGER,
        created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    # ========== 8. 反馈与事件 ==========
    """
    CREATE TABLE IF NOT EXISTS feedback (
        id         TEXT PRIMARY KEY,
        answer_id  TEXT,                       -- 与 insight_id 二选一
        insight_id TEXT,
        user_id    TEXT NOT NULL,
        vote       TEXT NOT NULL,              -- up/down
        reason_tag TEXT,                       -- wrong_data/wrong_intent/bad_chart/untrusted_insight/other
        comment    TEXT,
        status     TEXT NOT NULL DEFAULT 'pending',  -- pending/processing/resolved/dismissed
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_feedback_pending ON feedback(status, created_at DESC)",
    """
    CREATE TABLE IF NOT EXISTS feedback_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        feedback_id TEXT NOT NULL REFERENCES feedback(id),
        old_vote    TEXT, new_vote TEXT,
        old_tag     TEXT, new_tag TEXT,
        changed_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS business_event (
        id                TEXT PRIMARY KEY,
        event_name        TEXT NOT NULL,
        event_type        TEXT NOT NULL,     -- campaign/price_change/store_change/system_issue/holiday/custom
        start_date        TEXT NOT NULL,
        end_date          TEXT NOT NULL,
        related_dimension TEXT,              -- NULL=全局事件
        related_value     TEXT,              -- tech_value
        impact_note       TEXT,
        created_by        TEXT NOT NULL,
        created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_event_date ON business_event(start_date, end_date)",
    """
    CREATE TABLE IF NOT EXISTS audit_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    TEXT NOT NULL,
        action     TEXT NOT NULL,            -- login/publish_version/update_policy/query_forbidden/...
        target     TEXT,
        detail     TEXT,
        ip         TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_log(created_at DESC)",
    # ========== 9. LLM 调用日志（spec 第2节） ==========
    """
    CREATE TABLE IF NOT EXISTS llm_call_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        task       TEXT NOT NULL,
        model      TEXT NOT NULL,
        prompt     TEXT,
        response   TEXT,
        latency_ms INTEGER,
        error      TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_llm_log_task ON llm_call_log(task, created_at DESC)",
    # ========== 10. 用户记忆 ==========
    # 短期会话上下文：只存可 JSON 序列化状态；pending 澄清仍保留在进程内
    """
    CREATE TABLE IF NOT EXISTS chat_context (
        session_id TEXT PRIMARY KEY REFERENCES chat_session(id),
        user_id    TEXT NOT NULL REFERENCES app_user(id),
        state_json TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_chat_context_user ON chat_context(user_id, updated_at DESC)",
    # 长期记忆：同一逻辑 key 的当前版本唯一；被取代/删除的版本永久保留用于审计
    """
    CREATE TABLE IF NOT EXISTS user_memory (
        id                 TEXT PRIMARY KEY,
        user_id            TEXT NOT NULL REFERENCES app_user(id),
        memory_type        TEXT NOT NULL,       -- semantic/episodic
        category           TEXT NOT NULL,       -- chart_preference/term_mapping/query_exemplar
        memory_key         TEXT NOT NULL,
        value_json         TEXT NOT NULL,
        confidence         REAL NOT NULL DEFAULT 1.0,
        source             TEXT NOT NULL,       -- explicit/chart_switch/clarify/feedback
        status             TEXT NOT NULL,       -- candidate/active/superseded/deleted
        valid_from         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        valid_to           TEXT,
        last_confirmed_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        last_accessed_at   TEXT,
        access_count       INTEGER NOT NULL DEFAULT 0,
        created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_user_memory_current
    ON user_memory(user_id, category, memory_key)
    WHERE status IN ('active', 'candidate')
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_memory_lookup ON user_memory(user_id, category, status)",
    """
    CREATE TABLE IF NOT EXISTS user_memory_event (
        id         TEXT PRIMARY KEY,
        memory_id  TEXT NOT NULL REFERENCES user_memory(id),
        user_id    TEXT NOT NULL REFERENCES app_user(id),
        event_type TEXT NOT NULL,             -- created/confirmed/applied/superseded/deleted/promoted
        session_id TEXT,
        detail_json TEXT,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_user_memory_event ON user_memory_event(memory_id, created_at)",
]


def init_schema(conn) -> None:
    """在 meta 库上建立全部元数据表（幂等，可重复执行）。"""
    for stmt in SCHEMA_STATEMENTS:
        conn.execute(stmt)
    conn.commit()
