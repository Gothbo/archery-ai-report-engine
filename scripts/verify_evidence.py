# -*- coding: utf-8 -*-
"""A5 证据复算（A12 mock 重造后）。

读 eng 下 mock_data 训练数据集_4周_张明.json，复算并输出：
- 全样本各周均环（周2→周3、周1→周4 差值）
- 风档表：[0,1.5)/[1.5,2.0)/[2.0,2.5)/≥2.5 四档各周 n 与均环、单调性判断
- 门槛核验：各档 n ≥ 20 判定、[0,1.5) 档 W2→W3 与 W1→W4 差值 vs MDC 0.30

输出数字供《工程化方案》D4.1 与《决策记录 v2》附录回填（保持"可独立复算"承诺）。
"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = BASE_DIR / "mock_data" / "训练数据集_4周_张明.json"
WIND_BANDS = [(0.0, 1.5), (1.5, 2.0), (2.0, 2.5), (2.5, 99.0)]  # 左闭右开
MDC_AVGSCORE = 0.30
MIN_N = 20


def load_data():
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def band_of(w: float) -> int:
    for i, (lo, hi) in enumerate(WIND_BANDS):
        if lo <= w < hi:
            return i
    raise ValueError(f"风速 {w} 不在任何风档（左闭右开）")


def main():
    data = load_data()
    sessions = data["sessions"]

    # 全样本按周：只取有风速的箭（风档归因需要风速；缺测风速不参与风档统计）
    weekly = {w: [] for w in range(1, 5)}
    bands: dict[int, dict[int, list[float]]] = {b: {w: [] for w in range(1, 5)} for b in range(4)}
    for s in sessions:
        w = s["weekNo"]
        for sh in s["shots"]:
            weekly[w].append(sh["score"])
            if sh["windSpeed"] is not None:
                b = band_of(sh["windSpeed"])
                bands[b][w].append(sh["score"])

    print("== 全样本各周均环 ==")
    avg = {w: round(sum(v) / len(v), 3) for w, v in sorted(weekly.items())}
    for w in sorted(avg):
        print(f"  W{w}: n={len(weekly[w])}  均环={avg[w]}")
    diff_23 = round(avg[3] - avg[2], 3)
    diff_14 = round(avg[4] - avg[1], 3)
    print(f"  W2→W3 差值 = {diff_23}  (MDC {MDC_AVGSCORE}) → {'跨过' if diff_23 >= MDC_AVGSCORE else '未跨'}")
    print(f"  W1→W4 差值 = {diff_14}  (MDC {MDC_AVGSCORE}) → {'跨过' if diff_14 >= MDC_AVGSCORE else '未跨'}")

    print("\n== 风档表（左闭右开，仅计有风速箭）==")
    names = ["[0,1.5)", "[1.5,2.0)", "[2.0,2.5)", "≥2.5"]
    print(f"  {'风档':<9} {'周':<3} {'n':<4} {'均环':<7} {'≥20':<4} 单调性")
    mono_all = True
    for b in range(4):
        vals = [bands[b][w] for w in range(1, 5)]
        week_avg = {w: round(sum(v) / len(v), 3) for w, v in enumerate(vals, start=1) if v}
        n_ok = {w: len(bands[b][w]) >= MIN_N for w in range(1, 5)}
        seq = [week_avg[w] for w in range(1, 5) if w in week_avg]
        mono = all(seq[i] < seq[i + 1] for i in range(len(seq) - 1)) if len(seq) >= 2 else "n/a"
        if isinstance(mono, bool):
            mono_all = mono_all and mono
        for w in range(1, 5):
            if not bands[b][w]:
                continue
            print(f"  {names[b]:<9} W{w}  n={len(bands[b][w]):<3} {week_avg[w]:<7} {str(n_ok[w]):<4}  "
                  f"{mono if w == 1 else ''}")
    print(f"\n四档全周单调：{mono_all}")

    # [0,1.5) 档核心判定
    b0 = bands[0]
    a1, a4 = round(sum(b0[1]) / len(b0[1]), 3), round(sum(b0[4]) / len(b0[4]), 3)
    a2, a3 = round(sum(b0[2]) / len(b0[2]), 3), round(sum(b0[3]) / len(b0[3]), 3)
    d23, d14 = round(a3 - a2, 3), round(a4 - a1, 3)
    print(f"\n== [0,1.5) 档核心判定 ==")
    print(f"  W1 n={len(b0[1])} 均环={a1}  W2 n={len(b0[2])} 均环={a2}  W3 n={len(b0[3])} 均环={a3}  W4 n={len(b0[4])} 均环={a4}")
    print(f"  W2→W3 差值 {d23} vs MDC {MDC_AVGSCORE} → {'跨过（进步显著）' if d23 >= MDC_AVGSCORE else '未跨（进步不显著）'}")
    print(f"  W1→W4 差值 {d14} vs MDC {MDC_AVGSCORE} → {'跨过（进步显著）' if d14 >= MDC_AVGSCORE else '未跨（进步不显著）'}")

    if not mono_all:
        print("\n注意：存在非单调风档，文档风档证据段需如实改写（A5 诚实原则）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
