# -*- coding: utf-8 -*-
"""PR #4：GET /api/v1/ingest/v12/last-received + 索引。"""
import copy
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store.database import get_database
from tests.test_v12_ingest import URL, _examples, make_dt2

LR = "/api/v1/ingest/v12/last-received"
Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture()
def client(engine_env):
    return TestClient(app)


def test_empty_all_null(client):
    body = client.get(LR).json()
    assert {k: body[k] for k in ("dt1", "dt2", "dt3", "dt4", "dt5", "dt6", "dt7")} == dict.fromkeys(
        ("dt1", "dt2", "dt3", "dt4", "dt5", "dt6", "dt7"))
    assert body["source"] == "v12" and Z_RE.match(body["as_of_utc"])
    assert set(body) == {"dt1", "dt2", "dt3", "dt4", "dt5", "dt6", "dt7", "source", "as_of_utc"}


def test_after_ingest_per_type(client):
    msgs = [m for m in _examples() if m["dataType"] in (1, 2, 4)]
    r = client.post(URL, json=msgs).json()
    accepted = {x["dataType"] for x in r["results"] if x["status"] == "accepted"}
    assert {1, 2, 4} <= accepted
    body = client.get(LR).json()
    for dt in (1, 2, 4):
        assert Z_RE.match(body[f"dt{dt}"]), body
        row = get_database().query("SELECT MAX(received_at_utc) t FROM ingest_messages WHERE data_type=?", (dt,))
        assert body[f"dt{dt}"] == row[0]["t"]
    assert body["dt3"] is None and body["dt7"] is None  # 没收到 → null
    assert body["dt2"] <= body["as_of_utc"]


def test_duplicate_and_invalid_do_not_update(client):
    m = make_dt2(1)
    client.post(URL, json=m)
    t1 = client.get(LR).json()["dt2"]
    dup = client.post(URL, json=m).json()
    assert dup["results"][0]["status"] == "duplicate"
    bad = copy.deepcopy(make_dt2(2))
    bad["data"]["score"] = "x"
    assert client.post(URL, json=bad).json()["results"][0]["status"] == "invalid"
    assert client.get(LR).json()["dt2"] == t1
    get_database().conn.execute("UPDATE ingest_messages SET received_at_utc='2020-01-01T00:00:00.000Z'")
    get_database().conn.commit()
    client.post(URL, json=make_dt2(3))
    assert client.get(LR).json()["dt2"] > "2020-01-01T00:00:00.000Z"  # 新受理的会更新


def test_no_identity_fields(client):
    client.post(URL, json=make_dt2(1))
    body = client.get(LR).json()
    text = str(body)
    assert "spid" not in text and "lane" not in text and "athlete" not in text


def test_index_used(client):
    db = get_database()
    idx = {r["name"] for r in db.query("PRAGMA index_list(ingest_messages)")}
    assert "idx_ingest_dt_time" in idx
    plan = " ".join(str(tuple(r)) for r in db.query(
        "EXPLAIN QUERY PLAN SELECT MAX(received_at_utc) FROM ingest_messages WHERE data_type=?", (1,)))
    assert "idx_ingest_dt_time" in plan
