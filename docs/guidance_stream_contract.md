# AI 训练指导 · 流式接口契约与宿主集成指南（PR #3）

> 读者：公司研发（CU 激光射击软件 / ASMS 宿主 / 其他调用方）。
> 范围：本引擎（射箭 AI 训练报告引擎）新增的流式训练指导、单飞锁、状态与取消接口。
> **CU 页面和宿主软件不在本仓库，本 PR 没有改动它们**；接入由公司研发按本文实施。
> 时间均为引擎配置时区（默认 Asia/Shanghai，ISO 8601 带 `+08:00`）。

## 1. 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/athletes/{athlete_id}/guidance/stream` | 生成一份训练指导，响应为 `text/event-stream`（SSE） |
| GET | `/api/v1/llm/status` | 引擎当前是否有 LLM 任务在跑 |
| POST | `/api/v1/llm/tasks/{task_id}/cancel` | 取消任务（真正停止后台生成） |
| POST | `/api/v1/athletes/{athlete_id}/ask` | 原对话问答，**行为变化**：引擎忙时返回 409（见 §4） |

接口无鉴权，只监听本机（见 README「安全与隐私」）。

## 2. 生成指导：`POST /api/v1/athletes/{athlete_id}/guidance/stream`

### 2.1 请求

```json
{"granularity": "weekly", "window_key": "2026-W40", "view": "coach"}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `granularity` | 是 | `daily` / `weekly` / `monthly` / `quarterly` / `yearly` |
| `window_key` | 是 | 与报告接口一致：`daily:{session_id}`、`2026-W40`、`2026-10`、`2026Q4`、`2026` |
| `view` | 否 | `coach` / `athlete`，默认 `athlete` |

一次生成绑定「这份报告 + 这个视角」。换报告、切视角、关页面 = 取消这次生成并丢弃结果（见 §6）。

### 2.2 打开流之前的错误（普通 JSON，不是流）

| HTTP | `code` | 含义 | 建议界面状态（V17.2 §16.4） |
| --- | --- | --- | --- |
| 409 | `COACH-BUSY` | 引擎已有任务在跑或正在停止 | 3 已不再等待 / 重新生成禁用至空闲 |
| 503 | `COACH-OFFLINE` | `llm.enabled=false` | 7 AI 离线 → 规则版 |
| 400 | — | 运动员不存在 / 窗口无数据等（`detail` 为说明） | 5 生成中断（COACH-4xx） |
| 422 | — | 参数格式错误 | 5 生成中断（COACH-4xx） |

409 响应：

```http
HTTP/1.1 409 Conflict
Retry-After: 37

{"code": "COACH-BUSY", "detail": "引擎正在生成另一份内容，请稍后再试",
 "task_id": "9f3c1a2b4d5e6f70", "state": "running", "retry_after_sec": 37}
