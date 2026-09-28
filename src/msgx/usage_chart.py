"""本地消费趋势：缺少账目与费用未知分别处理，不调用 API。"""
from __future__ import annotations

from datetime import date, timedelta

PURPOSE_COLORS = {"摘要总结": "#2563eb", "图片理解": "#b45309"}
FONT = "Microsoft YaHei UI"


def daily_series(rows: list[dict], end: date, days: int = 30) -> dict[str, list[dict]]:
    start = end - timedelta(days=days - 1)
    dates = [(start + timedelta(days=i)).isoformat() for i in range(days)]
    grouped = {}
    for row in rows:
        key = (row["purpose"], row["day"])
        if key[0] not in PURPOSE_COLORS or key[1] not in dates:
            continue
        item = grouped.setdefault(key, {"cost_cny": None, "unknown_calls": 0, "calls": 0})
        if row.get("cost_cny") is not None:
            item["cost_cny"] = (item["cost_cny"] or 0) + float(row["cost_cny"])
        item["unknown_calls"] += int(row.get("unknown_calls") or 0)
        item["calls"] += int(row.get("calls", 1))
    return {purpose: [dict(day=day, **grouped.get((purpose, day),
                         {"cost_cny": 0.0, "unknown_calls": 0, "calls": 0}))
                      for day in dates] for purpose in PURPOSE_COLORS}


def day_description(series: dict[str, list[dict]], index: int) -> str:
    parts = []
    for purpose, values in series.items():
        row = values[index]
        amount = "费用未知" if row["cost_cny"] is None else f"¥{row['cost_cny']:.6f}"
        if row["unknown_calls"]:
            amount += f"（另有 {row['unknown_calls']} 次费用未知）" if row["cost_cny"] is not None else f"（{row['unknown_calls']} 次）"
        parts.append(f"{purpose}：{amount}")
    return f"{next(iter(series.values()))[index]['day']}（北京） · " + "；".join(parts)


def draw_daily_costs(canvas, series: dict[str, list[dict]]) -> tuple[float, float]:
    """返回绘图区横坐标范围，供界面显示鼠标所在日期的金额。"""
    canvas.delete("all")
    width, height = max(canvas.winfo_width(), 260), max(canvas.winfo_height(), 145)
    left, right, bottom = 66, width - 18, height - 30
    legend_y, top = (29, 52) if width < 550 else (15, 40)
    rows = next(iter(series.values()))
    known = [row["cost_cny"] for values in series.values() for row in values if row["cost_cny"] is not None]
    ceiling = max(known, default=0) * 1.1 or 0.01
    canvas.create_text(left, 15, text="人民币 / 元（本地估算）", anchor="w", fill="#555555", font=(FONT, 9))
    for i, (purpose, color) in enumerate(PURPOSE_COLORS.items()):
        x = max(left, right - 205) + i * 105
        canvas.create_line(x, legend_y, x + 18, legend_y, fill=color, width=2)
        canvas.create_text(x + 23, legend_y, text=purpose, anchor="w", fill=color, font=(FONT, 9))
    for tick in range(4):
        value = ceiling * tick / 3
        y = bottom - (bottom - top) * tick / 3
        canvas.create_line(left, y, right, y, fill="#e5e7eb")
        canvas.create_text(left - 7, y, text=f"{value:.4f}", anchor="e", fill="#666666", font=(FONT, 8))
    canvas.create_line(left, top, left, bottom, right, bottom, fill="#999999")
    count = len(rows)
    def x_for(index: int) -> float:
        return left + (right - left) * index / max(count - 1, 1)
    for index in sorted({0, (count - 1) // 2, count - 1}):
        canvas.create_text(x_for(index), bottom + 16, text=rows[index]["day"][5:], fill="#666666", font=(FONT, 8))
    if not any(row["calls"] for values in series.values() for row in values):
        canvas.create_text((left + right) / 2, (top + bottom) / 2,
                           text="最近 30 天暂无当前用途的消费记录", fill="#666666", font=(FONT, 10))
        return left, right
    for purpose, values in series.items():
        color = PURPOSE_COLORS[purpose]
        previous = None
        for index, row in enumerate(values):
            x = x_for(index)
            if row["cost_cny"] is None:
                # 费用未知不是零：此处断线，并以橙色叉号提示。
                canvas.create_line(x - 3, bottom - 3, x + 3, bottom + 3, fill="#d97706", width=2)
                canvas.create_line(x - 3, bottom + 3, x + 3, bottom - 3, fill="#d97706", width=2)
                previous = None
                continue
            y = bottom - row["cost_cny"] / ceiling * (bottom - top)
            if previous is not None:
                canvas.create_line(*previous, x, y, fill=color, width=2, tags="series")
            if row["calls"]:
                canvas.create_oval(x - 3, y - 3, x + 3, y + 3,
                                   fill=color, outline="#d97706" if row["unknown_calls"] else color,
                                   width=2, tags="point")
            previous = (x, y)
    return left, right
