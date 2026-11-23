"""新需求回归：全范围 64 位整数、跨午夜同一直线、旧摘要兼容、舍入口径。"""
import sqlite3
from fractions import Fraction

import pytest

from app.exact import (
    INT64_MAX, INT64_MIN, decode_profile, round_seconds,
    threshold_seconds_from_pieces,
)
from app.importer import ImportError_, build_daily_summaries, import_csv, parse_csv
from app.query import run_query

MAX = INT64_MAX
MIN = INT64_MIN


def csv_bytes(rows, header=True):
    lines = ["timestamp_utc,level_mm"] if header else []
    lines.extend(f"{ts},{lvl}" for ts, lvl in rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def approx(value):
    return pytest.approx(float(value), abs=1e-6)


# ---------------------------------------------------------------- 大整数读数
def test_two_huge_levels_one_mm_apart_stay_a_slope(db):
    """两笔大整数读数只差 1mm：不得因浮点精度被当成平水。"""
    rows = parse_csv(csv_bytes([
        ("2026-03-01T10:00:00Z", MAX - 3),
        ("2026-03-01T11:00:00Z", MAX - 1),
    ]))
    summaries, _ = build_daily_summaries(rows)
    day = summaries["2026-03-01"]
    pieces = decode_profile(day["profile"])
    assert pieces[0][1] == MAX - 3 and pieces[0][2] == MAX - 1
    # 斜坡中点恰好达到 MAX-2：其后 1800s 达标
    assert threshold_seconds_from_pieces(pieces, MAX - 2) == Fraction(1800)
    assert threshold_seconds_from_pieces(pieces, MAX - 1) == 0
    # 低端 3600s 全达标
    assert threshold_seconds_from_pieces(pieces, MAX - 3) == 3600

    import_csv(db, "BIG", csv_bytes([
        ("2026-03-01T10:00:00Z", MAX - 3),
        ("2026-03-01T11:00:00Z", MAX - 1),
    ]))
    r = run_query(db, "BIG", "2026-03-01", "2026-03-01", str(MAX - 2))
    assert approx(r["above_seconds"]) == 1800
    assert r["threshold_mm"] == MAX - 2


def test_full_int64_range_readings_and_thresholds(db):
    """有符号 64 位整数毫米读数的全范围都能导入并核算。"""
    for idx, (lo, hi) in enumerate([
        (MIN, MIN + 2),
        (MIN + 1, MAX),
        (0, MAX),
    ]):
        station = f"R{idx}"
        payload = csv_bytes([
            ("2026-04-01T00:00:00Z", lo),
            ("2026-04-01T01:00:00Z", hi),
        ])
        import_csv(db, station, payload)
        for h in (MIN, MIN + 1, 0, MAX - 1, MAX):
            r = run_query(db, station, "2026-04-01", "2026-04-01", str(h))
            assert 0 <= r["above_seconds"] <= 3600

    # 边界读数确实按直线分摊
    r = run_query(db, "R0", "2026-04-01", "2026-04-01", str(MIN + 1))
    assert approx(r["above_seconds"]) == 1800
    # 超出 int64 的读数在导入时被明确拒绝，且不留下任何数据
    with pytest.raises(ImportError_, match="64 位"):
        import_csv(db, "TOOBIG", csv_bytes([
            ("2026-04-02T00:00:00Z", MAX + 1),
            ("2026-04-02T01:00:00Z", 0),
        ]))
    assert db.execute(
        "SELECT COUNT(*) c FROM daily_summary WHERE station='TOOBIG'"
    ).fetchone()["c"] == 0


def test_threshold_outside_sqlite_integer_range_does_not_error(db):
    """警戒线超出 SQLite 整数范围时按包络判定，不再绑定报错。"""
    import_csv(db, "S", csv_bytes([
        ("2026-03-01T10:00:00Z", 100),
        ("2026-03-01T11:00:00Z", 120),
    ]))
    high = run_query(db, "S", "2026-03-01", "2026-03-01", str(MAX + 1))
    assert high["above_seconds"] == 0
    low = run_query(db, "S", "2026-03-01", "2026-03-01", str(MIN - 1))
    assert low["above_seconds"] == 3600
    # 查询是只读的
    assert db.execute("SELECT COUNT(*) c FROM daily_level_tail").fetchone()["c"] == 0


# ------------------------------------------------------------ 跨午夜同一直线
def test_midnight_segment_keeps_single_straight_line(db):
    """跨午夜段在两天内共用同一条直线（大整数也不漂移）。"""
    rows = parse_csv(csv_bytes([
        ("2026-01-01T23:30:00Z", MIN + 1),
        ("2026-01-02T00:30:00Z", MIN + 3),
        ("2026-01-02T01:30:00Z", MIN + 5),
    ]))
    summaries, _ = build_daily_summaries(rows)
    d1 = summaries["2026-01-01"]
    d2 = summaries["2026-01-02"]
    # 23:30-24:00 是同一斜率的前半段：起点 MIN+1，午夜 MIN+2
    pieces1 = decode_profile(d1["profile"])
    assert pieces1 == [(1800, Fraction(MIN + 1), Fraction(MIN + 2))]
    # 00:00-01:30 延续同一斜率，从 MIN+2 到 MIN+5；同斜率片段合并为一行
    pieces2 = decode_profile(d2["profile"])
    assert pieces2 == [(5400, Fraction(MIN + 2), Fraction(MIN + 5))]

    import_csv(db, "M", csv_bytes([
        ("2026-01-01T23:30:00Z", MIN + 1),
        ("2026-01-02T00:30:00Z", MIN + 3),
        ("2026-01-02T01:30:00Z", MIN + 5),
    ]))
    # MIN+2 恰在午夜：day1 仅在 24:00 端点触及，计 0s；day2 全程 5400s
    r = run_query(db, "M", "2026-01-01", "2026-01-02", str(MIN + 2))
    assert approx(r["above_seconds"]) == 5400
    # MIN+4：day1 0，day2 从 01:00 起 1800s
    r = run_query(db, "M", "2026-01-01", "2026-01-02", str(MIN + 4))
    assert approx(r["above_seconds"]) == 1800


def test_flat_segment_equal_threshold_is_counted_fully(db):
    import_csv(db, "F", csv_bytes([
        ("2026-03-01T08:00:00Z", 90),
        ("2026-03-01T08:30:00Z", 90),
        ("2026-03-01T09:00:00Z", 70),
    ]))
    r = run_query(db, "F", "2026-03-01", "2026-03-01", "90")
    assert approx(r["above_seconds"]) == 1800
    # 大整数平水段同理
    import_csv(db, "F2", csv_bytes([
        ("2026-03-01T08:00:00Z", MAX),
        ("2026-03-01T08:30:00Z", MAX),
    ]))
    r = run_query(db, "F2", "2026-03-01", "2026-03-01", str(MAX))
    assert approx(r["above_seconds"]) == 1800


# -------------------------------------------------------------- 舍入口径
def test_rounding_is_exact_half_up_to_three_decimals():
    # 1/3 秒 -> 0.333；2/3 -> 0.667；恰好 .0005 时四舍五入
    assert round_seconds(Fraction(1, 3)) == Fraction(333, 1000)
    assert round_seconds(Fraction(2, 3)) == Fraction(667, 1000)
    assert round_seconds(Fraction(1, 2000)) == Fraction(1, 1000)   # 0.0005 -> 0.001
    assert round_seconds(Fraction(3, 2000)) == Fraction(2, 1000)   # 0.0015 -> 0.002
    assert round_seconds(Fraction(5, 2000)) == Fraction(3, 1000)   # 0.0025 -> 0.003
    assert round_seconds(Fraction(-1, 2000)) == Fraction(-1, 1000)
    assert round_seconds(Fraction(-3, 2000)) == Fraction(-2, 1000)
    # 与原有浮点显示口径在可精确表示的值上一致
    assert f"{float(round_seconds(Fraction(1, 3))):.3f}" == "0.333"


def test_daily_and_total_rounding_consistent(db):
    """逐日与合计都从同一精确值按 3 位小数舍入，不受浮点累计影响。"""
    # 构造小段：100 -> 130 跨 3 秒，使 tail(101) = 29/30*3 出现非整秒
    payload = csv_bytes([
        ("2026-05-01T00:00:00Z", 100),
        ("2026-05-01T00:00:03Z", 130),
    ])
    import_csv(db, "Q", payload)
    summary = db.execute(
        "SELECT profile FROM daily_summary WHERE station='Q'"
    ).fetchone()
    pieces = decode_profile(summary["profile"])
    exact = threshold_seconds_from_pieces(pieces, 101)
    r = run_query(db, "Q", "2026-05-01", "2026-05-01", "101")
    assert r["above_seconds_exact"] == exact
    # 页面/接口拿到的 float 已由精确值按 3 位小数舍入
    assert r["above_seconds"] == pytest.approx(float(round_seconds(exact)), abs=1e-12)


# ------------------------------------------------------- 旧摘要与批量事务
def _insert_legacy_day(conn, station, day, measured, lo, hi, tails):
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO daily_summary "
        "(station, day, measured_seconds, unmeasured_seconds, min_level, max_level, profile) "
        "VALUES (?, ?, ?, ?, ?, ?, NULL)",
        (station, day, measured, 86400 - measured, lo, hi),
    )
    conn.executemany(
        "INSERT INTO daily_level_tail (station, day, level_mm, seconds) VALUES (?, ?, ?, ?)",
        [(station, day, h, secs) for h, secs in tails.items()],
    )
    conn.execute("COMMIT")


