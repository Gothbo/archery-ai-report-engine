# 服务中心（门户）× 引擎 · 字段契约（PR #4 · P0）

> 读者：门户（六六 `service_portal`）、宿主研发、引擎维护者。
> 基线：本 PR 叠在 PR #3（`feat/streaming-guidance` @ `28e1618`）之上，依赖 PR #1 → #2 → #3。
> 本文只写 **本 PR 新增 / 变化** 的字段；未提到的接口和字段保持 PR #3 的行为。
> 范围由 PM（浩然）确认：同源托管、结论引擎化（风档差 / 和自己比 / 平台期）、`display_no`、第一批接口、两项小加固。

---

## 0. 通用约定

- 前缀 `/api/v1`，本机无鉴权（只监听 127.0.0.1）。`view` 是展示视角，**不是访问控制**。
- 时间：字段名以 `_utc` 结尾的是 UTC，ISO 8601 毫秒 + `Z`（如 `2026-10-08T07:36:00.123Z`）；`/llm/status` 里不带 `_utc` 的时间沿用 PR #3，带本地偏移（如 `2026-10-08T15:36:00+08:00`）。
- 本 PR 新增接口的错误体统一为 `{"code": "…", "detail": "中文说明"}`；已有接口的其他错误仍是 FastAPI 默认的 `{"detail": …}`。
- 字段「只加不改」：已有字段名、类型、含义都没变（例外见 §3.4「行为变化」）。
- `null` 一律表示「没有 / 不适用」，页面不要把 `null` 当 0。

### 0.1 新增错误码

| HTTP | `code` | 出现在 | 含义 |
|---|---|---|---|
| 404 | `REPORT-NOT-FOUND` | `GET /reports/{id}/content` | 报告 id 不存在或已失效（同窗口重新生成 / 同场次重新导入会换 id） |
| 404 | `REPORT-BODY-MISSING` | `GET /reports/{id}/content` | 报告是本 PR 之前生成的，没有保存正文；调用生成接口（不带 `refresh`）会补存 |
| 404 | `SESSION-NOT-FOUND` | 日报生成、`/ask`、`/guidance/stream`（日报窗口） | 该运动员没有这个场次（以前会生成一份 0 箭的空报告并写进缓存） |
| 403 | `CROSS-SITE-BLOCKED` | 所有 POST / PUT / PATCH / DELETE | 跨站页面发起的写请求（见 §7） |
| 404 | `PORTAL-NOT-CONFIGURED` | `GET /portal/…` | 没有配置门户产物目录，或目录里没有 `index.html` |

---

## 1. 同源托管：`/portal/`

- 配置 `config.json`：`"portal": {"dist_dir": "D:/service_portal/dist"}`（门户 `npm run build` 的产物目录；相对路径按引擎根目录解析）。不配置 = 不托管，`/portal/` 返回 404 `PORTAL-NOT-CONFIGURED`。
- 入口：`http://127.0.0.1:8000/portal/`（`/portal` 会 307 跳到 `/portal/`），宿主 WebView2 导航到 `http://127.0.0.1:8000/portal/index.html#/overview`。
- 门户产物不提交进引擎仓库；改了配置要重启引擎（配置在启动时读取，每次请求按当前配置解析目录）。
- **不开 CORS，不允许 `null` origin**。页面与接口同源，不需要预检，也不受 LNA 影响。

---

## 2. 运动员接口：`display_no`、`lane`、空名

### 2.1 `GET /api/v1/athletes`

```json
{ "athletes": [
  { "athlete_id": "1963169497552654337", "name": "张明", "bow_type": "反曲弓", "level": null,
    "last_session_utc": "2026-08-28T01:00:00.000Z",
    "display_no": 1, "lane": "lane-02", "lane_seen_at_utc": "2026-08-28T01:09:40.000Z" },
  { "athlete_id": "…", "name": null, "bow_type": "反曲弓", "level": null,
    "last_session_utc": null, "display_no": 2, "lane": null, "lane_seen_at_utc": null }
] }
```

