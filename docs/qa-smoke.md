# 粉粉验收 · 冒烟清单（对照当前 main）

> 交给测试同学「粉粉」手测用。范围：`main` 已合入功能（会话备注 #1、v1.2 接入 #2、流式指导+真取消 #3、门户 P0+#12 报告质量 #4、静态对比度/metricGrid #13、风档表 #14）。
> 方式：复现 → 定位 → 现象 → 是否回归。不 @ 任何人；结果回传圆圆 / PM 浩然即可。
>
> 环境：Windows 目标机优先；`start.bat` 起引擎 → `http://127.0.0.1:8000`。无真库时先 `POST /api/v1/ingest/mock`。有模型用默认 `config.llamacpp.local.json`；只测报告 UI 可用 `config.demo.json`。

**图例**：✅ 通过 / ❌ 失败（记下 URL、请求、截图、时间 Asia/Shanghai）/ ⏭ 跳过（缺依赖）

---

## 0. 启动与健康

| # | 步骤 | 期望 | 结果 |
| --- | --- | --- | --- |
| 0.1 | 运行 `start.bat`（或等价 uvicorn） | 无报错；浏览器可开 `http://127.0.0.1:8000/` | |
| 0.2 | `GET /api/v1/health` | `status=ok`，有 `timezone`、`mdc_source`、`llm_enabled`、`engine_version` | |
| 0.3 | 确认监听本机 | 仅 `127.0.0.1:8000`，不是对全网开放 | |

---

## 1. 灌数（mock 或 v1.2 快乐路径）

| # | 步骤 | 期望 | 结果 |
| --- | --- | --- | --- |
| 1.1 | `POST /api/v1/ingest/mock` | `status=ok`，`stats` 有导入箭/场次数；可重复调用幂等 | |
| 1.2 | （可选）`POST /api/v1/ingest/v12` 投一条合法 dt2（见 `tests/fixtures/v12/`） | HTTP 200，该条 `accepted`；再投同 `messageId` → `duplicate` | |
| 1.3 | `GET /api/v1/athletes` | 至少 1 人；有 `display_no`（≥1）；`name` 允许为 null（未命名） | |
| 1.4 | `GET /api/v1/ingest/v12/last-received`（若走了 v12） | 对应 `dt2` 等时间为 UTC `…Z` 或 null；无设备号/姓名泄露 | |

---

## 2. 报告章节与正文（含 #12 质量）

| # | 步骤 | 期望 | 结果 |
| --- | --- | --- | --- |
| 2.1 | 静态页选运动员 → 生成「周报」（或 `POST .../reports/weekly?view=coach`） | 返回完整正文：`sections`、`suggestions`；教练视角可有 `coach_extra` | |
| 2.2 | `GET /api/v1/reports/{id}/content?view=athlete` | 200；**无** `coach_extra`；`metrics` / `conclusions` 存在（结构见门户契约） | |
| 2.3 | 同 id 再 `view=coach` | 可有 `coach_extra`；不重算打乱已存结论（`cached` 语义合理） | |
| 2.4 | 看 `sections` 骨架 | 固定段可见（成绩 / 水平或降级说明 / 数据完整性 / 风档等）；缺数据时有「本窗口无 X」类说明，而非空白消失 | |
| 2.5 | `conclusions.wind_gap` / `self_compare` / `plateau` / `verdicts` | 字段齐全；`null` 不当 0；`provisional` 等演示口径可出现 | |

---

## 3. 静态页 UI（#13 metricGrid / 对比度，#14 风档表）

| # | 步骤 | 期望 | 结果 |
| --- | --- | --- | --- |
| 3.1 | 打开 `/`，生成或选中一份有成绩的报告 | 「本周/本日成绩」区出现 **metricGrid** 数字块（均环等），而非只有长段落 | |
| 3.2 | 有风速数据的窗口 | 「风档」段为**对齐小表**（风档 / 均环 / n），可读 | |
| 3.3 | 无风速窗口 | 风档段显式缺失说明，不报错、不空崩 | |
| 3.4 | 浅色背景上的正文/表格 | 对比度可接受（字不是灰到看不清）；快速扫一眼按钮与标题 | |

---

## 4. 门户 P0（#4，可选但建议）

| # | 步骤 | 期望 | 结果 |
| --- | --- | --- | --- |
| 4.1 | 配置 `portal.dist_dir` 指向已 build 的门户 dist，重启 | `GET /portal/` 出门户；未配置时 404 `PORTAL-NOT-CONFIGURED` | |
| 4.2 | 门户内打开概览 / 选手 | 同源调引擎接口成功；选手有 `display_no` | |
| 4.3 | （若方便）从其他 Origin 对写接口发 POST | 403 `CROSS-SITE-BLOCKED` | |

无门户产物 → 整节标 ⏭，并在备注写「未配 dist」。

---

## 5. 流式 AI 指导 + 真取消（#3）

前置：`llm_enabled=true` 且 llama-server 可用。否则 5.x 标 ⏭，并确认 `COACH-OFFLINE` 可理解。

| # | 步骤 | 期望 | 结果 |
| --- | --- | --- | --- |
| 5.1 | 教练视角点「生成指导」（或打 stream 接口） | 事件：`accepted` → `reading` → `writing` → 有 `delta` 草稿 → `verifying` → `final`（或护栏失败降级） | |
| 5.2 | 生成中点取消（或 `POST /llm/tasks/{task_id}/cancel`） | 稍后 `cancelled`；`/llm/status` 经 `cancelling` 回 `idle`；可再次生成 | |
| 5.3 | 取消未完成时再点生成 | 409 `COACH-BUSY`，不双开 | |
| 5.4 | 运动员视角生成 | **全程无 delta**；终态仍是 final / 降级 / error 之一 | |
| 5.5 | 护栏失败（若偶发） | 草稿撤下，界面不展示未通过 AI 正文 | |

---

## 6. 会话 / 备注（#1，轻量）

| # | 步骤 | 期望 | 结果 |
| --- | --- | --- | --- |
| 6.1 | `GET .../sessions` 与点进一场 | 场次列表与逐箭可打开 | |
| 6.2 | 教练视角加一条备注，运动员视角列表 | 可见性按类型过滤（运动员默认仅伤病/目标类）；软删后列表不再展示 | |

---

## 7. 回归速查（改完易碎点）

- [ ] mock 再导一次不炸、不重复造幽灵场次到异常多
- [ ] `health` 仍 200
- [ ] 静态页报告区刷新无整页白屏
- [ ] 无模型时报告链路仍可用（指导可失败，报告不能挂）

---

## 回传模板

```
环境：机型 / 配置档 / 是否有 llama-server / 是否配了 portal
结果：0.x～7 中 ❌ 项编号 + 现象 + 截图路径
阻塞：无 / 有（写一句）
结论：可验收 / 需修后再测
```