def test_new_and_legacy_summaries_combined_in_one_query(db):
    import_csv(db, "S", csv_bytes([
        ("2026-01-02T10:00:00Z", 100),
        ("2026-01-02T11:00:00Z", 120),
    ]))
    _insert_legacy_day(db, "S", "2026-01-01", 3600, 100.0, 120.0,
                       {100: 3600.0, 110: 1800.0, 111: 1710.0})
    r = run_query(db, "S", "2026-01-01", "2026-01-02", "110")
    # 旧摘要 1800s + 新摘要 1800s
    assert approx(r["above_seconds"]) == 3600
    assert r["per_day"][0]["measured_seconds"] == 3600
    # 旧摘要的 min 边界仍沿用其已存精度
    r = run_query(db, "S", "2026-01-01", "2026-01-02", "100")
    assert approx(r["above_seconds"]) == 7200
    r = run_query(db, "S", "2026-01-01", "2026-01-02", "121")
    assert r["above_seconds"] == 0


def test_legacy_summary_threshold_beyond_sqlite_range(db):
    _insert_legacy_day(db, "L", "2026-01-01", 3600, 100.0, 120.0,
                       {100: 3600.0, 110: 1800.0})
    assert run_query(db, "L", "2026-01-01", "2026-01-01", str(MAX + 1))["above_seconds"] == 0
    assert approx(
        run_query(db, "L", "2026-01-01", "2026-01-01", str(MIN - 1))["above_seconds"]
    ) == 3600


