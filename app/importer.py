"""CSV 导入与逐日摘要核算。

规则：
- 单站有序 CSV，UTC 整秒时间戳，水位为有符号 64 位整数毫米。
- 相邻观测间隔 <= 1 小时：段内按直线（线性）估算水位，全程精确有理数计算。
- 相邻观测间隔 > 1 小时：该段判为未测，不做插值。
- 跨午夜的观测段按两个 UTC 日实际占秒分摊，仍使用同一条直线；统计口径为
  UTC 自然日（每天 86400 秒）。
- 只保存逐日摘要，原始观测不落库；日内摘要保存线性片段，不按毫米逐档展开。
- 同站同一天已有摘要即视为重叠，整批退回（不写入任何数据）。
"""
import csv
import io
import re
from datetime import datetime, timezone
from fractions import Fraction

from .exact import (
    INT64_MAX, INT64_MIN, TailView, encode_profile, profile_bounds,
)

DAY_SECONDS = 86400
MAX_GAP_SECONDS = 3600


class ImportError_(Exception):
    """导入阶段的用户可读错误（整批拒绝）。"""


_INT_RE = re.compile(r"^[+-]?\d+$")


def parse_timestamp(text, line_no):
    """解析 UTC 整秒 ISO-8601 时间戳，返回 epoch 秒（int）。"""
    text = text.strip()
    if not text:
        raise ImportError_(f"第 {line_no} 行：时间戳为空。")
    # 只接受秒级精度，拒绝小数秒
    if re.search(r"\d[\.,]\d+", text):
        raise ImportError_(f"第 {line_no} 行：只接受 UTC 整秒时间戳，发现小数秒：{text}")
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        raise ImportError_(f"第 {line_no} 行：无法识别的时间戳：{text}")
    if dt.tzinfo is None:
        raise ImportError_(f"第 {line_no} 行：时间戳必须带 UTC 时区（以 Z 或 +00:00 结尾）：{text}")
    if dt.utcoffset().total_seconds() != 0:
        raise ImportError_(f"第 {line_no} 行：只接受 UTC 时间戳：{text}")
    epoch = int(dt.timestamp())
    if datetime.fromtimestamp(epoch, tz=timezone.utc).timestamp() != epoch:
        raise ImportError_(f"第 {line_no} 行：时间戳必须为整秒：{text}")
    return epoch


def parse_level(text, line_no):
    text = text.strip()
    if not _INT_RE.match(text):
        raise ImportError_(f"第 {line_no} 行：水位必须是整数毫米：{text or '(空)'}")
    digits = text.lstrip("+-").lstrip("0") or "0"
    # 先按位数拦截，避免超长数字串触发 Python 的整数转换位数上限
    try:
        if len(digits) > 19:
            raise ValueError
        level = int(text)
    except ValueError:
        raise ImportError_(
            f"第 {line_no} 行：水位超出有符号 64 位整数范围"
            f"（{INT64_MIN} … {INT64_MAX}）：{text[:40]}"
        )
    if level < INT64_MIN or level > INT64_MAX:
        raise ImportError_(
            f"第 {line_no} 行：水位超出有符号 64 位整数范围"
            f"（{INT64_MIN} … {INT64_MAX}）：{text}"
        )
    return level


def parse_csv(raw_bytes):
    """解析上传的 CSV，返回 [(epoch_seconds, level_mm_int), ...]。"""
    try:
        text = raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportError_("文件不是有效的 UTF-8 文本。")
    reader = csv.reader(io.StringIO(text))
    rows = []
    last_ts = None
    first_data_row = True
    for line_no, row in enumerate(reader, start=1):
        if not row or all(not c.strip() for c in row):
            continue  # 跳过空行
        if len(row) < 2:
            raise ImportError_(f"第 {line_no} 行：至少需要“时间戳,水位”两列。")
        ts_text, level_text = row[0], row[1]
        # 首个非空行若两个单元格都不像数据（时间戳非时间、水位非整数）则视为表头
        if first_data_row and not _looks_like_timestamp(ts_text) \
                and not _INT_RE.match(level_text.strip()):
            first_data_row = False
            continue
        first_data_row = False
        ts = parse_timestamp(ts_text, line_no)
        level = parse_level(level_text, line_no)
        if last_ts is not None and ts <= last_ts:
            raise ImportError_(f"第 {line_no} 行：时间戳必须严格递增，发现 {ts} 不晚于上一条。")
        rows.append((ts, level))
        last_ts = ts
    if len(rows) < 2:
        raise ImportError_("有效观测不足 2 条，无法核算相邻观测段。")
    return rows


def _looks_like_timestamp(text):
    text = text.strip()
    if not text:
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def utc_day_bounds(epoch):
    """返回 epoch 所在 UTC 日的 0 点 epoch 和日期字符串。"""
    day_index = epoch // DAY_SECONDS
    start = day_index * DAY_SECONDS
    day = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%d")
    return start, day


