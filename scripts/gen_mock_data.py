# -*- coding: utf-8 -*-
"""A12 mock 数据重造（eng 版生成器）。

在 MVP gen_mock_data.py 基础上修复 3 项（决策记录 v2 / 推进计划 Step 6）：
1. 单场时长 ≥ 20 分钟：箭间隔 40–60s（原 20–40s ≈ 14.5 分钟，心率失真）
2. 注入缺测箭：~4% heartRate→null、~3% windSpeed→null（windDirectionDeg 同步 null），
   每场有效样本仍 ≥ 20（不破样本门槛）
3. 补 S013 跨日场次：22:00Z 开训（北京次日 06:00 早训），本地日期 ≠ UTC 日期（A1 有真实数据支撑）

保留：seed=42、逐周技能进步、风暴场（第 3/9 场）、环值/坐标/MCRT 物理自洽、字段对齐 v1.2。
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

WEEKS = 4
SESSIONS_PER_WEEK = 3
SHOTS_PER_SESSION = 30
MIN_SESSION_SPAN_MS = 20 * 60 * 1000  # 单场跨度 ≥ 20 分钟

# 常规场次第 1 场开训时刻：2026-08-03 09:00:00Z（北京 17:00 训练时段语义 +8h）
FIRST_SESSION_START = datetime(2026, 8, 3, 9, 0, 0, tzinfo=timezone.utc)
SESSION_GAP_DAYS = 2  # 隔天一场
CROSS_DAY_SESSION_START = datetime(2026, 8, 27, 22, 0, 0, tzinfo=timezone.utc)  # 北京次日 06:00 早训

MISS_HR_RATE = 0.04
MISS_WIND_RATE = 0.03

ATHLETE = {
    "athleteId": "1963169497552654337",
    "name": "张明",
    "archeryType": "反曲弓",
    "level": "省队",
    "gender": "男",
    "age": 22,
    "baseline": {
        "avgScore": 9.62,
        "mcrT": 0.46,
        "hrVolatility": 12.0,
        "dispersionMm": 13.8,
        "collectedAtUtc": "2026-07-20T00:00:00.000Z",
    },
}


def iso_utc(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts_ms % 1000:03d}Z"


def gen_heart_rate(shot_idx):
    ramp = min(shot_idx / SHOTS_PER_SESSION, 1.0)
    hr = 68 + ramp * 35 + random.gauss(0, 9)
    return int(max(50, min(155, hr)))


def gen_wind(session_idx):
    # 第 3、9 场风大，其余正常
    stormy = session_idx in (2, 8)
    base = 2.6 if stormy else 1.0
    return max(0.0, min(5.0, base + abs(random.gauss(0, 0.8))))


def gen_score(skill_mean, wind):
    wind_penalty = 0.0
    if wind > 2.0:
        wind_penalty = 0.15 + (wind - 2.0) * 0.2
    if wind > 3.5:
        wind_penalty += 0.15
    raw = skill_mean - wind_penalty + random.gauss(0, 0.28)
    return round(max(0.0, min(10.9, raw)), 1)


def gen_xy(score):
    if score == 0.0:
        return random.gauss(0, 60), random.gauss(0, 60)
    radius = max(0.0, (10.9 - score)) * 8.0 + random.gauss(0, 3.0)
    angle = random.uniform(0, 2 * math.pi)
    return round(radius * math.cos(angle), 1), round(radius * math.sin(angle), 1)


def gen_mcrt(week_no):
    return round(0.45 - 0.07 * (week_no / WEEKS) + random.gauss(0, 0.03), 3)


def gen_session(week_no, session_idx, global_idx, start_ts_ms, force_cross_day=False):
    skill_mean = 9.55 + 0.8 * (week_no / WEEKS) + random.uniform(-0.01, 0.01)
    ts_ms = start_ts_ms
    shots = []
    for i in range(SHOTS_PER_SESSION):
        wind = gen_wind(global_idx)
        score = gen_score(skill_mean, wind)
        hit = score >= 1.0 and random.random() > 0.01
        x, y = gen_xy(score)
        heart_rate = None if random.random() < MISS_HR_RATE else gen_heart_rate(i)
        wind_speed = None if random.random() < MISS_WIND_RATE else round(wind, 1)
        wind_dir = None if wind_speed is None else random.randint(0, 359)
        shots.append({
            "scoreId": str(1963169497552654337 + global_idx * 10000 + i),
            "score": score,
            "hit": hit,
            "xMm": x,
            "yMm": y,
            "mcrT": gen_mcrt(week_no),
            "heartRate": heart_rate,
            "windSpeed": wind_speed,
            "windDirectionDeg": wind_dir,
            "shootingMode": 0 if i < 6 else 1,  # A9：每场前 6 支试射，其余记分
            "bowType": "反曲弓",                 # A9：基线键组成部分
            "shootingTimeUtc": iso_utc(ts_ms),
        })
        ts_ms += random.randint(40000, 60000)  # 修复1：箭间隔 40–60s
    return {
        "sessionId": f"S{global_idx + 1:03d}",
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
    print("== A12 mock 自检 ==")
    # 1) 单场时长
    for s in sessions:
        span = (
            datetime.fromisoformat(s["shots"][-1]["shootingTimeUtc"].replace("Z", "+00:00"))
            - datetime.fromisoformat(s["shots"][0]["shootingTimeUtc"].replace("Z", "+00:00"))
        ).total_seconds() * 1000
        ok = span >= MIN_SESSION_SPAN_MS
        assert ok, f"{s['sessionId']} 时长 {span/1000:.1f}min < 20min"
    print(f"1) 单场时长 ≥ 20 分钟：{len(sessions)} 场全部通过")

    # 2) 缺测箭
    miss_hr = sum(1 for s in sessions for sh in s["shots"] if sh["heartRate"] is None)
    miss_wind = sum(1 for s in sessions for sh in s["shots"] if sh["windSpeed"] is None)
    total = len(sessions) * SHOTS_PER_SESSION
    for s in sessions:
        valid = sum(1 for sh in s["shots"] if sh["heartRate"] is not None)
        assert valid >= 20, f"{s['sessionId']} 有效心率样本 {valid} < 20"
    print(f"2) 缺测：heartRate {miss_hr} 箭 ({miss_hr/total:.1%})、windSpeed {miss_wind} 箭 ({miss_wind/total:.1%})；每场有效样本 ≥ 20 通过")

    # 3) 跨日场次 S013
    s013 = next(s for s in sessions if s["sessionId"] == "S013")
    s013_dt = datetime.fromisoformat(s013["dateUtc"].replace("Z", "+00:00"))
    utc_day = s013_dt.day
    local_day = s013_dt.astimezone(LOCAL_TZ).day
    assert utc_day != local_day, "S013 本地日期应 ≠ UTC 日期"
    print(f"3) S013 跨日：UTC {utc_day} 日 22:00Z → 北京次日 {local_day} 日 06:00（本地日期 ≠ UTC 日期）通过")

    # 4) 周均环单调
    avgs = weekly_avg_scores(sessions)
    vals = list(avgs.values())
    assert all(vals[i] < vals[i + 1] for i in range(len(vals) - 1)), f"周均环不单调：{avgs}"
    print(f"4) 周均环单调上升：{avgs}")


def main():
    sessions = []
    g = 0
    for w in range(1, WEEKS + 1):
        for s in range(1, SESSIONS_PER_WEEK + 1):
            start = FIRST_SESSION_START.timestamp() * 1000 + g * SESSION_GAP_DAYS * 86400000
            sessions.append(gen_session(w, s, g, int(start)))
            g += 1
    # S013 跨日场次（第 13 场，W4 末尾）：2026-08-27T22:00:00Z → 北京 08-28 06:00 早训
    sessions.append(gen_session(4, 4, g, int(CROSS_DAY_SESSION_START.timestamp() * 1000), force_cross_day=True))

    data = {"athlete": ATHLETE, "sessions": sessions}
    out = OUT_DIR / "训练数据集_4周_张明.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    self_check(sessions)
    print(f"生成完成: {out}（{len(sessions)} 场 × {SHOTS_PER_SESSION} 箭）")


if __name__ == "__main__":
    main()