def test_duplicate_day_rejects_whole_batch_and_rolls_back(db):
    import_csv(db, "D", csv_bytes([
        ("2026-06-01T10:00:00Z", 100),
        ("2026-06-01T11:00:00Z", 120),
    ]))
    before = db.execute("SELECT COUNT(*) c FROM daily_summary").fetchone()["c"]
    # 新批次含一天新日期(06-02)与一天重叠日期(06-01)：整批退回
    with pytest.raises(ImportError_, match="重叠"):
        import_csv(db, "D", csv_bytes([
            ("2026-06-01T20:00:00Z", 10),
            ("2026-06-01T21:00:00Z", 20),
            ("2026-06-02T00:00:00Z", 30),
            ("2026-06-02T01:00:00Z", 40),
        ]))
    assert db.execute("SELECT COUNT(*) c FROM daily_summary").fetchone()["c"] == before
    assert db.execute(
        "SELECT COUNT(*) c FROM daily_summary WHERE day = '2026-06-02'"
    ).fetchone()["c"] == 0
    # 原摘要未被覆盖
    old = db.execute(
        "SELECT measured_seconds FROM daily_summary WHERE station='D' AND day='2026-06-01'"
    ).fetchone()
    assert old["measured_seconds"] == 3600


class _FailingConnection:
    """代理真实连接：某一天的 daily_summary 写入抛错，用于验证整批回滚。"""

    def __init__(self, conn, failing_day):
        self._conn = conn
        self._failing_day = failing_day

    def execute(self, sql, params=()):
        if (sql.lstrip().startswith("INSERT INTO daily_summary")
                and params and params[1] == self._failing_day):
            raise sqlite3.OperationalError("模拟磁盘写入失败")
        return self._conn.execute(sql, params)

    def executemany(self, sql, seq):
        return self._conn.executemany(sql, seq)


def test_write_failure_rolls_back_whole_batch(db):
    """写入中途失败时整批回滚，旧摘要不受影响。"""
    import_csv(db, "W", csv_bytes([
        ("2026-07-01T10:00:00Z", 100),
        ("2026-07-01T11:00:00Z", 120),
    ]))
    failing = _FailingConnection(db, "2026-07-03")
    with pytest.raises(sqlite3.OperationalError):
        import_csv(failing, "W", csv_bytes([
            ("2026-07-02T00:00:00Z", 10),
            ("2026-07-02T01:00:00Z", 20),
            ("2026-07-02T02:00:00Z", 30),
            ("2026-07-03T00:00:00Z", 40),
            ("2026-07-03T01:00:00Z", 50),
        ]))
    assert db.execute(
        "SELECT COUNT(*) c FROM daily_summary WHERE day IN ('2026-07-02','2026-07-03')"
    ).fetchone()["c"] == 0
    assert db.execute(
        "SELECT measured_seconds FROM daily_summary WHERE station='W' AND day='2026-07-01'"
    ).fetchone()["measured_seconds"] == 3600


def test_import_stores_one_profile_row_per_day_not_per_mm(db):
    from datetime import datetime, timedelta, timezone

    n_rows = 5000
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    rows = [
        ((base + timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), i)
        for i in range(n_rows)
    ]
    import_csv(db, "P", csv_bytes(rows))
    assert db.execute("SELECT COUNT(*) c FROM daily_summary").fetchone()["c"] == 1
    # 旧版逐毫米尾分布表不再增长
    assert db.execute("SELECT COUNT(*) c FROM daily_level_tail").fetchone()["c"] == 0
    profile = db.execute("SELECT profile FROM daily_summary").fetchone()["profile"]
    # 摘要片段数只与观测段的水位变化有关（5000 秒跨度覆盖 4999 个整数毫米），
    # 同斜率相邻段会被合并，绝不按毫米逐档展开
    assert len(decode_profile(profile)) == 1
    assert len(profile) < 1000
