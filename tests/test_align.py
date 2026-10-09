# -*- coding: utf-8 -*-
"""M4：对齐模块测试（透传 + 窗口最近匹配 + 缺测置 NULL）。"""
from app.ingest.align import (
    HR_WINDOW_MS,
    WIND_WINDOW_MS,
    _nearest_int,
    _nearest_sample,
    _nearest_wind,
    _parse_utc_ms,
    align_session,
)
from app.ingest.base import SessionRaw, ShotRaw


def _mk_session(shots: list[ShotRaw]) -> SessionRaw:
    return SessionRaw(session_id="S001", athlete_id="1963169497552654337", shots=shots, distance_m=70)


def _shot(score: float, **kw) -> ShotRaw:
    base = dict(athlete_id="1963169497552654337", session_id="S001", shot_seq=1,
                score=score, hit=True, shooting_mode=1, bow_type="反曲弓",
                shot_time_utc="2026-08-03T09:00:00.000Z")
    base.update(kw)
    return ShotRaw(**base)


class TestParseUtcMs:
    def test_parse_iso_z(self):
        # 2026-08-03T09:00:00.123Z = 1785747600123 ms（UTC epoch）
        assert _parse_utc_ms("2026-08-03T09:00:00.123Z") == 1785747600123

    def test_parse_naive_as_utc(self):
        assert _parse_utc_ms("2026-08-03T09:00:00.123") == 1785747600123

    def test_parse_invalid(self):
        assert _parse_utc_ms("not-a-time") is None


class TestAlignPassthrough:
    def test_mock_passthrough(self):
        s = _mk_session([_shot(9.6, mcr_t=0.45, hr=80, wind_speed=1.2, wind_dir_deg=90.0)])
        dim, rows = align_session(s)  # hr_samples/wind_samples=None → 透传
        assert rows[0]["hr"] == 80
        assert rows[0]["wind_speed"] == 1.2
        assert rows[0]["mcr_t"] == 0.45
        assert dim["shot_count"] == 1
        assert dim["mode_composition"] == "记分1"

    def test_missing_fields_null(self):
        s = _mk_session([_shot(9.0, mcr_t=None, hr=None, wind_speed=None)])
        dim, rows = align_session(s)
        assert rows[0]["hr"] is None
        assert rows[0]["wind_speed"] is None
        assert rows[0]["mcr_t"] is None
        assert dim["avg_wind"] is None  # 无风数据 → avg_wind None
        assert rows[0]["score"] == 9.0


class TestWindowMatching:
    def test_hr_nearest_within_window(self):
        # 箭在 09:00:00.000，HR 采样 09:00:05 与 09:01:30 → 取 5s 那条
        s = _mk_session([_shot(9.6)])
        hr_samples = {"1963169497552654337": [(1785747605000, 82), (1785747690000, 90)]}
        _, rows = align_session(s, hr_samples=hr_samples, wind_samples=None)
        assert rows[0]["hr"] == 82

    def test_hr_outside_window_null(self):
        s = _mk_session([_shot(9.6)])
        hr_samples = {"1963169497552654337": [(1785747630000 + HR_WINDOW_MS + 1, 82)]}
        _, rows = align_session(s, hr_samples=hr_samples, wind_samples=None)
        assert rows[0]["hr"] is None

    def test_wind_matches_speed_and_dir(self):
        s = _mk_session([_shot(9.6)])
        wind_samples = {"1963169497552654337": [(1785747605000, 1.8, 194.6)]}
        _, rows = align_session(s, hr_samples=None, wind_samples=wind_samples)
        assert rows[0]["wind_speed"] == 1.8
        assert rows[0]["wind_dir_deg"] == 194.6

    def test_wind_outside_window_null(self):
        s = _mk_session([_shot(9.6)])
        wind_samples = {"1963169497552654337": [(1785747630000 + WIND_WINDOW_MS + 1, 1.8, 194.6)]}
        _, rows = align_session(s, hr_samples=None, wind_samples=wind_samples)
        assert rows[0]["wind_speed"] is None

    def test_nearest_helpers(self):
        assert _nearest_int(1000, [(900, 5), (1500, 9)], 1000) == 5
        assert _nearest_int(1000, [(2000, 9)], 500) is None
        assert _nearest_sample(1000, [(900, 1.5)], 500) == 1.5
        assert _nearest_wind(1000, [(900, 1.5, 45.0)], 500) == (1.5, 45.0)
        assert _nearest_wind(1000, [(900, 1.5, 45.0)], 50) is None
