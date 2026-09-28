"""金额工具：元（十进制字符串）<-> 分（int）。

约定：
- 内部计算一律使用 ``int`` 分；
- 输入为十进制字符串，最多接受 2 位小数，更小单位直接拒绝（静默四舍五入会隐藏错误）；
- 由 Decimal 派生的金额使用 ROUND_HALF_UP（银行常见的四舍五入，负数按绝对值处理）。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from app.errors import InvalidAmountError

_CENTS = Decimal("0.01")


def yuan_to_cents(text: str) -> int:
    """把元的十进制字符串转为分。

    例：``"100.05" -> 10005``。非法输入、负数、超过两位小数都会被拒绝。
    """
    if text is None:
        raise InvalidAmountError("金额不能为空", value=text)
    raw = str(text).strip()
    if not raw:
        raise InvalidAmountError("金额不能为空", value=text)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise InvalidAmountError("金额不是合法的十进制数字", value=text) from exc
    if not value.is_finite():
        raise InvalidAmountError("金额必须是有限数字", value=text)
    if value < 0:
        raise InvalidAmountError("金额不能为负", value=text)
    if value == 0:
        raise InvalidAmountError("金额必须大于 0", value=text)
    # 拒绝三位及以上小数，避免精度歧义
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise InvalidAmountError("金额最小单位为分（两位小数）", value=text)
    return int((value * 100).to_integral_value(rounding=ROUND_HALF_UP))


def cents_to_yuan(value: int) -> str:
    """分转元字符串，保留两位小数。"""
    sign = "-" if value < 0 else ""
    value = abs(value)
    return f"{sign}{value // 100}.{value % 100:02d}"


def quantize_cents(value: Decimal) -> int:
    """Decimal 元 -> int 分，ROUND_HALF_UP。"""
    quantized = value.quantize(_CENTS, rounding=ROUND_HALF_UP)
    return int((quantized * 100).to_integral_value(rounding=ROUND_HALF_UP))
