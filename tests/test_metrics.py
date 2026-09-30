# -*- coding: utf-8 -*-
"""M4：指标层纯函数测试（已知输入 → 期望输出，含边界与缺测）。"""
import math

import pytest

from app.metrics.environment import wind_band_avg_scores, wind_band_counts, wind_band_of, wind_gap
from app.metrics.performance import avg_score, far_miss_rate, hit_rate, inner10_rate, total_score
from app.metrics.physiology import hr_volatility
from app.metrics.process import dispersion_mm, mean_mcr_t, offset_mm


class TestPerformance:
    def test_avg_score(self):
        assert avg_score([9, 10, 8.5]) == 9.166666666666666

    def test_avg_score_empty(self):
        assert avg_score([]) == 0.0

    def test_inner10_rate(self):
        # >=10.0 算内十
        assert inner10_rate([10.0, 9.9, 10.9]) == 66.66666666666666

    def test_far_miss_rate_default_threshold(self):
        assert far_miss_rate([8.9, 9.0, 9.5]) == 33.33333333333333  # <9.0 计远弹

    def test_far_miss_rate_empty(self):
        assert far_miss_rate([]) == 0.0

    def test_hit_rate(self):
        assert hit_rate([True, True, False]) == 66.66666666666666

    def test_total_score(self):
        assert total_score([9.1, 8.2, 10.0]) == 27.3


class TestProcess:
    def test_mean_mcr_t(self):
        assert mean_mcr_t([0.4, 0.5, 0.3]) == 0.4

    def test_dispersion_mm(self):
        # 两轴标准差平方均值开方（与 MVP 同口径）
        xs = [0.0, 10.0, 20.0]
        ys = [0.0, 10.0, 20.0]
        # pstdev([0,10,20]) = sqrt( ( (0-10)^2+(10-10)^2+(20-10)^2 )/3 ) = sqrt(200/3)
        pooled = math.sqrt(((math.sqrt(200 / 3)) ** 2 + (math.sqrt(200 / 3)) ** 2) / 2)
        assert dispersion_mm(xs, ys) == pytest.approx(pooled)

    def test_dispersion_insufficient(self):
        assert dispersion_mm([1.0], [2.0]) == 0.0  # 不足 2 箭 → 0（上层置 None）

    def test_offset_mm(self):
        assert offset_mm([0.0, 10.0], [5.0, 15.0]) == (5.0, 10.0)

    def test_offset_empty(self):
        assert offset_mm([], []) == (0.0, 0.0)


class TestPhysiology:
    def test_hr_volatility_rms(self):
        # 相邻差 [10, -10, 10] → RMS = sqrt((100+100+100)/3)
        assert hr_volatility([70, 80, 70, 80]) == pytest.approx(math.sqrt(100))

    def test_hr_volatility_insufficient(self):
        assert hr_volatility([70, 80]) == 0.0  # <3 个样本 → 0

    def test_hr_volatility_flat(self):
        assert hr_volatility([72, 72, 72]) == 0.0


class TestEnvironment:
    def test_wind_band_of_left_closed(self):
        assert wind_band_of(0.0) == 0
        assert wind_band_of(1.5) == 1  # 1.5 归 [1.5,2.0)
        assert wind_band_of(1.49) == 0
        assert wind_band_of(2.5) == 3  # [2.5, 99)

    def test_wind_band_of_out_of_range(self):
        with pytest.raises(ValueError):
            wind_band_of(99.5)

    def test_wind_band_avg_scores_skip_missing(self):
        shots = [
            {"wind_speed": 0.8, "score": 9.0},
            {"wind_speed": 1.8, "score": 8.0},
            {"wind_speed": None, "score": 10.0},  # 缺测不计入任何档
            {"wind_speed": 0.5, "score": 9.6},
        ]
        avg = wind_band_avg_scores(shots)
        assert avg[0] == pytest.approx(9.3)
        assert avg[1] == pytest.approx(8.0)
        assert 2 not in avg and 3 not in avg

    def test_wind_band_counts(self):
        shots = [{"wind_speed": 0.5, "score": 9.0}, {"wind_speed": 1.6, "score": 8.0}]
        assert wind_band_counts(shots) == {0: 1, 1: 1}

    def test_wind_gap(self):
        assert wind_gap(8.0, 9.5) == -1.5
