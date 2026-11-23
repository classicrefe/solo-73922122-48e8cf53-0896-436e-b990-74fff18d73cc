"""精确有理数核算工具。

水位读数为有符号 64 位整数毫米，观测段内的线性插值使用 :class:`Fraction`
计算，避免超过 2**53 后浮点把相差 1 毫米的两笔读数误判为平水。

每日摘要只保存“日内线性片段”：
    (seconds, 段起点水位, 段终点水位)
不含原始时间戳，也不按整数毫米逐档展开。查询任意整数警戒线 h 时，
对每个片段直接求水位 >= h 的秒数并累加，结果仍为精确有理数。
"""
import json
from fractions import Fraction

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1

PROFILE_VERSION = 2
_SCALE = 1000  # 展示/舍入口径：3 位小数


class ProfileError(ValueError):
    """摘要内容损坏或版本不受支持。"""


def _fraction_to_text(value):
    value = Fraction(value)
    return f"{value.numerator}/{value.denominator}"


def _text_to_fraction(text):
    try:
        numerator, denominator = str(text).split("/", 1)
        return Fraction(int(numerator), int(denominator))
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise ProfileError(f"无法解析水位分数：{text!r}") from exc


def encode_profile(pieces):
    """把日内片段编码为 JSON 文本（每个 UTC 日只存一行）。"""
    payload = {
        "version": PROFILE_VERSION,
        "pieces": [
            [int(seconds), _fraction_to_text(start), _fraction_to_text(end)]
            for seconds, start, end in pieces
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def decode_profile(text):
    """解码 encode_profile 的结果，返回 [(seconds, Fraction, Fraction)]。"""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ProfileError("日内摘要不是有效的 JSON。") from exc
    if not isinstance(payload, dict) or payload.get("version") != PROFILE_VERSION:
        raise ProfileError("不支持的日内摘要版本。")
    raw_pieces = payload.get("pieces")
    if not isinstance(raw_pieces, list) or not raw_pieces:
        raise ProfileError("日内摘要缺少有效片段。")
    pieces = []
    for raw in raw_pieces:
        try:
            seconds = int(raw[0])
            start = _text_to_fraction(raw[1])
            end = _text_to_fraction(raw[2])
        except (TypeError, ValueError, IndexError) as exc:
            if isinstance(exc, ProfileError):
                raise
            raise ProfileError("日内摘要片段格式错误。") from exc
        if seconds <= 0:
            raise ProfileError("日内摘要片段时长必须为正。")
        pieces.append((seconds, start, end))
    return pieces


def profile_bounds(pieces):
    """返回片段的精确最低、最高水位。"""
    lows = [min(start, end) for _seconds, start, end in pieces]
    highs = [max(start, end) for _seconds, start, end in pieces]
    return min(lows), max(highs)


def threshold_seconds_from_pieces(pieces, threshold):
    """计算日内片段中水位 >= threshold 的精确秒数。

    threshold 可为任意 Python 整数（可超出 SQLite 整数范围）。
    - 平水片段：h <= 平水水位时整段计入（恰好等于也算），否则 0；
    - 线性片段：h <= 低端整段计入；低端 < h <= 高端按比例 (hi-h)/(hi-lo)；
    - h > 高端：0。
    """
    h = Fraction(threshold)
    total = Fraction(0)
    for seconds, start, end in pieces:
        if start == end:
            if h <= start:
                total += seconds
            continue
        if start < end:
            lo, hi = start, end
        else:
            lo, hi = end, start
        if h <= lo:
            total += seconds
        elif h <= hi:
            total += Fraction(seconds) * (hi - h) / (hi - lo)
    return total


class TailView:
    """按需计算 tail(h) 的只读视图（仅用于保持旧返回结构的兼容性）。

    不预先生成任何毫米档，也不写入数据库：每次访问都是对日内片段做一次
    精确求值，因此仍满足“不按毫米逐档展开”的要求。
    """

    __slots__ = ("_pieces",)

    def __init__(self, pieces):
        self._pieces = tuple(pieces)

    def __getitem__(self, threshold):
        return threshold_seconds_from_pieces(self._pieces, threshold)

    def get(self, threshold, default=None):
        if not isinstance(threshold, int):
            return default
        return self[threshold]

    def __contains__(self, threshold):
        if not isinstance(threshold, int):
            return False
        return self[threshold] != 0

    def __len__(self):
        raise TypeError("尾分布视图不预先展开毫米档，无法给出长度。")


class ThresholdCalculator:
    """按（片段签名）缓存每日达标秒数，便于一次查询合计大量日期。

    同一查询的警戒线固定，同站同日摘要不变；签名相同的结果完全相同，
    因此可在单次查询内安全复用，避免重复解析 JSON、重复求分数。
    """

    def __init__(self, threshold):
        self.threshold = threshold
        self._cache = {}
        self._h = Fraction(threshold)

    def seconds(self, pieces):
        signature = tuple((s, a.numerator, a.denominator, b.numerator, b.denominator)
                          for s, a, b in pieces)
        cached = self._cache.get(signature)
        if cached is None:
            cached = threshold_seconds_from_pieces(pieces, self._h)
            self._cache[signature] = cached
        return cached


def round_seconds(value):
    """按现有 3 位小数口径舍入（四舍五入、远离零），返回 Fraction。

    对精确有理数直接舍入，再展示/相加，避免浮点累计误差导致逐日与合计口径不一致。
    返回值分母一定整除 1000。
    """
    value = value if isinstance(value, Fraction) else Fraction(value)
    numerator, denominator = value.numerator, value.denominator
    quotient, remainder = divmod(abs(numerator) * _SCALE, denominator)
    if 2 * remainder >= denominator:  # 恰好 .0005 也进位（四舍五入）
        quotient += 1
    if numerator < 0:
        quotient = -quotient
    return Fraction(quotient, _SCALE)
