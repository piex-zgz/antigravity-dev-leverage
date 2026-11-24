#!/usr/bin/env python3
"""Document aesthetics & anti-meta-narration Gate scanner for Markdown files.

Flags:
  1. Self-defensive / meta-narration phrases (per writing discipline).
  2. Generic AI cliché / filler openings and transitions.

Usage:
  doc_style_check.py <file1.md> [file2.md ...] [--format markdown|json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

RULES: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "meta_narration",
        "自我辩白/元叙述：直接给出结论或案例，不替先前思路辩护",
        re.compile(
            r"(只是帮助我们发现问题的起点|(?:我们的?)?本意不是|这并不是?最终落脚点|只是出发点|"
            r"并非为了[^，。；\n]{1,16}而是为了|这里需要澄清的是|需要说明的是我们并不是)"
        ),
    ),
    (
        "ai_cliche",
        "AI 烂大街套话/空洞过渡语：删掉空话，直接写事实与判断",
        re.compile(
            r"(在当今[^，。；\n]{0,12}(浪潮|时代|背景下)|总而言之[，,]?\s*言而总之|"
            r"综上所述[，,]?\s*不难发现|值得注意的是[，,]?\s*我们需要意识到|"
            r"毋庸置疑的是|不可否认的是|赋能[^，。；\n]{0,10}生态闭环|"
            r"全方位多维度|降本增效的全新范式|携手共创|显而易见的是)"
        ),
    ),
    (
        "hedging_filler",
        "过度铺垫/低信息密度套话：考虑精简为直接结论",
        re.compile(
            r"(让我们一起来看看|接下来我将为您详细|希望以上内容对您有所帮助|"
            r"如果您有任何其他问题|作为一个AI|在深入探讨之前)"
        ),
    ),
]


def scan_file(path: Path) -> tuple[list[dict[str, Any]], bool]:
    findings: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return (
            [{"file": str(path), "line": 0, "rule": "read_error", "match": str(exc), "advice": "Check file path/permissions"}],
            True,
        )

    in_code_fence = False
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        # Ignore inline code spans so quoted anti-pattern examples in docs do not false-positive
        prose = re.sub(r"`[^`]+`", "", line)
        for rule_id, advice, pattern in RULES:
            for m in pattern.finditer(prose):
                findings.append(
                    {
                        "file": str(path),
                        "line": idx,
                        "rule": rule_id,
                        "match": m.group(0),
                        "excerpt": stripped[:100],
                        "advice": advice,
                    }
                )
    return findings, False


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Markdown docs for meta-narration and AI clichés")
    parser.add_argument("files", nargs="+")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    args = parser.parse_args()

    all_findings: list[dict[str, Any]] = []
    had_io_error = False
    for f in args.files:
        file_findings, err = scan_file(Path(f))
        all_findings.extend(file_findings)
        had_io_error = had_io_error or err

    status = "ERROR" if had_io_error else ("PASS" if not all_findings else "WARN")
    if args.format == "json":
        print(
            json.dumps(
                {"status": status, "count": len(all_findings), "findings": all_findings},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2 if had_io_error else 0

    if not all_findings:
        print(f"PASS: Checked {len(args.files)} file(s); 0 meta-narration or AI cliché hits.")
        return 0

    print(f"{status}: Found {len(all_findings)} issue(s) across {len(args.files)} file(s):\n")
    print("| Location | Rule | Match | Advice |")
    print("| :--- | :--- | :--- | :--- |")
    for item in all_findings:
        loc = f"`{Path(item['file']).name}:{item['line']}`"
        print(f"| {loc} | `{item['rule']}` | `{item['match']}` | {item['advice']} |")
    return 2 if had_io_error else 0


if __name__ == "__main__":
    sys.exit(main())