| 字段 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `display_no` | integer（≥1） | 否 | **引擎本地顺序号**：按档案在本库首次建立的先后分配 1、2、3…，库内唯一、不复用。和身份证号、HMAC、`athlete_id`、平台 spid **没有任何数学关系**。重建数据库（删 `facts.db` 后重新导入）会重新编号，已获 PM 认可。 |
| `lane` | string | 是 | 该运动员**最近一支带靶位的箭**的靶位，原样字符串（如 `"lane-01"`），不解析成数字。来自 v1.2 dt2；SQLite / mock 源没有靶位 → `null`。 |
| `lane_seen_at_utc` | string | 是 | 上面那支箭的时间（UTC）。`lane` 为 `null` 时也是 `null`。 |
| `name` | string | **是** | 本 PR 起，v1.2 首次出现、没有档案的运动员**不再自动命名**为 `运动员{athlete_id 后四位}`，`name` 为 `null`，页面显示「未命名选手」。 |

### 2.2 `GET /api/v1/athletes/{athlete_id}`

在原有 `{athlete_id, profile, baseline}` 上追加顶层 `lane`、`lane_seen_at_utc`（同 §2.1）；`profile`（不为 `null` 时）里多一个 `display_no`。

### 2.3 `GET /api/v1/athletes/{athlete_id}/profile`

返回体多一个 `display_no`（integer）。`PUT …/profile` 不能修改 `display_no`。

### 2.4 旧库迁移（引擎启动时自动执行，幂等）

1. 给 `athlete_profile` 补列 `display_no`，按档案的插入顺序（SQLite `rowid`）给没有编号的档案依次编号，并建唯一索引。
2. 把**恰好等于** `运动员` + `athlete_id 后四位` 的名字清空为 `NULL`（只匹配旧版自动生成的名字，手工改过的名字不动）。
3. 给 `report_cache` 补列 `report_json`；给 `ingest_messages` 建索引 `(data_type, received_at_utc)`。

---

## 3. 报告正文：落库、只读读取、结论字段

### 3.1 `GET /api/v1/reports/{report_id}/content?view=athlete|coach`（新增）

- 只读：**不重算、不改 `generated_at_utc`、不改历史结论**。
- `view` 默认 `athlete`，只接受 `athlete` / `coach`（其他值 422）。
- 200：返回与生成接口相同结构的正文（§3.2），`cached: true`，`view` 为本次请求的视角。
- 404 `REPORT-NOT-FOUND` / 404 `REPORT-BODY-MISSING`（见 §0.1）。
- 视角处理（服务端完成）：`view=athlete` **不含 `coach_extra`**；「近期备注」一节按本次视角现取（运动员视角只有 injury / goal）。

### 3.2 报告正文结构（生成接口与 §3.1 共用）

```json
{
  "report_id": "6f0d…", "granularity": "weekly", "window_key": "2026-W33",
  "athlete": { "id": "1963169497552654337", "name": "张明" },
  "view": "coach", "cached": false, "generated_at_utc": "2026-10-08T02:10:00.123Z",
  "sections": [ { "key": "window_score", "title": "本周成绩", "content": ["…"], "evidence": [] } ],
  "suggestions": [],
  "coach_extra": { "warnings": [], "load": {}, "rolling_baseline": { "n_shots": 288, "avg_score": 9.1, "collected_at_utc": "…" } },
  "anchor_rebuild_hint": { "hint": false, "reason": "…" },
  "metrics": { "n_shots": 90, "avg_score": 9.12, "inner10_rate": 23.3, "far_miss_rate": 0.0, "hit_rate": 100.0,
               "total_score": 820.8, "mcr_t": 0.415, "hr_volatility": 3.2, "dispersion_mm": 41.5 },
  "conclusions": { "schema_version": 1, "verdicts": {…}, "wind_gap": {…}, "self_compare": {…}, "plateau": {…} }
}
```

- 新增 `metrics`、`conclusions`；`sections` 等原有字段不变（引擎自带页面和问答骨架继续用）。
- `athlete.name` 可为 `null`（未命名）。
- `coach_extra` **只在 `view=coach` 时出现**（本 PR 起生成接口和 §3.1 都在服务端剔除）。
- `metrics` 字段：`n_shots` integer；`avg_score`、`inner10_rate`（%）、`far_miss_rate`（%）、`hit_rate`（%）、`total_score` number；`mcr_t`（s）、`hr_volatility`（bpm）、`dispersion_mm`（mm）number 或 `null`（缺测）。

