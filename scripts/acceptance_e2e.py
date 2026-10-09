# -*- coding: utf-8 -*-
"""M6 端到端验收：通过 HTTP 全链路验证（健康/导入/五档报告/档案/备注）。"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000/api/v1"
ATHLETE = "1963169497552654337"


def call(method: str, path: str, body: dict | None = None, params: dict | None = None):
    url = BASE + path
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def wait_health(retries=20):
    for i in range(retries):
        try:
            return call("GET", "/health")
        except Exception:
            time.sleep(1)
    raise RuntimeError("服务未就绪")


def main():
    print("== 1 健康检查 ==")
    st, h = wait_health()
    assert st == 200 and h["status"] == "ok" and h["timezone"] == "Asia/Shanghai"
    print("health:", h["timezone"], "| mdc_source:", h["mdc_source"], "| db:", h["db"])

    print("\n== 2 导入 mock（幂等；全新库首导 390，已导入则 0 新增）==")
    st, r = call("POST", "/ingest/mock")
    assert st == 200 and r["stats"]["inserted_shots"] in (0, 390), r
    print("导入:", r["stats"])
    st, r2 = call("POST", "/ingest/mock")
    assert r2["stats"]["inserted_shots"] == 0, r2
    print("重导幂等:", r2["stats"])

    print("\n== 3 档案建档（mock 源自带姓名）==")
    st, p = call("GET", f"/athletes/{ATHLETE}/profile")
    assert st == 200 and p["name"] == "张明", p
    print("档案:", p["name"], p.get("level"))

    print("\n== 4 即时报告 + 周报 ==")
    st, rep = call("POST", f"/athletes/{ATHLETE}/reports/daily", params={"session_id": "S001"})
    assert st == 200 and rep["cached"] is False and rep["granularity"] == "daily"
    rid = rep["report_id"]
    st, g = call("GET", f"/reports/{rid}")
    assert st == 200
    print("daily 段:", [s["key"] for s in rep["sections"]])
    st, wk = call("POST", f"/athletes/{ATHLETE}/reports/weekly",
                  params={"week": "2026-W32", "refresh": "true"})
    assert st == 200
    keys = [s["key"] for s in wk["sections"]]
    print("weekly 段:", keys)
    texts = " ".join(" ".join(str(c) for c in s["content"]) for s in wk["sections"])
    assert "含脱靶均环" in texts
    assert "level" in keys and "无锚点" in texts  # 无锚点 → level 段为占位（不静默省略）
    print("无锚点 level 占位: True")

    print("\n== 4b 建锚点后周报走降级模板（mdc_source=null，M4.5 前只描述不判定）==")
    st, anc = call("POST", f"/athletes/{ATHLETE}/baseline/anchor",
                   body={"bow_type": "反曲弓", "source_session_id": "S001"})
    assert st == 200 and anc["snapshot"]["snap_type"] == "anchor", anc
    print("锚点已建:", anc["snapshot"]["avg_score"], "环, 可比性元数据:",
          anc["snapshot"]["distance_m"], "/", anc["snapshot"]["mode_composition"])
    st, wk2 = call("POST", f"/athletes/{ATHLETE}/reports/weekly", params={"week": "2026-W32", "refresh": "true"})
    assert st == 200
    keys2 = [s["key"] for s in wk2["sections"]]
    assert "level" in keys2, keys2
    texts2 = " ".join(" ".join(str(c) for c in s["content"]) for s in wk2["sections"])
    assert "降级期" in texts2 and "暂无进步/退步判定" in texts2, texts2  # 降级提示（ADR-0002）
    assert "未判定" in texts2, texts2  # 锚点降级模板：只描述不判定
    print("weekly 段:", keys2)
    print("降级模板（只描述不判定）: True")

    print("\n== 5 真 SQLite 库导入（M5，只读消费）==")
    st, r3 = call("POST", "/ingest/sqlite")
    assert st == 200, r3
    print("真库导入:", r3["stats"], "| 建档运动员:", r3["athlete_profiles"])

    print("\n== 6 真库运动员抽查（最近一次训练 + 周报）==")
    st, bl = call("GET", f"/athletes/{ATHLETE}/baseline")
    rolls = [s for s in bl["snapshots"] if s["snap_type"] == "rolling"]
    print("张明滚动基线:", rolls[0]["n_shots"], "箭 avg:", rolls[0]["avg_score"])

    st, sess = call("GET", f"/athletes/{ATHLETE}/sessions")
    print("张明场次数:", len(sess["sessions"]))

    print("\n== 7 备注双视角 + 软删 ==")
    st, c = call("POST", f"/athletes/{ATHLETE}/notes",
                 body={"note_type": "injury", "content": "右肩轻微酸痛", "author_role": "athlete", "actor_id": ATHLETE})
    assert st == 200
    nid = c["note_id"]
    st, n_a = call("GET", f"/athletes/{ATHLETE}/notes", params={"view": "athlete"})
    st, n_c = call("GET", f"/athletes/{ATHLETE}/notes", params={"view": "coach"})
    assert any(n["id"] == nid for n in n_a["notes"]) and any(n["id"] == nid for n in n_c["notes"])
    st, d = call("DELETE", f"/athletes/{ATHLETE}/notes/{nid}")
    assert st == 200
    print("备注写入/双视角可见/软删: 通过")

    print("\n== 验收结论：M6 端到端全部通过 ==")


if __name__ == "__main__":
    main()
