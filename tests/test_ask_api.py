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

    def test_mock_wording_matches_granularity(self, mclient):
        """真引擎联调 #3：mock 应答器的日报回答不能写「本周平均环」。"""
        _ingest_and_report(mclient)
        sid = get_database().query("SELECT session_id FROM session_dim ORDER BY session_time_utc DESC "
                                   "LIMIT 1")[0]["session_id"]
        assert mclient.post(f"/api/v1/athletes/{ATHLETE}/reports/daily",
                            params={"session_id": sid, "view": "coach"}).status_code == 200
        daily = mclient.post(f"/api/v1/athletes/{ATHLETE}/ask", json={
            "question": "这场怎么样？", "view": "coach", "granularity": "daily", "window_key": f"daily:{sid}"}).json()
        weekly = mclient.post(f"/api/v1/athletes/{ATHLETE}/ask", json={
            "question": "这周怎么样？", "view": "coach", "granularity": "weekly", "window_key": "2026-W33"}).json()
        assert daily["degraded"] is False and weekly["degraded"] is False
        assert "本场平均环" in daily["answer"] and "本周" not in daily["answer"]
        assert "本周平均环" in weekly["answer"]

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


class TestAskMultiTurn:
    @pytest.fixture()
    def llm_env(self, tmp_path, monkeypatch):
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

    def test_second_ask_includes_prior_turn(self, mclient, monkeypatch):
        """同一 athlete+view 的第二次提问应带上第一轮，并写入同一线程。"""
        _ingest_and_report(mclient)
        captured: list[list[dict]] = []

        def factory(cfg):
            from app.llm.client import MockLLMClient

            def responder(messages):
                captured.append(messages)
                return "请看本窗口报告结论。"

            return MockLLMClient(responder=responder)

        import app.api.ask as askmod
        monkeypatch.setattr(askmod, "get_llm_client", factory)

        r1 = mclient.post(f"/api/v1/athletes/{ATHLETE}/ask",
                          json={"question": "这周怎么样？", "view": "coach"})
        assert r1.status_code == 200
        body1 = r1.json()
        assert body1["degraded"] is False
        assert body1["conversation_id"]

        r2 = mclient.post(f"/api/v1/athletes/{ATHLETE}/ask",
                          json={"question": "能再说一遍吗？", "view": "coach"})
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2["conversation_id"] == body1["conversation_id"]
        assert len(captured) == 2
        prior_text = "\n".join(m["content"] for m in captured[1])
        assert "这周怎么样？" in prior_text
        assert "请看本窗口报告结论。" in prior_text
        assert captured[1][-1] == {"role": "user", "content": "能再说一遍吗？"}
        assert captured[1][0]["role"] == "system"

    def test_get_conversation_survives_new_client(self, mclient, monkeypatch):
        _ingest_and_report(mclient)
        from app.llm.client import MockLLMClient
        mock = MockLLMClient(responses=["请看本窗口报告结论。", "可以继续问。"])

        import app.api.ask as askmod
        monkeypatch.setattr(askmod, "get_llm_client", lambda cfg: mock)
        mclient.post(f"/api/v1/athletes/{ATHLETE}/ask",
                     json={"question": "这周怎么样？", "view": "coach"})
        mclient.post(f"/api/v1/athletes/{ATHLETE}/ask",
                     json={"question": "还有补充吗？", "view": "coach"})

        other = TestClient(app)
        g = other.get(f"/api/v1/athletes/{ATHLETE}/conversation", params={"view": "coach"})
        assert g.status_code == 200
        body = g.json()
        assert body["conversation_id"]
        roles = [(m["role"], m["content"]) for m in body["messages"]]
        assert roles == [
            ("user", "这周怎么样？"),
            ("assistant", "请看本窗口报告结论。"),
            ("user", "还有补充吗？"),
            ("assistant", "可以继续问。"),
        ]

        empty = other.get(f"/api/v1/athletes/{ATHLETE}/conversation", params={"view": "athlete"})
        assert empty.status_code == 200
        assert empty.json()["messages"] == []
        assert empty.json()["conversation_id"] is None

    def test_unknown_athlete_conversation_400(self, mclient):
        r = mclient.get("/api/v1/athletes/NOPE/conversation")
        assert r.status_code == 400

    def test_old_note_in_ask_not_in_notes_panel(self, mclient):
        """超过 14 天的 active 备注进入 ask 上下文，但不出现在备注面板。"""
        _ingest_and_report(mclient)
        db = get_database()
        db.insert_note({
            "athlete_id": ATHLETE,
            "note_type": "goal",
            "content": "旧目标守住 9.50 均环",
            "author_role": "athlete",
            "actor_id": ATHLETE,
            "created_at_utc": "2026-01-01T00:00:00.000Z",
        })
        listed = mclient.get(f"/api/v1/athletes/{ATHLETE}/notes", params={"view": "coach"}).json()
        assert all("旧目标守住 9.50" not in (n.get("content") or "") for n in listed["notes"])

        r = mclient.post(f"/api/v1/athletes/{ATHLETE}/ask",
                         json={"question": "目标还记得吗？", "view": "coach"})
        assert r.status_code == 200
        body = r.json()
        assert any("旧目标守住 9.50" in (s.get("summary") or "") for s in body["sources"])

    def test_history_number_not_false_g2(self, mclient, monkeypatch):
        """用户上一轮提到的数字应并入允许集合，跟问时不得被 G2 误降级。"""
        _ingest_and_report(mclient)
        from app.llm.client import MockLLMClient
        replies = iter(["请看本窗口报告结论。", "你提到的 8.88 并不在本窗口骨架里。"])

        def factory(cfg):
            return MockLLMClient(responder=lambda _msgs: next(replies))

        import app.api.ask as askmod
        monkeypatch.setattr(askmod, "get_llm_client", factory)
        r1 = mclient.post(f"/api/v1/athletes/{ATHLETE}/ask",
                          json={"question": "我记得有个 8.88 环", "view": "coach"})
        assert r1.status_code == 200
        assert r1.json()["degraded"] is False
        r2 = mclient.post(f"/api/v1/athletes/{ATHLETE}/ask",
                          json={"question": "刚才那个 8.88 是怎么来的", "view": "coach"})
        assert r2.status_code == 200
        body = r2.json()
        assert body["degraded"] is False
        assert "8.88" in body["answer"]

    def test_confirm_note_writeback_conversation(self, mclient):
        """建议记录需确认后才落库；带 provenance=conversation。"""
        _ingest_and_report(mclient)
        add_note(get_database(), ATHLETE, "goal", "冲击 9.8 均环", "athlete", ATHLETE)
        r = mclient.post(f"/api/v1/athletes/{ATHLETE}/ask",
                         json={"question": "怎么调整训练目标？", "view": "coach"})
        sn = r.json()["suggested_note"]
        assert sn["readonly"] is True
        posted = mclient.post(f"/api/v1/athletes/{ATHLETE}/notes", json={
            "note_type": sn["note_type"],
            "content": sn["content"],
            "author_role": "coach",
            "actor_id": ATHLETE,
            "provenance": "conversation",
            "quote_text": "怎么调整训练目标？",
        })
        assert posted.status_code == 200
        notes = mclient.get(f"/api/v1/athletes/{ATHLETE}/notes", params={"view": "coach"}).json()["notes"]
        written = [n for n in notes if n["id"] == posted.json()["note_id"]][0]
        assert written["provenance"] == "conversation"
        assert written["quote_text"] == "怎么调整训练目标？"
        assert written["confirmed_at_utc"]
