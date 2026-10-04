# -*- coding: utf-8 -*-
"""内容截断自检。

**原则：任何截断都必须是显式的、可度量的、可失败的。**

背景：项目里曾经有一处 `_MAX_INPUT_CHARS=45000` 的「取头 + 取尾、丢掉中间」，
论文全文 110K 字，于是中间约 6 万字（方法与实验的核心）从未进入 Planner，
而日志里一个字都没提。表现为成品里大量「（表 3）」指代却没有对应表格。

用法：
    cov = Coverage()
    cov.record("planner", total=len(fulltext), sent=len(text),
               detail="按页等比裁剪")
    ...
    print(cov.report())
    if P2P_STRICT=1: cov.raise_if_violated()

阈值：默认丢弃超过 5% 就算违规（可在 .env 用 P2P_COVERAGE_TOLERANCE 调）。
"""
from __future__ import annotations

import os

DEFAULT_TOLERANCE = 0.05


def tolerance() -> float:
    try:
        return float(os.environ.get("P2P_COVERAGE_TOLERANCE", str(DEFAULT_TOLERANCE)))
    except ValueError:
        return DEFAULT_TOLERANCE


class Coverage:
    """记录每个环节「应送入 vs 实际送入」的字符数。"""

    def __init__(self, records: list[dict] | None = None):
        self.records: list[dict] = list(records or [])

    def record(self, stage: str, total: int, sent: int,
               detail: str = "", dropped_items: list[str] | None = None,
               unit: str = "字") -> dict:
        """unit: 计量单位（字 / 页 / 块）。别把页数写成"字"——报告要能自证准确。"""
        total = max(0, int(total))
        sent = max(0, int(sent))
        dropped = max(0, total - sent)
        ratio = (dropped / total) if total else 0.0
        rec = {
            "stage": stage,
            "total": total,
            "sent": sent,
            "dropped": dropped,
            "ratio": round(ratio, 4),
            "unit": unit,
            "detail": detail,
            "dropped_items": list(dropped_items or [])[:20],
        }
        self.records.append(rec)
        if dropped:
            shown = ("；丢弃项：" + ", ".join(rec["dropped_items"])) if rec["dropped_items"] else ""
            print(f"[coverage] {stage}: 送入 {sent}/{total} {unit}"
                  f"（丢弃 {ratio:.1%}）{('，' + detail) if detail else ''}{shown}")
        return rec

    def violations(self, tol: float | None = None) -> list[dict]:
        tol = tolerance() if tol is None else tol
        return [r for r in self.records if r["ratio"] > tol]

    def report(self) -> str:
        if not self.records:
            return "[coverage] 无记录"
        lines = ["## 内容覆盖自检", ""]
        for r in self.records:
            flag = "⚠" if r["ratio"] > tolerance() else "✓"
            unit = r.get("unit", "字")
            lines.append(f"- {flag} {r['stage']}: 送入 {r['sent']}/{r['total']} {unit}"
                         f"（丢弃 {r['ratio']:.1%}）"
                         + (f" — {r['detail']}" if r["detail"] else ""))
            if r["dropped_items"]:
                lines.append(f"    丢弃: {', '.join(r['dropped_items'])}")
        v = self.violations()
        lines.append("")
        lines.append(f"结论：{'⚠ 有 ' + str(len(v)) + ' 处超出容忍度' if v else '✓ 全部在容忍度内'}"
                     f"（阈值 {tolerance():.0%}）")
        return "\n".join(lines)

    def raise_if_violated(self, tol: float | None = None) -> None:
        v = self.violations(tol)
        if v:
            detail = "；".join(f"{r['stage']} 丢弃 {r['ratio']:.1%}" for r in v)
            raise RuntimeError(f"内容覆盖自检未通过（P2P_STRICT=1）：{detail}")


def as_dicts(cov: "Coverage | list[dict] | None") -> list[dict]:
    if cov is None:
        return []
    return cov.records if isinstance(cov, Coverage) else list(cov)


def from_state(state: dict | None) -> Coverage:
    return Coverage((state or {}).get("coverage") or [])