```

`state=running` 时 `retry_after_sec` 按任务截止时间估算；`state=cancelling` 时固定 2。

### 2.3 事件流

响应头：`Content-Type: text/event-stream`、`Cache-Control: no-cache`、`X-Accel-Buffering: no`。
每个事件为 `event: <名称>` + `data: <JSON>` + 空行；每个 `data` 都带 `task_id`。

| 顺序 | 事件 | data（除 task_id） | 说明 |
| --- | --- | --- | --- |
| 1 | `accepted` | `kind, view, granularity, window_key, report_id, context_version, started_at, deadline_at, stream_deltas` | 记下 `task_id`，取消要用 |
| 2 | `stage` | `{"stage":"reading"}` | 读取数据：模型 prompt 预处理（prefill） |
| 3 | `stage` | `{"stage":"writing"}` | 收到模型第一个字时发（真实信号，不是估算） |
| 4… | `delta` | `{"text":"…"}` | **仅教练视角**，草稿片段，按到达顺序拼接 |
| 5 | `stage` | `{"stage":"verifying"}` | 模型结束，服务端对全文做数据核对（G1 禁用词 / G2 数字必须在报告中 / G3 长度） |
| 末 | 终止事件（4 选 1） | 见下表 | 流随后关闭 |
| 任意 | 注释行 `: ping` | — | 无事件超过约 10 s 时发（读取阶段常见），解析时忽略 |

终止事件：

| 事件 | data | 界面 |
| --- | --- | --- |
| `final` | `text, degraded:false, guard:{passed:true}, generated_at, sources[], context_version, granularity, window_key, view` | 0 已生成：**用 `text` 整体替换草稿**（不是把草稿转色） |
| `guardrail_failed` | `code:"COACH-GUARD", message, rule ("G1"/"G2"/"G3"), fallback_skeleton, sources[], context_version, …`；教练视角另有 `reason` | 6 未通过核对：撤下草稿，**不显示任何 AI 文本**；`message` 为与 `/ask` 相同的降级文案，可附 `fallback_skeleton`（报告原文） |
| `error` | `code, message`，超时另有 `timeout`（`first_token`/`idle`/`total`），上游错误另有 `detail` | `COACH-OFFLINE` → 7 规则版；`COACH-TIMEOUT` / `COACH-LLM` / `COACH-EMPTY` → 5 生成中断 |
| `cancelled` | `reason`（`user` / `disconnect` / `shutdown`） | 回到未生成，可立即重新生成 |

**客户端读到流结束却没有终止事件**（引擎进程崩溃、网络断开）→ 按 `COACH-EMPTY` 生成中断处理。

### 2.4 运动员视角

- 服务端**从不发送 `delta`**，只发 `accepted → reading → writing → verifying → 终止事件`；不依赖前端开关。
- `guardrail_failed` 在运动员视角不带 `reason`（原因里可能有未核对的文字或数字）。
- 输入侧：运动员视角的骨架只含 `notes_visibility.athlete_visible_types`（默认伤病、目标）类备注，不含教练观察；滚动基线两个视角都带（浩然拍板，保持现状）。

### 2.5 正文格式

纯文本，固定五个小标题（每个单独一行）：`一、本次概述` / `二、数据基础` / `三、技术表现` / `四、状态与负荷` / `五、提升方案`。
结构化字段（等级、规则号 B2/C1/D1 等）**不由模型生成**；需要时由规则引擎给出。模型可能偶尔不严格遵守格式，宿主按纯文本展示即可。

## 3. 状态：`GET /api/v1/llm/status`

```json
{"busy": true, "state": "running", "task_id": "9f3c1a2b4d5e6f70", "kind": "guidance",
 "stage": "writing", "started_at": "2026-10-06T15:02:11+08:00",
 "deadline_at": "2026-10-06T15:05:11+08:00", "llm_enabled": true}
