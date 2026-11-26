# -*- coding: utf-8 -*-
"""08 选择性记忆：提炼/候选/确认/拒绝/编辑/删除/召回。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.memory.notes import add_note
from app.rag.context import assemble_context
from app.reports.generator import generate_report
from tests.conftest import ATHLETE

BASE = f"/api/v1/athletes/{ATHLETE}"


@pytest.fixture()
def client(engine_env):
    return TestClient(app)


def _extract(client, text: str) -> dict:
    r = client.post(f"{BASE}/memories/extract",
                    json={"text": text, "actor_id": ATHLETE})
    assert r.status_code == 200, r.text
    return r.json()


def _notes(client, view: str = "athlete") -> list[dict]:
    r = client.get(f"{BASE}/notes", params={"view": view})
    assert r.status_code == 200, r.text
    return r.json()["notes"]


def _candidates(client) -> list[dict]:
    r = client.get(f"{BASE}/memory-candidates")
    assert r.status_code == 200, r.text
    return r.json()["candidates"]


class TestExtract:
    def test_normal_goal_is_saved_and_visible(self, client):
        result = _extract(client, "我的目标是打进省队前八")
        assert [n["note_type"] for n in result["saved"]] == ["goal"]
        assert result["candidates"] == []
        notes = _notes(client)
        assert any(n["content"] == "我的目标是打进省队前八" for n in notes)
        saved = next(n for n in notes if n["content"] == "我的目标是打进省队前八")
        assert saved["provenance"] == "conversation"
        assert saved["confirmed_at_utc"]

    def test_injury_stops_as_candidate_and_is_not_recalled(self, client):
        result = _extract(client, "我右肩膀疼，最近不敢加练")
        assert result["saved"] == []
        assert [n["note_type"] for n in result["candidates"]] == ["injury"]
        assert _notes(client) == []
        candidates = _candidates(client)
        assert candidates[0]["content"].startswith("我右肩膀疼")

    def test_equipment_is_saved_and_visible_to_athlete(self, client):
        result = _extract(client, "我换了 38 磅的弓片")
        assert [n["note_type"] for n in result["saved"]] == ["other"]
        assert any("弓片" in n["content"] for n in _notes(client))

    def test_does_not_store_full_chat_text(self, client):
        text = "今天天气不错，我看了会儿比赛。我的目标是打进省队前八。晚上吃什么好呢？"
        _extract(client, text)
        stored = " ".join(n["content"] + (n.get("quote_text") or "") for n in _notes(client))
        assert "省队前八" in stored
        assert "天气" not in stored
        assert "晚上吃什么" not in stored

    def test_repeated_extraction_does_not_duplicate(self, client):
        _extract(client, "我的目标是打进省队前八")
        _extract(client, "我的目标是打进省队前八")
        assert len([n for n in _notes(client) if n["content"] == "我的目标是打进省队前八"]) == 1


class TestCandidateLifecycle:
    def test_confirm_candidate_moves_it_into_memory(self, client):
        note_id = _extract(client, "右肩膀疼")["candidates"][0]["id"]
        r = client.post(f"{BASE}/memory-candidates/{note_id}/confirm")
        assert r.status_code == 200, r.text
        assert _candidates(client) == []
        assert any(n["id"] == note_id for n in _notes(client))

    def test_reject_candidate_keeps_it_out_of_memory(self, client):
        note_id = _extract(client, "右肩膀疼")["candidates"][0]["id"]
        r = client.delete(f"{BASE}/memory-candidates/{note_id}")
        assert r.status_code == 200, r.text
        assert _candidates(client) == []
        assert _notes(client) == []

    def test_confirm_unknown_candidate_returns_404(self, client):
        assert client.post(f"{BASE}/memory-candidates/999999/confirm").status_code == 404


class TestMemoryEditing:
    def test_edit_and_delete_saved_memory(self, client):
        note_id = _extract(client, "我的目标是打进省队前八")["saved"][0]["id"]
        r = client.patch(f"{BASE}/memory-notes/{note_id}", json={"content": "我的目标是打进省队前四"})
        assert r.status_code == 200, r.text
        assert r.json()["content"] == "我的目标是打进省队前四"
        assert any("前四" in n["content"] for n in _notes(client))

        assert client.delete(f"{BASE}/memory-notes/{note_id}").status_code == 200
        assert _notes(client) == []

    def test_cannot_edit_another_athletes_memory(self, client):
        note_id = _extract(client, "我的目标是打进省队前八")["saved"][0]["id"]
        r = client.patch(f"/api/v1/athletes/OTHER/memory-notes/{note_id}",
                         json={"content": "被改掉"})
        assert r.status_code == 404


class TestRecall:
    def _report(self, db):
        db.upsert_profile({"athlete_id": ATHLETE, "name": "张明"})
        generate_report(db, ATHLETE, "weekly", "2026-W32", view="athlete", force=True)

    def test_profile_memory_is_always_injected(self, db):
        self._report(db)
        add_note(db, ATHLETE, "goal", "目标打进省队前八", "athlete", ATHLETE)
        ctx = assemble_context(db, ATHLETE, "这周风大吗？", view="athlete")
        assert "省队前八" in ctx["skeleton"]

    def test_unconfirmed_candidate_is_never_injected(self, db):
        self._report(db)
        add_note(db, ATHLETE, "injury", "右肩膀疼", "athlete", ATHLETE,
                 provenance="conversation", confirmed=False)
        ctx = assemble_context(db, ATHLETE, "肩膀还疼吗？", view="athlete")
        assert "肩膀疼" not in ctx["skeleton"]

    def test_relevant_topic_recalls_conversation_detail_only(self, db):
        self._report(db)
        add_note(db, ATHLETE, "injury", "上次说肩膀疼", "athlete", ATHLETE,
                 provenance="conversation", confirmed=True)
        related = assemble_context(db, ATHLETE, "肩膀现在怎么样？", view="athlete")
        unrelated = assemble_context(db, ATHLETE, "这周风大吗？", view="athlete")
        assert "上次说肩膀疼" in related["skeleton"]
        assert "上次说肩膀疼" not in unrelated["skeleton"]

    def test_conversation_goal_and_equipment_are_always_injected(self, db):
        self._report(db)
        add_note(db, ATHLETE, "goal", "目标打进省队前八", "athlete", ATHLETE,
                 provenance="conversation", confirmed=True)
        add_note(db, ATHLETE, "other", "换了 38 磅弓片", "athlete", ATHLETE,
                 provenance="conversation", confirmed=True)
        ctx = assemble_context(db, ATHLETE, "这周风大吗？", view="athlete")
        assert "目标打进省队前八" in ctx["skeleton"]
        assert "换了 38 磅弓片" in ctx["skeleton"]
