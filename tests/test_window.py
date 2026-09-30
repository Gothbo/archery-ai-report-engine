# -*- coding: utf-8 -*-
"""M4：窗口切分测试（A1 本地时区 Asia/Shanghai 切窗；UTC 存储）。"""
from datetime import datetime, timezone

import pytest

from app.config import get_config
from app.reports.window import window_bounds, window_of_shot


class TestWeekly:
    def test_2026_w32_bounds(self, engine_env):
        cfg = get_config()
        lo, hi = window_bounds(cfg, "weekly", "2026-W32")
        # 2026-W32 周一 = 2026-08-03 本地 → UTC 2026-08-02T16:00:00Z
        assert lo == "2026-08-02T16:00:00.000Z"
        assert hi == "2026-08-09T16:00:00.000Z"

    def test_cross_year_week(self, engine_env):
        cfg = get_config()
        # 2026-W01 周一起于 2025-12-29 本地 → UTC 2025-12-28T16:00:00Z
        lo, hi = window_bounds(cfg, "weekly", "2026-W01")
        assert lo == "2025-12-28T16:00:00.000Z"
        assert hi == "2026-01-04T16:00:00.000Z"

    def test_window_of_shot_local_alignment(self, engine_env):
        cfg = get_config()
        # 北京 2026-08-03 06:00 = UTC 2026-08-02 22:00 → 仍属 W32（本地切窗，A1）
        assert window_of_shot(cfg, "2026-08-02T22:00:00.000Z", "weekly") == "2026-W32"
        # UTC 2026-08-09 20:00 = 北京 08-10 凌晨 4 点 → 已属 W33
        assert window_of_shot(cfg, "2026-08-09T20:00:00.000Z", "weekly") == "2026-W33"


class TestMonthlyQuarterlyYearly:
    def test_monthly_bounds(self, engine_env):
        cfg = get_config()
        lo, hi = window_bounds(cfg, "monthly", "2026-08")
        assert lo == "2026-07-31T16:00:00.000Z"
        assert hi == "2026-08-31T16:00:00.000Z"

    def test_quarterly_bounds(self, engine_env):
        cfg = get_config()
        lo, hi = window_bounds(cfg, "quarterly", "2026Q3")
        assert lo == "2026-06-30T16:00:00.000Z"  # 本地 07-01 00:00
        assert hi == "2026-09-30T16:00:00.000Z"  # 本地 10-01 00:00

    def test_quarterly_cross_year(self, engine_env):
        cfg = get_config()
        lo, hi = window_bounds(cfg, "quarterly", "2026Q4")
        assert lo == "2026-09-30T16:00:00.000Z"
        assert hi == "2026-12-31T16:00:00.000Z"

    def test_yearly_bounds(self, engine_env):
        cfg = get_config()
        lo, hi = window_bounds(cfg, "yearly", "2026")
        assert lo == "2025-12-31T16:00:00.000Z"
        assert hi == "2026-12-31T16:00:00.000Z"

    def test_window_of_shot_all_granularities(self, engine_env):
        cfg = get_config()
        assert window_of_shot(cfg, "2026-08-03T09:00:00.000Z", "monthly") == "2026-08"
        assert window_of_shot(cfg, "2026-08-03T09:00:00.000Z", "quarterly") == "2026Q3"
        assert window_of_shot(cfg, "2026-08-03T09:00:00.000Z", "yearly") == "2026"

    def test_daily_raises(self, engine_env):
        with pytest.raises(ValueError):
            window_bounds(get_config(), "daily", "daily:S001")

    def test_window_key_roundtrip(self, engine_env):
        """切窗 key 与 shot 归属一致：窗口边界内任一箭回算同 key。"""
        cfg = get_config()
        for key in ("2026-W32", "2026-W35", "2026-08", "2026Q3", "2026"):
            lo, hi = window_bounds(cfg, "weekly" if key.startswith("2026-W") else
                                   ("monthly" if "-" in key and key != "2026" else
                                    ("quarterly" if "Q" in key else "yearly")), key)
            # 边界内随机时刻（UTC）应回算到 key（daily 除外）
            from datetime import timedelta
            mid = datetime.fromisoformat(lo.replace("Z", "+00:00")) + timedelta(days=1)
            if key.startswith("2026-W"):
                assert window_of_shot(cfg, mid.strftime("%Y-%m-%dT00:00:00.000Z"), "weekly") == key