### 3.3 `conclusions`（引擎计算，页面只展示）

#### 3.3.1 `conclusions.verdicts` —— 四个指标对锚点的判定

键：`avgScore`、`mcrT`、`dispersionMm`、`hrVolatility`，每项：

```json
{ "verdict": "steady", "reason": "below_mdc", "value": 9.12, "anchor": 9.0, "delta": 0.12, "mdc": 0.3 }
```

| 字段 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `verdict` | `"progress"` \| `"regression"` \| `"steady"` | 是 | `null` = 未判定（原因见 `reason`）。方向按 `config.mdc[*].direction`（`mcrT`、`dispersionMm`、`hrVolatility` 越小越好） |
| `reason` | string | 否 | `above_mdc` / `below_mdc` / `mdc_pending`（`mdc_source` 为空或该指标阈值待共识）/ `sample_insufficient`（样本门槛未达，含周报 <2 次训练）/ `not_comparable`（距离 / 风况与锚点不可比）/ `no_anchor`（没有锚点）/ `missing_data`（本窗口或锚点缺这项数据） |
| `value` | number | 是 | 本窗口值 |
| `anchor` | number | 是 | 锚点值 |
| `delta` | number | 是 | **带符号** `value − anchor`（3 位小数）；`sections` 文案里的差值是绝对值 |
| `mdc` | number | 是 | 该指标的最小可检测变化阈值 |

#### 3.3.2 `conclusions.wind_gap` —— 风档差

```json
{ "status": "ok", "triggered": true, "split_mps": 2.0,
  "low":  { "n_shots": 60, "avg_score": 9.35 },
  "high": { "n_shots": 30, "avg_score": 8.95 },
  "gap": 0.4, "min_shots": 5, "threshold": 0.3, "provisional": true }
```

| 字段 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `status` | `"ok"` \| `"insufficient_sample"` \| `"no_wind_data"` | 否 | `no_wind_data`：本窗口没有任何有效风速；`insufficient_sample`：低风或高风一侧箭数 < `min_shots` |
| `triggered` | boolean | 否 | `status=="ok"` 且 `gap >= threshold` 时为 `true`（页面据此显示「风大时成绩略低」类提示） |
| `split_mps` | number | 否 | 切分风速：低风 = 风速 `< split_mps`，高风 = `>= split_mps`（缺测 / 负值哨兵不计入） |
| `low` / `high` | object | 否 | `n_shots` integer；`avg_score` number，该侧 0 箭时为 `null` |
| `gap` | number | 是 | **低风均环 − 高风均环**（正数 = 风拉低了成绩），2 位小数；`status!="ok"` 时为 `null` |
| `min_shots` / `threshold` | number | 否 | 本次使用的口径（来自 `config.conclusions`） |
| `provisional` | boolean | 否 | `true` = 切分 / 门槛 / 阈值是**演示口径，待 PM / 专家定**（默认 2.0 m/s、5 箭、0.3 环，与门户原临时推导一致） |

#### 3.3.3 `conclusions.self_compare` —— 和自己比（上一期平均环）

```json
{ "status": "ok",
  "previous": { "granularity": "daily", "window_key": "daily:S012", "started_at_utc": "2026-08-25T01:00:00.000Z",
                "n_shots": 30, "avg_score": 9.0, "bow_type": "反曲弓", "distance_m": 70 },
  "current_avg_score": 9.12, "delta": 0.12, "direction": "higher", "comparable": true }
```

| 字段 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `status` | `"ok"` \| `"no_previous"` \| `"no_data"` | 否 | `no_data`：本窗口 0 箭；`no_previous`：没有上一期数据 |
| `previous` | object | 是 | 上一期：**日报** = 该运动员时间上紧邻的前一场；**周 / 月 / 季 / 年报** = 紧邻的上一个窗口（如 `2026-W32`），那个窗口没有箭就是 `no_previous`，不往前找。直接从逐箭数据现算，不依赖历史结论 |
| `previous.started_at_utc` / `bow_type` / `distance_m` | string / string / integer | 是 | 只有日报有，其他粒度为 `null` |
| `current_avg_score` | number | 否 | 同 `metrics.avg_score` |
| `delta` | number | 是 | `current_avg_score − previous.avg_score`，2 位小数 |
| `direction` | `"higher"` \| `"lower"` \| `"same"` | 是 | 按 `delta` 符号（`delta == 0` → `same`）。**不是 MDC 判定**，只是事实描述 |
| `comparable` | boolean | 是 | 仅日报：前后两场弓种、距离都相同为 `true`；其他粒度 `null` |