```

- `state`：`running` / `cancelling` / `idle`；`kind`：`guidance` / `ask`；`stage`：`reading` / `writing` / `verifying`（`ask` 为 null）。
- 空闲时 `busy=false, state="idle"`，其余字段为 null。
- **不含运动员身份与内容**。
- 用途：409 或「正在停止」后轮询（建议 2 s 一次，只在等待期间轮询），空闲即解禁「生成 / 重新生成」；页面重开时恢复状态。

## 4. 取消：`POST /api/v1/llm/tasks/{task_id}/cancel`

| 结果 | HTTP | body |
| --- | --- | --- |
| 已受理，正在停止 | 202 | `{"task_id": "…", "state": "cancelling"}` |
| 带 `?wait_sec=N`（0–10）且 N 秒内已停止 | 200 | `{"task_id": "…", "state": "cancelled"}` |
| 任务不存在或已结束 | 404 | `{"code": "COACH-TASK-NOT-FOUND", …}` |
| 非流式 `/ask` 任务（不可中途取消） | 409 | `{"code": "COACH-NOT-CANCELLABLE", …, "retry_after_sec": N}` + `Retry-After` |

- 取消后原 SSE 流会在**后台真正停止之后**发 `cancelled` 并关闭；在此之前 `status.state=cancelling`，新请求仍 409。
- 兜底：客户端断开连接（关页面、abort fetch）时，服务端同样执行取消。建议宿主**先调 cancel 再断开**：中间层（代理、WebView2）对断开的传递不一定可靠，显式接口可确认。
- `/ask`：仍是非流式一次性返回；它也占用同一把锁（忙时 409，`code=COACH-BUSY`）。**这是行为变化**，原来调用 `/ask` 的宿主需要处理 409。

## 5. 超时与停止（服务端）

| 配置（`config.json → llm`） | 默认 | 触发 |
| --- | --- | --- |
| `connect_timeout_sec` | 5 | 连不上模型服务 → `error COACH-OFFLINE` |
| `first_token_timeout_sec` | 90 | 读取阶段等第一个字超时 → `error COACH-TIMEOUT (first_token)` |
| `idle_timeout_sec` | 30 | 两个字之间空闲超时 → `error COACH-TIMEOUT (idle)` |
| `total_timeout_sec` | 180 | 单次总截止 → `error COACH-TIMEOUT (total)` |
| `guidance_max_len` | 1000 | 全文超过上限：提前停止模型，判 `guardrail_failed (G3)` |

- 流式路径**不自动重试**，由用户点「重试」。非流式 `/ask` 仍用 `timeout_sec` + `max_retries`。
- 锁只在模型服务**确实停下**之后释放：关闭上游连接后，引擎轮询 llama-server 的 `GET /slots`，直到没有 slot 在处理（最多等 `first_token_timeout_sec`）。`/slots` 不可用时不等。
- 超时值是估算值，需在 780M 目标机实测后调整（见 §8）。

## 6. 宿主侧实现要点（CU / ASMS / WebView2）

1. **用 `fetch` + `ReadableStream` 读 SSE**（`EventSource` 只能发 GET，不能带请求体）。按空行切事件；忽略以 `:` 开头的心跳行。参考实现：本仓库 `static/index.html` 中的 `startGuidance()`。
2. **绑定请求**：每次生成记录 `seq/task_id + 报告 + 视角`。换报告、切视角、关弹窗、换运动员：调 cancel → abort fetch → 丢弃之后到达的所有事件。运动员视角下绝不能出现教练视角的草稿。
3. **防连点**：在途时禁用「生成」；「取消」后显示「正在停止…」，等 `cancelled` 事件或 `status` 空闲再解禁。
4. **草稿与正文**：教练视角把 `delta` 拼成灰色草稿（「草稿 · 待核对」），`final.text` 到达后整体替换；`guardrail_failed` / `error` / `cancelled` 时草稿整体撤下。
5. **409**：显示「引擎正在生成另一份内容」，按 `Retry-After` 或轮询 `status` 等待空闲，不要自动排队重发。
6. **中间层缓冲**：若 SSE 经过代理或宿主转发，必须关闭响应缓冲，否则流式会退化成一次性显示（需在宿主环境做 PoC）。
7. **WebView2**（角角报告 `research/engineering/780m_llamacpp_and_webview2.md`）：网页内实现所有弹窗/下拉（空域问题）；不要把报告页放在默认未选中的 Tab 里初始化；UserDataFolder 设到 `%LOCALAPPDATA%`；DPI 用 PerMonitorV2、不开 `gdiScaling`；与宿主通信用 postMessage + JSON。
8. **CU 页**：目前调用的是另一个后端（`POST {COACH_BASE}/api/coach`，不在本仓库）。是否改为调用本引擎、要不要在 CU 后端做 `/api/coach` → `/api/v1/...` 的映射，由公司研发评估；本 PR 只提供本文契约。

## 7. 部署约束

- **模型服务用 llama.cpp 原生 `llama-server`**（`start.bat` 已改）。不再使用 llama-cpp-python 的 `llama_cpp.server`（据其上游代码与文档：第一个字之前无法取消，且默认新请求会打断旧的流式请求）。建议参数：`-c 4096 -np 1`，`/slots` 保持默认开启。
- **单 uvicorn worker**：单飞锁在进程内，多 worker 会失效（`start.bat` 默认 1 个 worker）。
- 引擎重启时登记表清空，不会留下僵尸锁。

## 8. 已验证 / 未验证

| 项 | 结论 | 依据 |
| --- | --- | --- |
| 事件顺序、运动员视角无 delta、409、status、cancel、超时、护栏降级、断开兜底 | 已验证 | 自动化测试 `tests/test_guidance_stream.py`（mock 流式 LLM + 线程内真 uvicorn） |
| 关闭上游 HTTP 连接后，上游服务端观察到断开并停止产出 | 已验证 | 同上，假 llama-server（OpenAI 兼容 SSE） |
| 真 llama-server **撰写阶段**断开即停 | 已验证（box，CPU） | llama.cpp b11435 + Qwen2.5-0.5B Q4_K_M：断开后约 0.05 s slot 空闲，日志 `srv stop: cancel task` |
| 真 llama-server **读取阶段（prefill）**断开 | **不会中途打断** | 同一环境：约 1700 token 的 prompt，第 1.5 s 断开，服务端把 prompt 处理完（约 5 s）才执行取消；`-b 256` 也一样。引擎因此在这段时间保持 `cancelling`，确认 `/slots` 空闲后才放锁（用引擎执行器直连真 llama-server 验证：取消后约 3.8 s 才释放，释放时 slot 空闲） |
| 780M（Vulkan）上的 prefill 时长、首字时间、停止耗时 | **未验证** | 需在目标机运行 `scripts/verify_stream_cancel.py` |
| WebView2 / 宿主中间层对 SSE 的缓冲与断开传递 | **未验证** | 需宿主 PoC |
