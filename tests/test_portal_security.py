# -*- coding: utf-8 -*-
"""PR #4：同源托管门户（/portal/）+ 跨站写请求拦截。"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


def _patch_config(tmp_path: Path, **sections) -> None:
    import app.config as cfgmod
    p = tmp_path / "config.json"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg.update(sections)
    p.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    cfgmod.get_config.cache_clear()


@pytest.fixture()
def client(engine_env):
    return TestClient(app)


@pytest.fixture()
def dist(tmp_path):
    d = tmp_path / "portal_dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text(
        '<!doctype html><html><head><script src="./assets/portal.js"></script></head>'
        '<body><div id="root"></div></body></html>', encoding="utf-8")
    (d / "assets" / "portal.js").write_text("console.log('portal');", encoding="utf-8")
    return d


class TestPortalHosting:
    def test_not_configured_404(self, client):
        r = client.get("/portal/")
        assert r.status_code == 404
        assert r.json()["code"] == "PORTAL-NOT-CONFIGURED"

    def test_missing_index_404(self, client, tmp_path):
        (tmp_path / "empty").mkdir()
        _patch_config(tmp_path, portal={"dist_dir": str(tmp_path / "empty")})
        r = client.get("/portal/")
        assert r.status_code == 404 and r.json()["code"] == "PORTAL-NOT-CONFIGURED"

    def test_serves_index_and_assets(self, client, tmp_path, dist):
        _patch_config(tmp_path, portal={"dist_dir": str(dist)})
        r = client.get("/portal/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert './assets/portal.js' in r.text
        r2 = client.get("/portal/index.html")
        assert r2.status_code == 200
        js = client.get("/portal/assets/portal.js")
        assert js.status_code == 200 and "portal" in js.text
        assert client.get("/portal/assets/nope.js").status_code == 404

    def test_redirect_without_slash(self, client, tmp_path, dist):
        _patch_config(tmp_path, portal={"dist_dir": str(dist)})
        r = client.get("/portal", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/portal/"

    def test_no_path_traversal(self, client, tmp_path, dist):
        (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
        _patch_config(tmp_path, portal={"dist_dir": str(dist)})
        r = client.get("/portal/..%2Fsecret.txt")
        assert r.status_code == 404
        assert "secret" not in r.text

    def test_no_cors_headers(self, client, tmp_path, dist):
        _patch_config(tmp_path, portal={"dist_dir": str(dist)})
        r = client.get("/api/v1/health", headers={"Origin": "null"})
        assert "access-control-allow-origin" not in {k.lower() for k in r.headers}

    def test_api_same_origin_from_portal(self, client, tmp_path, dist):
        """门户页面同源调用写接口（浏览器会带 Origin=本站）→ 放行。"""
        _patch_config(tmp_path, portal={"dist_dir": str(dist)})
        r = client.post("/api/v1/ingest/mock",
                        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200


class TestCrossSiteGuard:
    def test_cross_site_bodyless_post_blocked(self, client):
        r = client.post("/api/v1/ingest/mock", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
        assert r.json()["code"] == "CROSS-SITE-BLOCKED"
        # 没有执行：库里没有箭
        from app.store.database import get_database
        assert get_database().query("SELECT COUNT(*) c FROM shot_fact")[0]["c"] == 0

    def test_null_origin_blocked(self, client):
        r = client.post("/api/v1/ingest/mock", headers={"Origin": "null"})
        assert r.status_code == 403

    def test_sec_fetch_site_cross_site_without_origin_blocked(self, client):
        r = client.post("/api/v1/ingest/mock", headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403

    def test_delete_and_refresh_blocked(self, client):
        client.post("/api/v1/ingest/mock")
        r = client.post("/api/v1/athletes/1963169497552654337/reports/daily",
                        params={"session_id": "S001", "refresh": 1},
                        headers={"Origin": "http://127.0.0.1:5173"})
        assert r.status_code == 403
        r2 = client.delete("/api/v1/athletes/1963169497552654337/notes/1",
                           headers={"Origin": "https://evil.example"})
        assert r2.status_code == 403

    def test_no_origin_local_tool_allowed(self, client):
        assert client.post("/api/v1/ingest/mock").status_code == 200

    def test_get_not_affected(self, client):
        r = client.get("/api/v1/health", headers={"Origin": "https://evil.example"})
        assert r.status_code == 200

    def test_same_origin_allowed_with_port(self, engine_env):
        c = TestClient(app, base_url="http://127.0.0.1:8000")
        r = c.post("/api/v1/ingest/mock", headers={"Origin": "http://127.0.0.1:8000"})
        assert r.status_code == 200
        r2 = c.post("/api/v1/ingest/mock", headers={"Origin": "http://127.0.0.1:8001"})
        assert r2.status_code == 403

    def test_allowlisted_origin(self, client, tmp_path):
        _patch_config(tmp_path, security={"allowed_origins": ["https://portal.suooter.example/"]})
        r = client.post("/api/v1/ingest/mock", headers={"Origin": "https://portal.suooter.example"})
        assert r.status_code == 200
        assert client.post("/api/v1/ingest/mock", headers={"Origin": "null"}).status_code == 403

    def test_config_rejects_null_and_wildcard(self, tmp_path):
        from app.config import SecurityConfig
        for bad in ("null", "*"):
            with pytest.raises(ValueError):
                SecurityConfig(allowed_origins=[bad])

    def test_switch_off(self, client, tmp_path):
        _patch_config(tmp_path, security={"block_cross_site_writes": False})
        r = client.post("/api/v1/ingest/mock", headers={"Origin": "https://evil.example"})
        assert r.status_code == 200

    def test_json_post_from_cross_site_blocked(self, client):
        r = client.post("/api/v1/athletes/1963169497552654337/notes",
                        json={"note_type": "goal", "content": "x", "author_role": "coach", "actor_id": "1"},
                        headers={"Origin": "https://evil.example"})
        assert r.status_code == 403
