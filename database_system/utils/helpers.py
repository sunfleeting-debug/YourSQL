"""通用辅助函数。"""

from __future__ import annotations

from database_system.utils.constants import value_str


def _display_width(text: str) -> int:
    """按「中文占 2 列」计算显示宽度。"""
    width = 0
    for ch in text:
        width += 2 if ord(ch) > 0x2E80 else 1
    return width


def _pad(text: str, width: int, align: str = "left") -> str:
    gap = width - _display_width(text)
    if gap <= 0:
        return text
    if align == "right":
        return " " * gap + text
    return text + " " * gap


def format_table(columns, rows, align_right: bool = True) -> str:
    """把查询结果格式化为 ASCII 表格。"""
    if not columns:
        return ""
    headers = [str(c) for c in columns]
    body = [[value_str(v) for v in row] for row in rows]
    widths = [_display_width(h) for h in headers]
    for row in body:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], _display_width(cell))

    def line(left: str, mid: str, right: str, fill: str = "-") -> str:
        return left + mid.join(fill * (w + 2) for w in widths) + right

    out = [line("+", "+", "+"), "| " + " | ".join(_pad(h, widths[i]) for i, h in enumerate(headers)) + " |"]
    out.append(line("+", "+", "+"))
    for row in body:
        cells = []
        for i, cell in enumerate(row):
            align = "right" if (align_right and _is_number(cell)) else "left"
            cells.append(_pad(cell, widths[i], align))
        out.append("| " + " | ".join(cells) + " |")
    out.append(line("+", "+", "+"))
    return "\n".join(out)


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False
