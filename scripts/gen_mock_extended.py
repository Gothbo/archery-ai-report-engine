# -*- coding: utf-8 -*-
"""扩展版 mock 数据生成器（14 周，演示"报告效果"用）。

设计故事（张明，反曲弓，2026-W27 ~ W40，完整 Q3 + 当前周）：
- 入队测试场 S101（2026-06-29）：锚点来源场（avg ~9.55 / mcrT ~0.46 / 散布 ~14mm）
- 进步期 W27-W32：周均环 9.62 → 10.12（先稳态后超 MDC，MDC=0.30）
- 平台期 W33-W36：均环 10.12~10.16 波动（差值稳定超 MDC，报告呈"持续进步但放缓"）
- 再进步 W37-W40：均环 10.18 → 10.24
- 撒放用时 mcrT：W27-W31 稳（~0.458），W32 起持续改善（0.43 → 0.37），
  差值 0.06+ 超 MDC 0.05 → 进步判定
- 风暴场（风 >2.5）：W29-3 / W33-2 / W37-1；状态不佳日 W31-1（命中率低）；
  高心率场 W35-3；跨日早训场 W40-3（22:00Z → 北京次日 06:00）
- 缺测：~4% heartRate、~3% windSpeed（每场有效 ≥ 20，不破样本门槛）

约束沿用：seed=42、单场跨度 ≥20 分钟（箭间隔 40-60s）、物理自洽（环值/坐标/MCRT）、
字段对齐 v1.2（scoreId/shottingMode/bowType）。sessionId 用 S101 起（避开旧 4 周数据 S001-S013）。
"""
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

random.seed(42)

LOCAL_TZ = ZoneInfo("Asia/Shanghai")

OUT_DIR = Path(__file__).resolve().parent.parent / "mock_data"
OUT_DIR.mkdir(exist_ok=True)

WEEKS_FROM = 27   # 2026-W27（2026-06-29 周一）
WEEKS_TO = 40     # 2026-W40（含当前周，界面默认周有数据）
SESSIONS_PER_WEEK = 3
SHOTS_PER_SESSION = 30
MIN_SESSION_SPAN_MS = 20 * 60 * 1000
SESSION_GAP_DAYS = 2  # 周一/三/五训练
SESSION_START_HOUR = 9  # 09:00Z = 北京 17:00 训练时段
MISS_HR_RATE = 0.04
MISS_WIND_RATE = 0.03

ATHLETE_ID = "1963169497552654337"

# 周均环目标（锚点 9.55，MDC 0.30 → W29 起超阈值判进步）
WEEK_AVG_TARGET = {
    27: 9.62, 28: 9.74, 29: 9.86, 30: 9.98, 31: 10.06, 32: 10.12,
    33: 10.14, 34: 10.12, 35: 10.16, 36: 10.13, 37: 10.18, 38: 10.20,
    39: 10.22, 40: 10.24,
}
# 锚点场（入队测试）skill 基准略低于首周目标
ANCHOR_SKILL = 9.55

# mcrT 周均值（锚 0.46，MDC 0.05 方向 -1 → W32 起差值超阈值判进步）
WEEK_MCRT = {
    27: 0.458, 28: 0.456, 29: 0.460, 30: 0.455, 31: 0.452, 32: 0.430,
    33: 0.420, 34: 0.410, 35: 0.398, 36: 0.390, 37: 0.385, 38: 0.378,
    39: 0.372, 40: 0.365,
}

STORM_SESSIONS = {(29, 3), (33, 2), (37, 1)}      # (week, session_idx) 风暴
BAD_DAY_SESSION = (31, 1)                         # 状态不佳（命中率低/远弹多）
HIGH_HR_SESSION = (35, 3)                         # 高心率（强度/紧张场）

ATHLETE = {
    "athleteId": ATHLETE_ID,
    "name": "张明",
    "archeryType": "反曲弓",
    "level": "省队",
    "gender": "男",
    "age": 22,
    "baseline": {
        "avgScore": 9.55,        # 入队测试锚点（S101 记分箭实算）
        "mcrT": 0.46,
        "hrVolatility": 14.0,
        "dispersionMm": 14.0,
        "collectedAtUtc": "2026-06-29T00:00:00.000Z",
    },
}


def iso_utc(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts_ms % 1000:03d}Z"


