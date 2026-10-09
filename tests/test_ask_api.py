# -*- coding: utf-8 -*-
"""B1-7：ask 端点测试（关=降级 / 开=mock 走通 / G1/G2/G3 回退 / suggested_note 只读）。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.memory.notes import add_note
from app.store.database import get_database
from tests.conftest import ATHLETE, seed_anchor


@pytest.fixture()
def client(engine_env, monkeypatch):
    return TestClient(app)


def _ingest_and_report(client, *, week="2026-W33", anchor=True):
    """导入 mock 数据集 + 建锚点 + 生成周报告，返回 (client)。"""
    client.post("/api/v1/ingest/mock")
    if anchor:
        db = get_database()
        sid = db.query("SELECT session_id FROM session_dim ORDER BY session_time_utc LIMIT 1")[0]["session_id"]
        seed_anchor(db, ATHLETE, sid)
    r = client.post(f"/api/v1/athletes/{ATHLETE}/reports/weekly",
                    params={"week": week, "view": "coach", "refresh": "1"})
    assert r.status_code == 200
    return client


class TestHealthExposesLlm:
    def test_health_llm_enabled(self, client):
        body = client.get("/api/v1/health").json()
        assert "llm_enabled" in body
        assert body["llm_enabled"] is False  # 测试配置默认关闭


class TestAskOff:
    def test_off_returns_degraded(self, client):
        _ingest_and_report(client)
        r = client.post(f"/api/v1/athletes/{ATHLETE}/ask",
                        json={"question": "这周为什么进步了？", "view": "coach"})
        assert r.status_code == 200
        body = r.json()
        assert body["degraded"] is True
        assert "未开启" in body["answer"]
        assert body["sources"]  # 溯源仍返回
        assert body["context_version"]

    def test_unknown_athlete_400(self, client):
        r = client.post("/api/v1/athletes/NOPE/ask", json={"question": "你好"})
        assert r.status_code == 400

    def test_no_report_yet_400(self, client):
        client.post("/api/v1/ingest/mock")  # 建档但未生成报告
        r = client.post(f"/api/v1/athletes/{ATHLETE}/ask", json={"question": "你好"})
        assert r.status_code == 400
        assert "暂无已生成报告" in r.json()["detail"]

    def test_question_too_long_422(self, client):
        _ingest_and_report(client)
        r = client.post(f"/api/v1/athletes/{ATHLETE}/ask",
                        json={"question": "问" * 201, "view": "coach"})
        assert r.status_code == 422


class TestAskMockOn:
    @pytest.fixture()
    def llm_env(self, tmp_path, monkeypatch):
        """开启 llm.enabled + provider=mock（演示/测试用，G2 恒通过）。"""
        import app.config as cfgmod
        import app.store.database as dbmod
        import tests.conftest as ct
        cfg = ct._make_temp_config(tmp_path)
        cfg["llm"] = {"enabled": True, "provider": "mock", "model": "mock",
                      "endpoint": "http://127.0.0.1:1"}
        (tmp_path / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        monkeypatch.setenv("ENGINE_CONFIG", str(tmp_path / "config.json"))
        monkeypatch.setattr(cfgmod, "CONFIG_PATH", tmp_path / "config.json")
        cfgmod.get_config.cache_clear()
        dbmod._SINGLETON = None
        yield cfg
        cfgmod.get_config.cache_clear()
        dbmod._SINGLETON = None

    @pytest.fixture()
    def mclient(self, llm_env):
        return TestClient(app)

    def test_on_answers_from_skeleton(self, mclient):
        _ingest_and_report(mclient)
        r = mclient.post(f"/api/v1/athletes/{ATHLETE}/ask",
                         json={"question": "这周为什么进步了？", "view": "coach"})
        assert r.status_code == 200
        body = r.json()
        assert body["degraded"] is False
        assert body["answer"]
        # mock 应答器数字全部来自骨架 → 无骨架外数字
        assert "0.35" not in body["answer"]  # 1.5B 编造教训：任何外来数字都不应出现
        assert body["sources"]
        assert body["window_key"] == "2026-W33"

    def test_g2_fallback_on_fabricated_number(self, mclient, monkeypatch):
        """G2 反向用例：输出出现骨架外数字 → 回退降级模板（对运动员的数字承诺）。"""
        _ingest_and_report(mclient)
        from app.llm.client import MockLLMClient

        def fake_factory(cfg):
            return MockLLMClient(responses=["本周平均环 99.9 环，进步 5.0 环"])

        import app.api.ask as askmod
        monkeypatch.setattr(askmod, "get_llm_client", fake_factory)
        r = mclient.post(f"/api/v1/athletes/{ATHLETE}/ask",
                         json={"question": "这周怎么样？", "view": "coach"})
        assert r.status_code == 200
        body = r.json()
        assert body["degraded"] is True
        assert "G2" in body["reason"]
        assert body["fallback_skeleton"]  # 回退到报告原文

    def test_numbered_list_not_g2_false_positive(self, mclient, monkeypatch):
        """行首列表序号不是数据：编号列表不得触发 G2 误降级（真模型实测教训）。"""
        _ingest_and_report(mclient)
        from app.llm.client import MockLLMClient

        def fake_factory(cfg):
            return MockLLMClient(responses=["1. 技术更稳定\n2. 心态更专注\n3. 节奏更干脆"])

        import app.api.ask as askmod
        monkeypatch.setattr(askmod, "get_llm_client", fake_factory)
        r = mclient.post(f"/api/v1/athletes/{ATHLETE}/ask",
                         json={"question": "这周为什么进步了？", "view": "coach"})
        assert r.status_code == 200
        body = r.json()
        assert body["degraded"] is False
        assert "1. 技术更稳定" in body["answer"]

    def test_g1_forbidden_fallback(self, mclient, monkeypatch):
        _ingest_and_report(mclient)
        from app.llm.client import MockLLMClient

        def fake_factory(cfg):
            return MockLLMClient(responses=["只要练它即提分，均环没问题"])

        import app.api.ask as askmod
        monkeypatch.setattr(askmod, "get_llm_client", fake_factory)
        r = mclient.post(f"/api/v1/athletes/{ATHLETE}/ask",
                         json={"question": "怎么练？", "view": "coach"})
        assert r.status_code == 200
        assert r.json()["degraded"] is True
        assert "G1" in r.json()["reason"]

    def test_llm_failure_fallback(self, mclient, monkeypatch):
        _ingest_and_report(mclient)
        from app.llm.client import LLMError

        def fake_factory(cfg):
            raise LLMError("连接失败")

        import app.api.ask as askmod
        monkeypatch.setattr(askmod, "get_llm_client", fake_factory)
        r = mclient.post(f"/api/v1/athletes/{ATHLETE}/ask",
                         json={"question": "这周怎么样？", "view": "coach"})
        assert r.status_code == 200
        assert r.json()["degraded"] is True
        assert "不可用" in r.json()["answer"]

    def test_suggested_note_readonly(self, mclient):
        _ingest_and_report(mclient)
        db = get_database()
        add_note(db, ATHLETE, "goal", "冲击 9.8 均环", "athlete", ATHLETE)
        r = mclient.post(f"/api/v1/athletes/{ATHLETE}/ask",
                         json={"question": "怎么调整训练目标？", "view": "coach"})
        assert r.status_code == 200
        body = r.json()
        assert body["suggested_note"] is not None
        assert body["suggested_note"]["readonly"] is True  # D4：MVP 只展示不落库