def accumulate_segment(t1, l1, t2, l2, days):
    """把一个观测段 (t1,l1)->(t2,l2) 线性估算并按 UTC 日分摊进 days。

    days: {day: {"measured": int, "min": Fraction, "max": Fraction,
                  "pieces": [(piece_seconds, start_level, end_level)]}}
    段最长 3600 秒，最多跨一个午夜边界；跨午夜时两段共用同一条直线。
    """
    duration = t2 - t1
    slope = Fraction(l2 - l1, duration)

    def level_at(epoch):
        return Fraction(l1) + slope * (epoch - t1)

    first_start, first_day = utc_day_bounds(t1)
    _last_start, last_day = utc_day_bounds(t2)

    pieces = []
    if first_day == last_day:
        pieces.append((first_day, t1, t2))
    else:
        day_start = first_start
        day = first_day
        while day_start < t2:
            next_start = day_start + DAY_SECONDS
            seg_start = max(t1, day_start)
            seg_end = min(t2, next_start)
            if seg_end > seg_start:
                pieces.append((day, seg_start, seg_end))
            day_start = next_start
            day = _day_string(day_start)

    for day, seg_start, seg_end in pieces:
        piece_seconds = seg_end - seg_start
        if piece_seconds <= 0:
            continue  # 段端点恰好落在午夜边界时产生的零长度片段
        entry = days.setdefault(
            day, {"measured": 0, "min": None, "max": None, "pieces": []}
        )
        entry["measured"] += piece_seconds
        la, lb = level_at(seg_start), level_at(seg_end)
        lo, hi = (la, lb) if la <= lb else (lb, la)
        entry["min"] = lo if entry["min"] is None else min(entry["min"], lo)
        entry["max"] = hi if entry["max"] is None else max(entry["max"], hi)
        entry["pieces"].append((piece_seconds, la, lb))


def _day_string(day_start_epoch):
    return datetime.fromtimestamp(day_start_epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def _merge_pieces(pieces):
    """合并“水位连续且斜率相同”的相邻片段，进一步压缩日摘要。

    判定达标秒数只依赖各片段的水位区间与时长，不依赖时间位置，因此
    即使两段之间隔着未测缺口，只要水位首尾相接且斜率一致即可安全合并。
    """
    merged = []
    for seconds, start, end in pieces:
        if merged:
            p_seconds, p_start, p_end = merged[-1]
            if p_end == start and (p_end - p_start) * seconds == (end - start) * p_seconds:
                merged[-1] = (p_seconds + seconds, p_start, end)
                continue
        merged.append((seconds, start, end))
    return merged


def build_daily_summaries(rows):
    """从有序观测构建逐日摘要字典（只含日级信息，不展开毫米档）。"""
    days = {}
    unmeasured_notes = []
    for (t1, l1), (t2, l2) in zip(rows, rows[1:]):
        gap = t2 - t1
        if gap <= 0:
            raise ImportError_("观测时间戳必须严格递增。")
        if gap > MAX_GAP_SECONDS:
            # 未测段：不插值；其秒数在落库时按 86400 - measured 自然计入未测
            unmeasured_notes.append((t1, t2, gap))
            continue
        accumulate_segment(t1, l1, t2, l2, days)

    result = {}
    for day, entry in days.items():
        measured = entry["measured"]
        if measured <= 0:
            continue
        pieces = _merge_pieces(entry["pieces"])
        lo, hi = profile_bounds(pieces)
        result[day] = {
            "measured_seconds": measured,
            "unmeasured_seconds": DAY_SECONDS - measured,
            "min_level": lo,
            "max_level": hi,
            "pieces": pieces,
            "profile": encode_profile(pieces),
            # 兼容旧调用方：按需精确求值，不预生成、不落库任何毫米档
            "tails": TailView(pieces),
        }
    return result, unmeasured_notes


def _fraction_text(value):
    value = Fraction(value)
    return f"{value.numerator}/{value.denominator}"


def import_rows(conn, station, rows):
    """整批写入；任一天与已有摘要重叠则整批回滚。

    返回写入的天数与未测段数量。
    """
    station = station.strip()
    if not station:
        raise ImportError_("站码不能为空。")
    summaries, unmeasured_notes = build_daily_summaries(rows)
    if not summaries:
        raise ImportError_("所有相邻观测间隔都超过 1 小时，没有任何可核算的有测时段。")

    days = sorted(summaries)
    conn.execute("BEGIN IMMEDIATE")
    try:
        placeholders = ",".join("?" for _ in days)
        existing = conn.execute(
            f"SELECT day FROM daily_summary WHERE station = ? AND day IN ({placeholders})",
            [station, *days],
        ).fetchall()
        if existing:
            overlap = ", ".join(r["day"] for r in existing)
            raise ImportError_(f"与已有摘要重叠（{overlap}），整批退回，未写入任何数据。")

        for day in days:
            s = summaries[day]
            conn.execute(
                "INSERT INTO daily_summary "
                "(station, day, measured_seconds, unmeasured_seconds, min_level, max_level, profile) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (station, day, s["measured_seconds"], s["unmeasured_seconds"],
                 _fraction_text(s["min_level"]), _fraction_text(s["max_level"]),
                 s["profile"]),
            )
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except Exception:  # 事务可能已由 SQLite 自动回滚
            pass
        raise
    return {
        "days": days,
        "day_count": len(days),
        "unmeasured_gaps": len(unmeasured_notes),
        "observation_count": len(rows),
    }


def import_csv(conn, station, raw_bytes):
    rows = parse_csv(raw_bytes)
    return import_rows(conn, station, rows)