def gen_heart_rate(shot_idx, high=False):
    ramp = min(shot_idx / SHOTS_PER_SESSION, 1.0)
    hr = (68 + ramp * 35 + (25 if high else 0)) + random.gauss(0, 9)
    return int(max(50, min(155, hr)))


def gen_wind(week_no, session_idx):
    stormy = (week_no, session_idx) in STORM_SESSIONS
    mid = session_idx == 2
    base = 2.8 if stormy else (1.6 if mid else 0.8)
    return max(0.0, min(5.0, base + abs(random.gauss(0, 0.7))))


def gen_score(skill_mean, wind, bad_day=False):
    wind_penalty = 0.0
    if wind > 2.0:
        wind_penalty = 0.15 + (wind - 2.0) * 0.2
    if wind > 3.5:
        wind_penalty += 0.15
    penalty = wind_penalty + (0.35 if bad_day else 0.0)
    raw = skill_mean - penalty + random.gauss(0, 0.28)
    return round(max(0.0, min(10.9, raw)), 1)


def gen_xy(score):
    if score <= 0.5:  # 脱靶/远弹：坐标离散
        return round(random.gauss(0, 55), 1), round(random.gauss(0, 55), 1)
    radius = max(0.0, (10.9 - score)) * 8.0 + random.gauss(0, 3.0)
    angle = random.uniform(0, 2 * math.pi)
    return round(radius * math.cos(angle), 1), round(radius * math.sin(angle), 1)


def gen_mcrt(week_no):
    return round(WEEK_MCRT[week_no] + random.gauss(0, 0.02), 3)


def gen_session(week_no, session_idx, global_idx, start_ts_ms, force_cross_day=False):
    skill_mean = WEEK_AVG_TARGET[week_no] + random.uniform(-0.02, 0.02)
    if global_idx == 0:
        skill_mean = ANCHOR_SKILL  # S101 入队测试场
    bad_day = (week_no, session_idx) == BAD_DAY_SESSION
    high_hr = (week_no, session_idx) == HIGH_HR_SESSION
    ts_ms = start_ts_ms
    shots = []
    for i in range(SHOTS_PER_SESSION):
        wind = gen_wind(week_no, session_idx)
        score = gen_score(skill_mean, wind, bad_day)
        miss = random.random() < 0.06 if bad_day else random.random() < 0.01
        hit = score >= 1.0 and not miss
        x, y = gen_xy(score if not miss else 0.0)
        heart_rate = None if random.random() < MISS_HR_RATE else gen_heart_rate(i, high_hr)
        wind_speed = None if random.random() < MISS_WIND_RATE else round(wind, 1)
        wind_dir = None if wind_speed is None else random.randint(0, 359)
        shots.append({
            "scoreId": str(int(ATHLETE_ID) + global_idx * 10000 + i),
            "score": score,
            "hit": hit,
            "xMm": x,
            "yMm": y,
            "mcrT": gen_mcrt(week_no),
            "heartRate": heart_rate,
            "windSpeed": wind_speed,
            "windDirectionDeg": wind_dir,
            "shootingMode": 0 if i < 6 else 1,   # 前 6 支试射，其余记分
            "bowType": "反曲弓",
            "shootingTimeUtc": iso_utc(ts_ms),
        })
        ts_ms += random.randint(40000, 60000)
    return {
        "sessionId": f"S{100 + global_idx + 1:03d}",
        "weekNo": week_no,
        "sessionIdx": session_idx,
        "crossDay": force_cross_day,
        "distanceM": 70,
        "dateUtc": shots[0]["shootingTimeUtc"],
        "shots": shots,
    }


def weekly_avg_scores(sessions):
    by_week: dict[int, list[float]] = {}
    for s in sessions:
        by_week.setdefault(s["weekNo"], []).extend(sh["score"] for sh in s["shots"])
    return {w: round(sum(v) / len(v), 3) for w, v in sorted(by_week.items())}


