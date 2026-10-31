# SQLite → PostgreSQL 迁移

**现状：运行时仍使用 SQLite。** 本次提供的是可验证的**一次性数据迁移导出**，不是运行时适配层。

## 已完成

- `backend/scripts/sqlite_to_postgres.py` 把四个 SQLite 库导出为 PostgreSQL SQL：
  - `scenarios` 模式：情景、知识、剧情线配置、模型预设、角色包
  - `game_state` 模式：世界、权威状态、请求、领域事件、快照
  - `events` 模式：旧聊天事件与记忆晶体
  - `traces` 模式：LLM 调用追踪
- 类型转换：`REAL → DOUBLE PRECISION`、`BLOB → BYTEA`、`INTEGER PRIMARY KEY → BIGINT PRIMARY KEY`。
- 字符串转义、二进制十六进制、事务包裹；表名跨库不会冲突。
- 测试：`backend/tests/scripts/test_sqlite_to_postgres.py` 验证模式、DDL 与数据转义。

## 使用

```bash
backend/.venv/Scripts/python.exe backend/scripts/sqlite_to_postgres.py --out migration.sql
psql "$DATABASE_URL" -f migration.sql
```

导出前停止写入，或在副本上执行，避免拿到半提交的数据。

## 运行时适配（未完成）

要让应用直接跑在 PostgreSQL，仍需：

1. 存储层抽象：把四个 Store 的 `sqlite3` 连接替换为可注入驱动，`?` 占位符改 `%s`。
2. UPSERT/冲突语义：`ON CONFLICT ... DO UPDATE` 与 SQLite 行为逐条对齐。
3. 事务与并发：连接池、隔离级别、CAS 失败重试与当前 `BEGIN` 语义对齐。
4. 迁移器：把现有 `SCHEMA_VERSION` 迁移表转成 PostgreSQL 迁移脚本并纳入启动流程。
5. 测试矩阵：全部 1200+ 用例在 PostgreSQL 下重跑，重点覆盖并发提交、回滚、快照。

完成这些之前，不建议把 `DATABASE_URL` 指向 PostgreSQL 作为生产方案。
