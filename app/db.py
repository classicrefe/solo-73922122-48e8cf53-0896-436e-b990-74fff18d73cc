"""SQLite 连接与建表。

只保存逐日摘要，不保存原始观测、更不按毫米逐档展开：
- daily_summary          : 每站每天一行；有测/未测秒数、日内最低/最高水位，
                           并以 profile 列保存日内线性片段（精确有理数 JSON）
- daily_level_tail       : 旧版摘要（逐毫米尾分布），仅保留只读兼容
"""
import sqlite3

from flask import current_app, g

# 表结构版本：v2 起 daily_summary 增加 profile 列（日内线性片段 JSON）。
SCHEMA_VERSION = 2


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
            isolation_level=None,  # autocommit，事务在业务层显式控制
            timeout=15,            # 多 worker 时等待写锁，避免瞬时 database is locked
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_e=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_summary (
    station          TEXT    NOT NULL,
    day              TEXT    NOT NULL,          -- UTC 日期 YYYY-MM-DD
    measured_seconds INTEGER NOT NULL,          -- 有测（已插值覆盖）秒数
    unmeasured_seconds INTEGER NOT NULL,        -- 未测秒数，86400 - measured
    min_level        TEXT    NOT NULL,          -- 日内插值最低水位，精确有理数文本
    max_level        TEXT    NOT NULL,          -- 日内插值最高水位，精确有理数文本
    profile          TEXT,                      -- 日内线性片段 JSON（版本 2）
    PRIMARY KEY (station, day)
);

CREATE TABLE IF NOT EXISTS daily_level_tail (
    station   TEXT NOT NULL,
    day       TEXT NOT NULL,
    level_mm  INTEGER NOT NULL,                 -- 整数毫米警戒线 h
    seconds   REAL NOT NULL,                    -- 当日水位 >= h 的累计秒数
    PRIMARY KEY (station, day, level_mm),
    FOREIGN KEY (station, day) REFERENCES daily_summary(station, day)
);

CREATE INDEX IF NOT EXISTS idx_tail_lookup
    ON daily_level_tail (station, day, level_mm);
"""

# 旧库升级：为早期用 REAL 保存水位的 daily_summary 增列并迁移。
_SUMMARY_ADD_PROFILE = "ALTER TABLE daily_summary ADD COLUMN profile TEXT"


def _column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _migrate_summary(conn):
    columns = _column_names(conn, "daily_summary")
    if "profile" not in columns:
        try:
            conn.execute(_SUMMARY_ADD_PROFILE)
        except sqlite3.OperationalError as exc:
            # 多 worker 同时首次建表时另一个进程可能已加上该列
            if "duplicate column" not in str(exc).lower():
                raise
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def init_db(conn):
    conn.executescript(SCHEMA)
    _migrate_summary(conn)


def init_app(app):
    app.teardown_appcontext(close_db)

    @app.cli.command("init-db")
    def init_db_command():
        init_db(get_db())
        print("数据库已初始化。")