#### 3.3.4 `conclusions.plateau` —— 平台期（修正版）

```json
{ "status": "ok", "triggered": true, "metric": "avgScore", "periods": 3,
  "recent": [ { "window_key": "2026-W31", "report_id": "…", "verdict": "steady" },
              { "window_key": "2026-W32", "report_id": "…", "verdict": "steady" },
              { "window_key": "2026-W33", "report_id": "…", "verdict": "steady" } ] }
```

| 字段 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `status` | `"ok"` \| `"insufficient_history"` | 否 | 同粒度、同判定口径的已存正文报告不足 `periods` 期 |
| `triggered` | boolean | 否 | `recent` 里 `periods` 期的 `verdict` 全是 `steady` |
| `metric` | string | 否 | 看哪个指标的判定，默认 `avgScore`（**每期看哪一项待 PM 定义**，可在 `config.conclusions.plateau_metric` 改） |
| `periods` | integer | 否 | 期数，默认 3 |
| `recent` | array | 否 | **按窗口时间从旧到新，最后一项是本期**。每项 `window_key` string、`report_id` string、`verdict`（同 §3.3.1，可空） |

修正点：旧逻辑取 `report_memories` 最近 3 行，而每份报告每个指标各写一行，实际取到的是**上一份报告的 3 个指标**。新逻辑每份报告算一期（取它保存的 `verdicts[metric]`），按窗口时间（日报按场次时间）排序，只看本期及之前的窗口；没有保存正文的旧报告不计入。触发时仍写一条 `plateau` 历史结论、`sections` 里仍有「平台期提示」一节。

### 3.4 行为变化（相对 PR #3）

| 场景 | 以前 | 现在 |
|---|---|---|
| 生成接口命中缓存 | 只返回 `{report_id, cached:true}` | 返回已存正文 + `cached:true`（本 PR 之前生成的旧缓存行仍只返回 id） |
| `/ask`、`/guidance/stream` | 每次都强制重算报告，`report_id` 失效、历史结论被重写 | 有已存正文就直接用（`report_id`、生成时间、`context_version` 保持不变）；没有才生成 |
| 生成接口 `view=athlete` | 也返回 `coach_extra` | 不返回 `coach_extra` |
| 日报不存在的 `session_id` | 生成 0 箭空报告并写缓存 | 404 `SESSION-NOT-FOUND`，不写缓存（场次属于别的运动员同样 404） |
| 跨站页面发来的写请求 | 照常执行 | 403 `CROSS-SITE-BLOCKED`（§7） |

---

## 4. `GET /api/v1/ingest/v12/last-received`（新增）

```json
{ "dt1": "2026-10-08T07:36:00.123Z", "dt2": "2026-10-08T07:34:10.000Z", "dt3": null, "dt4": "2026-10-08T07:30:00.000Z",
  "dt5": null, "dt6": null, "dt7": null, "source": "v12", "as_of_utc": "2026-10-08T07:36:05.000Z" }
```

| 字段 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `dt1`…`dt7` | string（UTC） | 是 | 引擎**受理**该类 v1.2 消息的最后时间（引擎收到的时间，不是设备时间）。重复投递、非法、不支持的消息不更新。`null` = 从未受理 → 页面显示「未接入」。dt1 心率 / dt2 报靶 / dt3 视频 / dt4 风 / dt7 轨迹；dt5 / dt6 只存原文 |
| `source` | string | 否 | 固定 `"v12"`。宿主 SQLite 导入路径没有「收到时间」，不计入 |
| `as_of_utc` | string | 否 | 引擎生成本响应的时间，用来和上面的时间比新鲜度（避免页面与引擎时钟不一致） |

