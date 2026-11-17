"""Tiny dependency-free SVG line charts for the generated reports."""
from __future__ import annotations

from html import escape

PALETTE = ["#2563eb", "#16a34a", "#dc2626", "#9333ea", "#ea580c", "#0891b2"]


def line_chart(series: list[dict], title: str, y_label: str, width: int = 860, height: int = 320,
               x_label: str = "seconds since start") -> str:
    """series: [{"name", "x": [...], "y": [...], "dashed": bool}]"""
    pad_l, pad_r, pad_t, pad_b = 70, 20, 36, 46
    xs = [x for s in series for x in s["x"]] or [0, 1]
    ys = [y for s in series for y in s["y"]] or [0, 1]
    x0, x1 = min(xs), max(xs) if max(xs) > min(xs) else min(xs) + 1
    y1 = max(ys) * 1.1 if max(ys) > 0 else 1
    w, h = width - pad_l - pad_r, height - pad_t - pad_b

    def sx(x):
        return pad_l + (x - x0) / (x1 - x0) * w

    def sy(y):
        return pad_t + h - (y / y1) * h

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
           f'font-family="Segoe UI, sans-serif" font-size="12">',
           f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
           f'<text x="{pad_l}" y="20" font-size="14" font-weight="600">{escape(title)}</text>']
    for i in range(6):
        yv = y1 * i / 5
        y = sy(yv)
        out.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + w}" y2="{y:.1f}" stroke="#e5e7eb"/>')
        out.append(f'<text x="{pad_l - 6}" y="{y + 4:.1f}" text-anchor="end" fill="#6b7280">{_fmt(yv)}</text>')
    for i in range(7):
        xv = x0 + (x1 - x0) * i / 6
        out.append(f'<text x="{sx(xv):.1f}" y="{pad_t + h + 16}" text-anchor="middle" fill="#6b7280">'
                   f'{xv:.0f}</text>')
    out.append(f'<text x="{pad_l + w / 2}" y="{height - 6}" text-anchor="middle" fill="#374151">'
               f'{escape(x_label)}</text>')
    out.append(f'<text x="14" y="{pad_t + h / 2}" transform="rotate(-90 14 {pad_t + h / 2})" '
               f'text-anchor="middle" fill="#374151">{escape(y_label)}</text>')
    for i, s in enumerate(series):
        if not s["x"]:
            continue
        color = s.get("color") or PALETTE[i % len(PALETTE)]
        pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in zip(s["x"], s["y"]))
        dash = ' stroke-dasharray="6 4"' if s.get("dashed") else ""
        out.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.8"{dash} points="{pts}"/>')
        lx = pad_l + 10 + i * 190
        out.append(f'<line x1="{lx}" y1="{height - 28}" x2="{lx + 18}" y2="{height - 28}" stroke="{color}" '
                   f'stroke-width="2"{dash}/>')
        out.append(f'<text x="{lx + 24}" y="{height - 24}" fill="#111827">{escape(s["name"])}</text>')
    out.append("</svg>")
    return "\n".join(out)


def _fmt(v: float) -> str:
    if v >= 1e6:
        return f"{v / 1e6:.1f}M"
    if v >= 1e3:
        return f"{v / 1e3:.0f}k"
    return f"{v:.0f}"
