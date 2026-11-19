# -*- coding: utf-8 -*-
"""V10 桌面端壳：/desktop/ 静态托管与重定向冒烟。"""
from fastapi.testclient import TestClient

from app.main import app


def test_desktop_shell_served():
    client = TestClient(app)
    resp = client.get("/desktop/")
    assert resp.status_code == 200
    assert "射箭训练报告" in resp.text
    assert "suooter-logo.png" in resp.text


def test_desktop_redirect_without_slash():
    client = TestClient(app)
    resp = client.get("/desktop", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/desktop/"


def test_desktop_css_and_logo_served():
    client = TestClient(app)
    assert client.get("/desktop/styles.css").status_code == 200
    assert client.get("/desktop/app.js").status_code == 200
    assert client.get("/desktop/assets/suooter-logo.png").status_code == 200