只返回时间，不返回设备号、靶位、运动员。现场在开发侧上线 v1.2 推送前会全部是 `null`。

---

## 5. `GET /api/v1/llm/status` 追加字段

```json
{ "busy": false, "state": "idle", "task_id": null, "kind": null, "stage": null, "started_at": null, "deadline_at": null,
  "llm_enabled": true, "provider": "llamacpp", "backend": "ready", "backend_checked_at": "2026-10-08T10:20:00+08:00" }
```

| 字段 | 类型 | 可空 | 说明 |
|---|---|---|---|
| `provider` | `"llamacpp"` \| `"ollama"` \| `"mock"` | 否 | 配置的模型服务类型 |
| `backend` | `"ready"` \| `"loading"` \| `"unreachable"` | 是 | llamacpp：探测 llama-server `GET /health`，200 → `ready`，503 → `loading`（模型加载中），连不上 / 超时 / 其他状态码 → `unreachable`。ollama：`GET /api/version` 通 → `ready`，否则 `unreachable`（ollama 没有「加载中」状态）。mock → `ready`。`llm_enabled=false` 时**不探测**，为 `null` |
| `backend_checked_at` | string（本地偏移） | 是 | 探测时间；`backend` 为 `null` 时也是 `null` |

探测超时 1 秒，结果缓存 3 秒（页面轮询不会打满模型服务）；探测失败不影响报告链路。
**注意**：门户契约原来写的是 `offline`，引擎按 PM 确认的口径用 `unreachable`，请门户适配层映射。

## 6. `GET /api/v1/health` 追加字段

```json
{ "status": "ok", "config_version": "0.1.0", "timezone": "Asia/Shanghai", "db": "…", "mdc_source": "empty",
  "llm_enabled": false, "engine_version": "0.1.0" }
```

- `engine_version`：string，取自 `pyproject.toml` 的 `[project] version`（取不到时用已安装包的版本；都取不到为 `null`）。**版本号规则待 PM 定**，目前所有分支都是 `0.1.0`，暂时区分不出版本。
- `db` 路径仍返回（现有测试依赖）；门户不读取。

---

## 7. 跨站写请求拦截（小加固）

对 POST / PUT / PATCH / DELETE：
- 带 `Origin` 头：必须与请求的 `Host` 同源（同协议、同主机、同端口），或在 `config.security.allowed_origins` 列表里；否则 403 `CROSS-SITE-BLOCKED`。`Origin: null` 一律拒绝（配置里不允许写 `null` 或 `*`）。
- 不带 `Origin` 头：本机工具（curl、Python、宿主进程）照常放行；但如果浏览器带了 `Sec-Fetch-Site: cross-site`，拒绝。
- GET 不受影响；不开 CORS。
- 关闭开关：`config.security.block_cross_site_writes=false`（不建议）。
- 已知没做：Host 白名单（防 DNS 重绑定）、限流（门户轮询问题已在门户侧修复）。

---

## 8. 备注 `actor_id`（只写清楚现状，本 PR 不改语义）

- `POST /athletes/{id}/notes` 请求体的 `actor_id`：**必填**的字符串（pydantic `str`），引擎**不校验格式、不校验是否存在**，原样存进 `memory_notes.actor_id`（库注释：录入人雪花 ID，用于审计），并在 `GET …/notes` 里原样返回。
- 引擎不用 `actor_id` 做权限判断：删除只校验备注属于路径里的 `athlete_id`，**不校验录入人**。「运动员只能删自己写的备注」只是门户的展示层限制。
- 隐私：`actor_id` 会被存储并在接口里返回，**禁止传身份证号**，应传宿主的账号 / 用户 ID（雪花 ID）。

---

## 9. 未纳入本 PR（第二批 / 待定）

- `GET /reports` 跨运动员列表、`GET /diagnostics/error-codes`、报告正文里的 `baseline` 详情 / `wind_bands` 列表 / `session` / `week` / `generated_at`（带偏移）等门户 `EngineReportRaw` 其余字段。
- 风档差阈值、平台期口径、版本号规则：待 PM / 专家定。
- 运动员角色如何拿到 `athlete_id`（不经过身份证）：待 PM 与开发侧定。