def self_check(sessions):
    print("== 扩展 mock 自检 ==")
    # 1) 单场时长
    for s in sessions:
        span = (
            datetime.fromisoformat(s["shots"][-1]["shootingTimeUtc"].replace("Z", "+00:00"))
            - datetime.fromisoformat(s["shots"][0]["shootingTimeUtc"].replace("Z", "+00:00"))
        ).total_seconds() * 1000
        assert span >= MIN_SESSION_SPAN_MS, f"{s['sessionId']} 时长 {span/1000:.1f}min < 20min"
    print(f"1) 单场时长 ≥ 20 分钟：{len(sessions)} 场全部通过")

    # 2) 缺测
    miss_hr = sum(1 for s in sessions for sh in s["shots"] if sh["heartRate"] is None)
    miss_wind = sum(1 for s in sessions for sh in s["shots"] if sh["windSpeed"] is None)
    total = len(sessions) * SHOTS_PER_SESSION
    for s in sessions:
        valid = sum(1 for sh in s["shots"] if sh["heartRate"] is not None)
        assert valid >= 20, f"{s['sessionId']} 有效心率样本 {valid} < 20"
    print(f"2) 缺测：heartRate {miss_hr} ({miss_hr/total:.1%})、windSpeed {miss_wind} ({miss_wind/total:.1%})；每场有效 ≥20 通过")

    # 3) 跨日场
    cross = next(s for s in sessions if s["crossDay"])
    dt = datetime.fromisoformat(cross["dateUtc"].replace("Z", "+00:00"))
    assert dt.astimezone(LOCAL_TZ).day != dt.day, "跨日场本地日期应 ≠ UTC 日期"
    print(f"3) 跨日场 {cross['sessionId']}：UTC {dt.day} 日 → 北京次日 {dt.astimezone(LOCAL_TZ).day} 日 早训 通过")

    # 4) 周均环故事（先稳后进；W32 起差值 vs 锚 9.55 超 MDC 0.30）
    avgs = weekly_avg_scores(sessions)
    anchor = ANCHOR_SKILL
    w27_diff = avgs[27] - anchor
    w32_diff = avgs[32] - anchor
    w40_diff = avgs[40] - anchor
    assert w27_diff < 0.30, f"W27 应未超 MDC（稳态）：{w27_diff}"
    assert w32_diff >= 0.30, f"W32 应超 MDC（进步）：{w32_diff}"
    assert w40_diff >= 0.60, f"W40 应明显进步：{w40_diff}"
    assert avgs[40] >= avgs[32], f"周均环不应倒退：{avgs}"
    print(f"4) 周均环故事：W27 {avgs[27]}（+{w27_diff:.2f} 稳态）→ W32 {avgs[32]}（+{w32_diff:.2f} 进步）→ W40 {avgs[40]}（+{w40_diff:.2f} 明显进步）通过")

    # 5) 风暴场
    for wk, sidx in STORM_SESSIONS:
        s = next(s for s in sessions if s["weekNo"] == wk and s["sessionIdx"] == sidx)
        windy = sum(1 for sh in s["shots"] if (sh["windSpeed"] or 0) >= 2.5)
        assert windy >= 20, f"风暴场 {s['sessionId']} 大风箭 {windy} < 20"
    print("5) 风暴场（≥2.5m/s 箭 ≥20）：3 场通过")

    # 6) 滚动基线资源（记分箭 24/场 → W31 首周满 288）
    print(f"6) 记分箭总量：{sum(1 for s in sessions for sh in s['shots'] if sh['shootingMode'] == 1)}（滚动基线 288 箭在 W31 前后补满）")
    print(f"   总箭数：{total}，场次：{len(sessions)}")


def main():
    sessions = []
    g = 0
    for w in range(WEEKS_FROM, WEEKS_TO + 1):
        # 场次严格对齐 ISO 周：周一 09:00Z 起，隔天一场（修正连续 +2 天导致的周漂移）
        monday = datetime.fromisocalendar(2026, w, 1).replace(
            hour=SESSION_START_HOUR, tzinfo=timezone.utc)
        for s in range(1, SESSIONS_PER_WEEK + 1):
            start = monday.timestamp() * 1000 + (s - 1) * SESSION_GAP_DAYS * 86400000
            sessions.append(gen_session(w, s, g, int(start)))
            g += 1
    # 跨日早训场（W40 周五 22:00Z → 北京周六 06:00）
    cross_start = datetime(2026, 10, 2, 22, 0, 0, tzinfo=timezone.utc)
    sessions.append(gen_session(40, 4, g, int(cross_start.timestamp() * 1000), force_cross_day=True))

    data = {"athlete": ATHLETE, "sessions": sessions}
    out = OUT_DIR / "训练数据集_14周_张明.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    self_check(sessions)
    print(f"生成完成: {out}（{len(sessions)} 场 × {SHOTS_PER_SESSION} 箭，2026-W27 ~ W40）")


if __name__ == "__main__":
    main()
