"""UI 对比度闸门：从 static/index.html 解析 token，按 UI优化方案 §6.2 校验 WCAG 对比度。

零依赖（仅标准库）。任一项不达标即退出码 1。

§6.2 要求：正文 >= 4.5:1，次级文字 >= 3:1，禁用态 >= 3:1。
额外纳入：主按钮面与卡片的非文本对比 >= 3:1（WCAG 1.4.11）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"


def _lin(c: float) -> float:
    c = c / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _lum(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def parse_color(v: str) -> tuple[int, int, int]:
    v = v.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", v)
    if m:
        h = m.group(1)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    m = re.fullmatch(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)", v)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    raise ValueError(f"无法解析颜色: {v!r}")


def parse_alpha(v: str) -> float:
    m = re.fullmatch(r"rgba\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*([\d.]+)\s*\)", v.strip())
    return float(m.group(1)) if m else 1.0


def blend(fg: tuple[int, int, int], alpha: float, bg: tuple[int, int, int]) -> tuple[int, int, int]:
    return tuple(round(alpha * f + (1 - alpha) * b) for f, b in zip(fg, bg))


def tokens(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", block):
        out[name] = value.strip()
    return out


def root_blocks(css: str) -> tuple[str, str]:
    light = re.search(r":root\s*\{(.*?)\}", css, re.S)
    if not light:
        raise SystemExit("未找到浅色 :root 块")
    dark = re.search(
        r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{(.*?)\}\s*\}",
        css, re.S,
    )
    return light.group(1), (dark.group(1) if dark else "")


CHECKS: list[tuple[str, str, str, float]] = [
    ("正文 text / card", "--text", "--card", 4.5),
    ("正文 text / card-soft", "--text", "--card-soft", 4.5),
    ("正文 text / bubble", "--text", "--bubble", 4.5),
    ("次级 text-2 / card", "--text-2", "--card", 3.0),
    ("次级 text-2 / bg", "--text-2", "--bg", 3.0),
    ("次级 text-2 / card-soft", "--text-2", "--card-soft", 3.0),
    ("次级 text-2 / fill-1（分段未选）", "--text-2", "--fill-1", 3.0),
    ("强调色文本 / card", "--accent", "--card", 4.5),
    ("强调色文本 / card-soft", "--accent", "--card-soft", 4.5),
    ("主按钮标签 / accent-solid", "--on-accent", "--accent-solid", 4.5),
    ("主按钮标签 / accent-solid-hover", "--on-accent", "--accent-solid-hover", 4.5),
    ("禁用按钮标签 / disabled-bg", "--on-accent", "--disabled-bg", 3.0),
    ("分段选中标签 / seg-thumb", "--text", "--seg-thumb", 4.5),
    ("告警文本 / card", "--warn", "--card", 3.0),
    ("主按钮面 / card（非文本）", "--accent-solid", "--card", 3.0),
]


def main() -> int:
    css = INDEX.read_text(encoding="utf-8")
    light_block, dark_block = root_blocks(css)
    light = tokens(light_block)
    dark = {**light, **tokens(dark_block)}

    failures = 0
    for mode, toks in (("LIGHT", light), ("DARK", dark)):
        toks = dict(toks, **{"--on-accent": "#ffffff"})
        # 建议卡底色 = accent-soft 叠加在 card 上
        tint = blend(parse_color(toks["--accent-soft"]), parse_alpha(toks["--accent-soft"]),
                     parse_color(toks["--card"]))
        print(f"===== {mode} （建议卡底 rgb{tint}）=====")
        for label, fg_key, bg_key, need in CHECKS + [("强调色文本 / 建议卡底", "--accent", "__tint", 4.5)]:
            fg = parse_color(toks[fg_key])
            bg = tint if bg_key == "__tint" else parse_color(toks[bg_key])
            ratio = contrast(fg, bg)
            ok = ratio >= need
            failures += 0 if ok else 1
            print(f"{'PASS' if ok else 'FAIL'} {ratio:5.2f} (>= {need})  {label}")

    print()
    if failures:
        print(f"对比度闸门：{failures} 项不达标")
        return 1
    print("对比度闸门：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())