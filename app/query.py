"""警戒线核算查询：只读逐日摘要，不触碰原始观测。

任意整数毫米警戒线 h（可超出 SQLite 有符号 64 位整数范围）的达标累计秒数：
- 新版摘要：读取日内线性片段，按精确有理数逐段求水位 >= h 的秒数；
- 旧版摘要（仅有 daily_level_tail）：沿用已存精度，min/max 判定后读取
  tail(h) 行；h 超出 SQLite 整数范围时按上升/下降包络直接判定，避免绑定报错。
恰好等于警戒线的平水段在两种摘要中都整体计入。
"""
import re
from datetime import datetime, timedelta
from fractions import Fraction

from .exact import (
    INT64_MAX, INT64_MIN, ProfileError, ThresholdCalculator, decode_profile,
    profile_bounds, round_seconds,
)

DAY_SECONDS = 86400
_INT_RE = re.compile(r"^[+-]?\d+$")
_LEGACY_EPSILON = Fraction(1, 10**9)


class QueryError(Exception):
    pass


def parse_day(text, field_name):
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise QueryError(f"{field_name} 格式应为 YYYY-MM-DD。")


def parse_threshold(text):
    raw = "" if text is None else str(text).strip()
    if not _INT_RE.match(raw):
        raise QueryError("警戒线必须是整数毫米。")
    try:
        return int(raw)
    except ValueError:  # 极长数字串（V8 位数保护等）也按非法输入处理
        raise QueryError("警戒线必须是整数毫米。")


def daterange(start_day, end_day):
    cur = start_day
    while cur <= end_day:
        yield cur
        cur += timedelta(days=1)


def _fraction_from_stored(value):
    """解析摘要中保存的水位：新版为 '分子/分母' 文本，旧版为 REAL。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return Fraction(str(value))  # 沿用旧摘要已存的十进制精度
    text = str(value).strip()
    if "/" in text:
        try:
            numerator, denominator = text.split("/", 1)
            return Fraction(int(numerator), int(denominator))
        except (ValueError, ZeroDivisionError) as exc:
            raise QueryError(f"摘要中的水位无法解析：{text!r}") from exc
    try:
        return Fraction(text)
    except ValueError as exc:
        raise QueryError(f"摘要中的水位无法解析：{text!r}") from exc


def _day_bounds(row):
    lo = _fraction_from_stored(row["min_level"])
    hi = _fraction_from_stored(row["max_level"])
    return lo, hi


def _legacy_threshold_seconds(row, tail_seconds, h):
    """旧摘要：沿用原有精度与 1e-9 容差口径。"""
    lo, hi = _day_bounds(row)
    hq = Fraction(h)
    if lo is not None and lo + _LEGACY_EPSILON >= hq:
        return Fraction(int(row["measured_seconds"]))
    if hi is not None and hi + _LEGACY_EPSILON < hq:
        return Fraction(0)
    if tail_seconds is None:
        # 与旧版查询一致：缺少对应尾分布行时按 0 秒处理。
        # 旧摘要只存过 SQLite 整数范围内的 tail 行，因此 h 超出该范围而又落在
        # 日内最低/最高之间时，只能沿用这一已存口径，不额外报错。
        return Fraction(0)
    return Fraction(str(tail_seconds))


def _profile_threshold_seconds(row, calculator):
    profile = row["profile"]
    if not profile:
        return None
    try:
        pieces = decode_profile(profile)
    except ProfileError as exc:
        raise QueryError(f"站点 {row['station']} / {row['day']} 的摘要损坏：{exc}") from exc
    return calculator.seconds(pieces), pieces


def _day_result(row, tail_seconds, calculator):
    if row is None:
        return Fraction(0), None, None
    profile_result = _profile_threshold_seconds(row, calculator)
    if profile_result is not None:
        # 新版摘要的极值直接由精确片段求取，不依赖列亲和性（旧库该列为 REAL）
        exact, pieces = profile_result
        lo, hi = profile_bounds(pieces)
        return exact, lo, hi
    lo, hi = _day_bounds(row)
    return _legacy_threshold_seconds(row, tail_seconds, calculator.threshold), lo, hi


def _needs_int64_binding(h):
    return INT64_MIN <= h <= INT64_MAX


def run_query(conn, station, start_text, end_text, threshold_text):
    station = (station or "").strip()
    if not station:
        raise QueryError("请填写站码。")
    start_day = parse_day(start_text, "开始日期")
    end_day = parse_day(end_text, "结束日期")
    if start_day > end_day:
        raise QueryError("开始日期不能晚于结束日期。")
    h = parse_threshold(threshold_text)
    calculator = ThresholdCalculator(h)

    days = list(daterange(start_day, end_day))
    total_span = len(days) * DAY_SECONDS
    start_key, end_key = start_day.isoformat(), end_day.isoformat()

    summaries = {
        r["day"]: r
        for r in conn.execute(
            "SELECT * FROM daily_summary WHERE station = ? "
            "AND day >= ? AND day <= ? ORDER BY day",
            (station, start_key, end_key),
        )
    }

    tails = {}
    if _needs_int64_binding(h):
        tail_rows = conn.execute(
            "SELECT day, seconds FROM daily_level_tail "
            "WHERE station = ? AND level_mm = ? AND day >= ? AND day <= ?",
            (station, h, start_key, end_key),
        ).fetchall()
        tails = {r["day"]: r["seconds"] for r in tail_rows}
    # h 超出 SQLite 整数范围时旧摘要不会存在对应 tail 行：
    # 高于 2**63-1 的 h 只有在日内最低水位 >= h 时才达标（由 min 判定）；
    # 低于 -2**63 的 h 一定被日内最低水位覆盖（由 min 判定），无需读 tail。

    per_day = []
    total_above = Fraction(0)
    total_measured = 0
    total_unmeasured = 0
    for d in days:
        key = d.isoformat()
        row = summaries.get(key)
        above, lo, hi = _day_result(row, tails.get(key), calculator)
        if row is None:
            measured, unmeasured = 0, DAY_SECONDS
        else:
            measured = int(row["measured_seconds"])
            unmeasured = int(row["unmeasured_seconds"])
        total_above += above
        total_measured += measured
        total_unmeasured += unmeasured
        per_day.append({
            "day": key,
            "measured_seconds": measured,
            "unmeasured_seconds": unmeasured,
            # 先对精确有理数舍入，再转为 float 兼容既有调用方
            "above_seconds": float(round_seconds(above)),
            "above_seconds_exact": above,
            "min_level": lo,
            "max_level": hi,
            "has_data": row is not None,
        })

    return {
        "station": station,
        "start": start_key,
        "end": end_key,
        "threshold_mm": h,
        "total_span_seconds": total_span,
        # 合计对精确累计值按 3 位小数舍入；逐日同样按精确值舍入后展示
        "above_seconds": float(round_seconds(total_above)),
        "above_seconds_exact": total_above,
        "measured_seconds": total_measured,
        "unmeasured_seconds": total_unmeasured,
        "per_day": per_day,
    }
