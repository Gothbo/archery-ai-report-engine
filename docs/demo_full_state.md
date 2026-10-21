# 演示满态数据（设计师 / PM 用）

## 背景

UI「窗口留空」会按浏览器本地「今天」算 ISO 周。2026-10-19（Asia/Shanghai）对应 **2026-W43**。

旧扩展 mock 只到 **2026-W40**（末场约 10-02）；默认点「周报告」会落到空窗口。`POST /api/v1/ingest/mock` 默认仍是 4 周八月数据集，也不会覆盖到「今天」。

## 做法（最小改动）

- 扩展 `scripts/gen_mock_extended.py` → `mock_data/训练数据集_14周_张明.json` 覆盖 **2026-W27 ~ W43**（52 场 × 30 箭，含风档 / 心率 / 风暴场 / 跨日场 S152）。
- 演示重置仍走 `scripts/demo_reset.py`（清空张明 → 导入扩展集 → 锚点 S101）。

## 三步看到满态

1. 引擎用演示档启动（已有进程可跳过）：
   ```bash
   ENGINE_CONFIG=config.demo.json .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
   ```
2. 重置演示数据（幂等）：
   ```bash
   ENGINE_CONFIG=config.demo.json .venv/bin/python scripts/demo_reset.py
   ```
3. 打开 `http://127.0.0.1:8000/` → 选「张明」→ **窗口留空** → 点「周报告」（默认 2026-W43）。也可手填 `2026-W40` / `2026-10` / `2026Q3` 看故事线；日报选场次列表末尾的跨日场。

切换「教练 / 运动员」可看 `coach_extra` 是否剔除；备注与历史结论在下方两栏。

## 给设计师的样例 JSON

仓库外笔记目录可放一份满态周报：`archery-ai-report-engine-notes/full-report-sample.json`（由周报 `view=coach` 生成）。

## 不在本改动范围

- 静态页视觉改版（六六）
- 把 `POST /ingest/mock` 默认路径改成 14 周集（测试夹具仍依赖 4 周默认）

## 规则版指导（已接线）

报告 JSON 含 `rule_guidance`（与 `suggestions` 同源）。静态页 idle /「只看规则建议」/ `COACH-OFFLINE` 展示该字段；LLM 流式路径不变。
