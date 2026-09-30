# -*- coding: utf-8 -*-
"""M1 验收：/api/v1/health 返回 200 且口径字段齐全。"""
from fastapi.testclient import TestClient

from app.main import app


def test_health_ok():
    client = TestClient(app)
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["config_version"]
    assert body["timezone"] == "Asia/Shanghai"
    assert isinstance(body["db"], str) and body["db"]  # 真实配置 → 引擎库路径（惰性自初始化）
    assert body["mdc_source"] == "empty"  # 真实配置 mdc_source=null（M4.5 共识前降级）
