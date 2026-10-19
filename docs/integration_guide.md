# 联调说明 · 宿主（ASMS / CU / display_sys）× 本引擎

> 读者：公司软件工程师（宿主侧集成）。
> 基线：`main` @ PR #1～#14 已合入后的行为。
> 本文是**最短联调清单**；字段细节见 [`portal_api_contract.md`](portal_api_contract.md)、流式契约见 [`guidance_stream_contract.md`](guidance_stream_contract.md)、总览见根目录 [`README.md`](../README.md)。
>
> **本仓库不改宿主软件。** 引擎是本机只读消费侧的可降级旁路服务；ASMS / CU / display_sys 的 in-app 对接由公司研发完成。

---

## 1. 本地拉起引擎

```bat
REM Windows 目标机（推荐）
start.bat
```

- 默认监听：`http://127.0.0.1:8000`（`ENGINE_HOST=127.0.0.1`，**不要改成 `0.0.0.0`**；接口无鉴权）。
- 配置档（改 `start.bat` 顶部 `ENGINE_CONFIG` 一行）：

| 档位 | 用途 |
| --- | --- |
| `config.llamacpp.local.json`（默认） | 演示 MDC 判定 + 本地 llama-server `:8090`（流式指导可用） |
| `config.demo.json` | 演示 MDC，对话关闭 |
| `config.json` | 正式 SSOT：`mdc_source=null`，只描述不判定，对话关闭 |

手动：

```powershell
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

自检：`GET http://127.0.0.1:8000/api/v1/health` → `status=ok`；Swagger：`/docs`；引擎自带静态页：`/`。

Linux / 本 box 联调同理：在仓库根建 venv、`pip install -e ".[dev]"`，用 `ENGINE_CONFIG=...` 或默认配置起 uvicorn（只绑 `127.0.0.1`）。

---

## 2. 关键接口（以代码路由为准）

前缀均为 `/api/v1`。完整表见 README「API 一览」；联调高频：

| 方法 | 路径 | 联调用途 |
| --- | --- | --- |
| GET | `/health` | 活着 + `timezone` / `mdc_source` / `llm_enabled` / `engine_version` |
| POST | `/ingest/mock` | 无真数据时灌演示集 |
| POST | `/ingest/sqlite` | 只读消费 display_sys SQLite（路径见配置） |
| POST | `/ingest/v12` | **开发侧按 v1.2 推数**（单条对象或 ≤500 数组） |
| GET | `/ingest/v12/last-received` | 各类 dt 最后受理 UTC 时间（门户新鲜度） |
| GET | `/athletes` | 列表含 `display_no` / `lane`；`name` 可为 `null` |
| POST | `/athletes/{id}/reports/{daily\|weekly\|monthly\|quarterly\|yearly}` | 生成报告（`?view=coach&refresh=1`） |
| GET | `/reports/{report_id}/content?view=` | **只读**已存正文（含 `metrics` / `conclusions`） |
| POST | `/athletes/{id}/guidance/stream` | AI 训练指导 SSE |
| GET | `/llm/status` | 忙闲（不含运动员身份） |
| POST | `/llm/tasks/{task_id}/cancel` | 真取消（等模型停后再放锁） |
| GET | `/portal/` | 同源门户（需配置 `portal.dist_dir`） |

`view` 只是展示视角，**不是鉴权**。跨站写请求（非本站 `Origin` / `Sec-Fetch-Site: cross-site`）→ 403 `CROSS-SITE-BLOCKED`。

---

## 3. v1.2 上行期望（绑定契约）

- **契约源**：PM《射箭电子靶数据接口 v1.2》对接包。引擎内 schema 为逐字节副本：`app/ingest/v12/schema.json`（`schemaVersion` `"1.1"`）、`schema_v1_2.json`（`"1.2"`）。样例：`tests/fixtures/v12/`。
- **入口**：`POST /api/v1/ingest/v12`，Body = 单条信封或数组。HTTP 200 只表示批次已处理；**逐条看** `results[].status`：`accepted` / `duplicate` / `invalid` / `unsupported`。
- **信封**：`schemaVersion` + `messageId` + `dataType` + `data`。时间须 UTC 且以 `Z` 结尾。
- **本期分发**：dt2 弹着、dt1 心率、dt4 风 → 参与计算；dt3 视频路径 / dt7 轨迹元数据 → 存但不算；dt5/dt6 → 只存原文；dt8/dt9 → `unsupported` 不落库。dt2 缺 `athleteId` → `unsupported`（补齐后可原 `messageId` 重发）。
- **去重**：`messageId` 精确去重；dt2 另按 `shotId`/`scoreId` 防重，重复不覆盖。结构 `invalid` 不占 `messageId`，修正后可原 id 重发。
- **身份**：`athleteId` 经 HMAC 脱敏为 `athlete_id`（不可逆）；`display_no` 是引擎本地顺序号，与身份无关，重建库会重编。
- **场次**：同运动员同本地日、箭间隔 ≤ `session_gap_minutes` 归一场；场次号 `{athlete_id}_{YYYYMMDD}_v{nn}`。

