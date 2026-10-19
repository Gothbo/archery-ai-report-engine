# -*- coding: utf-8 -*-
"""PR #4：固化备注 actor_id 的现有语义（docs/portal_api_contract.md §8，本 PR 不改行为）。"""
import pytest
from fastapi.testclient import TestClient

from app.main import app

URL = "/api/v1/athletes/A1/notes"


@pytest.fixture()
def client(engine_env):
    return TestClient(app)


def _body(**over):
    return {"note_type": "goal", "content": "冲击 9.8", "author_role": "athlete", "actor_id": "1963000000000000001", **over}


def test_actor_id_required(client):
    body = _body()
    body.pop("actor_id")
    assert client.post(URL, json=body).status_code == 422


def test_actor_id_any_string_stored_and_returned(client):
    for actor in ("1963000000000000001", "host-user-7", ""):  # 不校验格式（空串也收）
        r = client.post(URL, json=_body(actor_id=actor))
        assert r.status_code == 200
    notes = client.get(URL, params={"view": "coach"}).json()["notes"]
    assert sorted(n["actor_id"] for n in notes) == sorted(["1963000000000000001", "host-user-7", ""])


def test_delete_checks_athlete_not_actor(client):
    nid = client.post(URL, json=_body(actor_id="author-1")).json()["note_id"]
    # 删除不带、也不校验录入人：只要备注属于路径里的运动员就能删
    assert client.delete(f"/api/v1/athletes/OTHER/notes/{nid}").status_code == 404
    assert client.delete(f"{URL}/{nid}").status_code == 200
