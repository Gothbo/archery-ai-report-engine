# -*- coding: utf-8 -*-
"""Mock JSON 数据源：读取 eng/mock_data 训练数据集。

mock 数据已自带逐箭心率/风速，对齐模块对 mock 直接透传（D2）。
A9 已补：shootingMode/bowType 字段（mock 生成器已补，缺失时按默认值回填）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from app.ingest.base import SessionRaw, ShotRaw, Source

DEFAULT_MOCK_PATH = Path(__file__).resolve().parent.parent.parent / "mock_data" / "训练数据集_4周_张明.json"


class MockSource(Source):
    def __init__(self, path: str | Path = DEFAULT_MOCK_PATH):
        self.path = Path(path)

    def athlete_profiles(self) -> dict[str, dict]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        a = data["athlete"]
        return {a["athleteId"]: {"name": a.get("name"), "gender": a.get("gender"),
                                 "age": a.get("age"), "level": a.get("level"),
                                 "bow_type": a.get("archeryType") or "反曲弓"}}

    def iter_sessions(self) -> Iterator[SessionRaw]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        athlete = data["athlete"]
        athlete_id = athlete["athleteId"]
        bow_type = athlete.get("archeryType") or "反曲弓"
        for s in data["sessions"]:
            shots: list[ShotRaw] = []
            for i, sh in enumerate(s["shots"]):
                shots.append(
                    ShotRaw(
                        athlete_id=athlete_id,
                        session_id=s["sessionId"],
                        shot_seq=i + 1,
                        score=float(sh["score"]),
                        hit=bool(sh.get("hit", True)),
                        x_mm=_num(sh.get("xMm")),
                        y_mm=_num(sh.get("yMm")),
                        mcr_t=_num(sh.get("mcrT")),
                        hr=_int_or_none(sh.get("heartRate")),
                        wind_speed=_num(sh.get("windSpeed")),
                        wind_dir_deg=_num(sh.get("windDirectionDeg")),
                        shooting_mode=int(sh.get("shootingMode", 1)),
                        bow_type=str(sh.get("bowType", bow_type)),
                        video_ref=None,
                        shot_time_utc=sh["shootingTimeUtc"],
                    )
                )
            yield SessionRaw(
                session_id=s["sessionId"],
                athlete_id=athlete_id,
                shots=shots,
                distance_m=int(s.get("distanceM", 70)),
                site=s.get("site"),
            )


def _num(v):
    return None if v is None else float(v)


def _int_or_none(v):
    return None if v is None else int(v)