开发侧按文档推数即可；**不要**要求引擎去改 display_sys / Broker 架构。

---

## 4. 门户路径

1. 门户仓库 `npm run build`，产物目录配进当前引擎配置：`"portal": {"dist_dir": "<绝对或相对引擎根的 dist>"}`。
2. 重启引擎 → 打开 `http://127.0.0.1:8000/portal/`（`/portal` 307 到带尾斜杠）。
3. 未配置或无 `index.html` → 404 `PORTAL-NOT-CONFIGURED`。
4. **同源、不开 CORS**；门户产物不进本仓库。结论在引擎：`conclusions`（判定 / 风档差 / 和自己比 / 平台期）由引擎算好写入报告正文，页面只展示。

字段契约：[`portal_api_contract.md`](portal_api_contract.md)。

---

## 5. 流式指导 + 取消 · 冒烟

前置：`llm.enabled=true`，llama.cpp 原生 `llama-server` 在 `:8090`（或配置的上游）；**单 uvicorn worker**。

```text
1. POST /athletes/{id}/guidance/stream
   body: {"granularity":"weekly","window_key":"2026-W40","view":"coach"}
2. 期望事件序：accepted → stage(reading) → stage(writing) → delta*（仅教练）
   → stage(verifying) → final | guardrail_failed | error | cancelled
3. 记下 accepted.task_id → POST /llm/tasks/{task_id}/cancel
   → 流上收到 cancelled；期间 /llm/status 可为 cancelling，新请求 409 COACH-BUSY
4. 运动员视角：永不出现 delta
```

细节与宿主要点：[`guidance_stream_contract.md`](guidance_stream_contract.md)；目标机取消脚本：`scripts/verify_stream_cancel.py`。无模型时可用 `POST /ingest/mock` + 静态页 `/` 看报告 UI；指导会 503 `COACH-OFFLINE`（预期降级）。

---

## 6. 降级与边界（宿主必须知道）

| 情况 | 引擎行为 | 宿主勿假设 |
| --- | --- | --- |
| `mdc_source=null` | 报告只描述不判定（ADR-0002） | 不要假定总有进步/退步文案 |
| 缺风速/心率等 | 对应段显式「本窗口无 X 数据」 | 不要把缺失当 0 |
| `llm.enabled=false` / 模型挂 | 指导 503；`/ask` 不可用 | 走规则版 / 仅报告 |
| 护栏未过 | `guardrail_failed`，撤草稿，不展示 AI 正文 | 不要把 delta 草稿当终稿 |
| 引擎忙 | 409 `COACH-BUSY`（指导与 ask 共锁） | 不要自动排队连发 |
| 报告缓存命中 | `/content` 只读已存正文；`refresh=1` 才换 `report_id` | 不要假定提问会刷新报告 id |
| 跨站写 | 403 | 门户须同源或配 `security.allowed_origins`（禁 `*`/`null`） |

---

## 7. 宿主不要假设 / 不要做的事

1. **不要改本仓库去「修」ASMS / CU / display_sys**——产品决策：宿主 in-app 集成由公司工程师做。
2. **不要把引擎当权威写库**：对 display_sys SQLite 只读；v1.2 是上行接收，不是反向写采集链路。
3. **不要暴露到局域网**而不加鉴权代理；默认本机环回。
4. **不要多 worker** 跑 uvicorn（单飞锁在进程内）。
5. **不要用 llama-cpp-python 的 server 替代原生 llama-server**（取消语义依赖连接断开停生成）。
6. **不要把 `display_no` / HMAC `athlete_id` 当成平台 spid 或身份证**；重建 `facts.db` 后 `display_no` 会变。
7. **不要依赖 CORS**：设计为同机同源；需要宿主虚拟主机名时用 `allowed_origins` 白名单。
8. **CU 现有 `/api/coach` 映射是否改打本引擎**：由公司研发评估；本引擎只提供本文与契约文档中的路径。

---

## 8. 建议联调顺序（半日）

1. `health` → `ingest/mock` 或 `ingest/v12` 灌数 → `athletes` 见人与 `display_no`。
2. 生成周报 → `GET .../content?view=coach` 核对 `sections` / `metrics` / `conclusions`。
3. 打开 `/`：成绩 metricGrid、风档小表（有风速时）、对比度可读。
4. （可选）配 `portal.dist_dir` → `/portal/` 点开概览。
5. （有模型）指导流式 + cancel；再试运动员视角无草稿。
6. 故意停模型 / 用 `config.json`：确认报告仍可用、指导降级路径可理解。

问题排查优先看：引擎日志目录（配置 `log_dir`）、`/llm/status`、`/ingest/v12/last-received`、pytest `tests/test_v12_ingest.py` / `test_guidance_stream.py`。
