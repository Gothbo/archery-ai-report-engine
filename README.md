# 射箭 AI 训练报告引擎（eng）

速得尔（SUOOTER）射箭训练报告引擎的工程化实现。核心引擎（`ingest/store/metrics/reports/memory`）零 API 依赖，`app/main.py` 只是 FastAPI 薄壳。

## 定位

- **纯消费者层**：只订阅 Broker / 读现有数据库，不改动既有数据采集架构
- **本地服务化**：单机 SQLite（`facts.db`）+ FastAPI，供三屏应用（运动员/教练/设备端）调用
- **证据可追溯**：报告每条结论带判定依据（锚点 ID / MDC 口径版本 / 样本量 / 可比性元数据）

## 快速开始

### 一键启动

```bat
start.bat
```

脚本自动完成：创建 `.venv`（缺省）→ 按 `pyproject.toml` 安装依赖（缺省）→ 起本地模型服务（`models\` 下有 gguf 且 `8090` 空闲时）→ 启动 `http://127.0.0.1:8000`。

运行档由脚本顶部 `ENGINE_CONFIG` 一行决定（改一行即可切换，脚本本身保持纯 ASCII，避免 CMD 代码页乱码）：

| 档位 | mdc_source | 报告 | 对话 |
| --- | --- | --- | --- |
| `config.llamacpp.local.json`（默认） | demo-thresholds-20260928 | 出 MDC 判定 | 接本地模型 `:8090` |
| `config.demo.json` | demo-thresholds-20260928 | 出 MDC 判定 | 关闭 |
| `config.json`（正式 SSOT） | null | 只描述不判定 | 关闭 |

已有外部模型服务（llama.cpp Vulkan 版 / Ollama）时保持 `LLM_PORT` 被占用即可，脚本会直接复用。

### 手动命令

```powershell
# 启动（默认只监听本机 127.0.0.1；接口无鉴权，不要改成 0.0.0.0 暴露到局域网）
.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 测试（118 用例：指标/规则/窗口/对齐/库/API/反硬编码）
.venv\Scripts\python -m pytest -q
# 服务自检
.venv\Scripts\python -c "import app.main; print('ok')"
```

### 环境要求

- Python 3.10+（Windows 下时区库 `tzdata` 已入依赖）
- 首次导入真库前，按环境调整 `config.json → store.sqlite_source_path`

### 安全与隐私（P0）

- **只监听本机**：`start.bat` 默认 `ENGINE_HOST=127.0.0.1`。接口没有鉴权且返回运动员数据，需要局域网访问时必须先加鉴权代理。
- **运动员 ID 不可逆**：身份证号（SQLite 源）或 v1.2 `athleteId` 经 HMAC-SHA256 脱敏为 19 位数字 `athlete_id`（`app/ingest/identity.py`）。身份证号不落库、不出现在任何接口响应里。
- **脱敏密钥**：优先读环境变量 `ENGINE_ID_SECRET`，其次 `config.store.id_secret_path`，否则用 `facts.db` 同目录的 `athlete_id.key`（首次自动生成）。**密钥须与 `facts.db` 一起备份；丢失或更换后所有 `athlete_id` 都会变，新旧数据无法关联。**
- **旧库迁移**：旧版 `athlete_id` 是 `10^18 + 身份证号`（可逆）。已有 `facts.db` 请先停服务，再运行 `python scripts/migrate_athlete_ids.py --db facts.db --source-db <SQLiteTest.db>`（默认只预览，加 `--apply` 执行；会自动备份、改写各表 ID、清空 `identity_id`、清掉报告缓存）。备份文件里仍有旧明文，核对后要安全删除。也可以直接清库，用新版本重新导入。

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/health` | 服务健康 + 口径摘要（timezone/mdc_source/db） |
| POST | `/api/v1/ingest/mock` | 导入 mock 数据集（幂等） |
| POST | `/api/v1/ingest/sqlite` | 导入 display_sys 真 SQLite 库（幂等，只读消费） |
| POST | `/api/v1/athletes/{id}/reports/{daily\|weekly\|monthly\|quarterly\|yearly}` | 按钮触发生成报告（`?view=coach&refresh=1`） |
| GET | `/api/v1/reports/{report_id}` | 按 ID 读取已生成报告 |
| GET | `/api/v1/athletes/{id}/reports` | 报告列表 |
| GET/PUT | `/api/v1/athletes/{id}/profile` | 运动员档案 |
| GET | `/api/v1/athletes/{id}/sessions` | 训练列表 |
| GET/POST | `/api/v1/athletes/{id}/baseline[/anchor]` | 基线快照 / 建锚点 |
| GET/POST/DELETE | `/api/v1/athletes/{id}/notes` | 备注（软删 + 双视角过滤 + 归属校验） |
| GET | `/api/v1/athletes/{id}/memories` | 历史结论沉淀 |
| GET | `/api/v1/sessions/{session_id}` | 逐箭详情 |
| POST | `/api/v1/ingest/v12` | 接收 v1.2 上行消息（单条对象或数组批量，逐条返回 accepted/duplicate/invalid/unsupported） |

## 数据源

### Mock（开发/演示）

`mock_data/训练数据集_4周_张明.json`：13 场 × 30 箭，自带逐箭心率/风速/MCRT，周均环单调上升。导入即建档（姓名等写入 `athlete_profile`）。

### 真 SQLite（M5，display_sys 只读消费）

字段语义映射（源码摸底 + 探查核实）：

- `ShootingTime/CreateDate/CreateTime` 为**本地时间（Asia/Shanghai）无时区** → 转 UTC 存储（A1）
- 身份证号（18 位，含 X 校验位）→ HMAC 脱敏 `athlete_id`（不可逆，见"安全与隐私"）；真接入时由登录系统下发正式雪花 ID
- 缺测哨兵：`HeartRate=0`、`WindSpeed<0` 不参与匹配；风向缺失记 NULL（不当 0°）
- `Score=0.0` 视为脱靶（命中 = Score>0）；`IsGood` 是"好箭"标记，不是命中
- `ShotType` 1/3/7 语义待开发确认 → `config.store.shot_type_map` 可配（默认 1=试射、3/7=记分）
- `ProjectId → 弓种` 映射可配；未映射项走 `proj{id}` 兜底（不同项目基线不混用）
- 场次聚类：同运动员同日、箭间隔 >90 分钟拆为两场（`session_gap_minutes` 可配）
- 心率 30s / 风速 60s 窗口最近匹配，无采样置 NULL（缺测不出结论）

> 注意：真库 2009-2023 年为早期测试数据（含 41 条 6 位身份号），由 `sqlite_min_time=2024-01-01` 过滤。

### v1.2 上行接口（设备/网关 → 引擎，`POST /api/v1/ingest/v12`）

按 PM 侧《射箭电子靶数据接口 v1.2》对接包接收消息。核心 `app/ingest/v12/receiver.py` 跟传输方式无关：现在接的是 HTTP，以后接 MQTT 订阅时直接调用 `ingest_messages()` 即可。

- **校验**：`app/ingest/v12/schema.json` 是对接包 schema 的逐字节副本（sha256 有测试守护）。在此基础上补了文档里写明、但 schema 没有表达出来的硬约束：时间必须是 UTC 且以 `Z` 结尾；实时流的 `shotId`/`shotSeq` 必须同时为空或同时有值；`subType` 只能用于 dt9。结构不合法的消息直接拒绝，不落库，也不占用 messageId，修正后可以用原 messageId 重发。
- **去重**：`messageId` 精确去重（表 `ingest_messages`，只存内容摘要）。dt2 另外按 `shotId`/`scoreId` 防重，重复的不覆盖。
- **本期分发范围**：dt2 弹着、dt1 心率、dt4 风参与计算；dt3 只存视频路径；dt7 存轨迹元数据和点列，不参与计算；dt5/dt6 只存原文；dt8/dt9 返回 `unsupported`，不落库。
- **字段映射**：`releaseTime` 作为锚点，写入 `shot_time_utc`；`x/y` 由 cm 乘 10 换成 `x_mm/y_mm`；`heartRate=0`、`windSpeed/windDirection/offsetM=-1` 转为 NULL；`bowType` 由 `config.store.v12_bow_type_map` 映射（默认 recurve→反曲弓、compound→复合弓）；`athleteId` 做 HMAC 脱敏。`shot_fact` 新增 `shot_id/score_id/lane/release_time_utc/hit_time_utc/flight_time_ms/inner_ten` 字段。`isGood` 不当作废箭标记使用。
- **场次**：沿用现有口径（同一运动员、同一本地日、两箭间隔 ≤ `session_gap_minutes`），按"运动员 + 本地日"重建。场次号格式为 `{athlete_id}_{YYYYMMDD}_v{nn}`，和 SQLite 源区分开。乱序到达、实时流晚到都会触发重建；弹着快照缺测时，用同 `shotId` 的实时流样本补。

## 口径来源

- 《射箭AI训练报告引擎-工程化方案.md》：D7 窗口/门槛、D4.1 MDC/风档/可比性、D3 API、§五 里程碑
- 《射箭AI训练报告引擎-记忆系统实施方案.md》：§3.1 记忆表、§3.2 基线、§七 里程碑
- 《窗口口径与样本门槛决策记录-v2》：拍板依据与复算附录

`config.json` 中 `timezone/sample_threshold/wind_bands/mdc` 均为文档口径的工程落点；`mdc_source=null` 表示 M4.5 专家共识落地前，报告走降级模板（只描述不判定）。

## 里程碑状态

| 里程碑 | 内容 | 状态 |
| --- | --- | --- |
| M1 | 工程骨架（pyproject/venv/config 严格校验/FastAPI/health） | ✅ |
| A12 | mock 数据重造（390 箭、缺测注入、跨日场次） | ✅ |
| M2 | 事实层：shot_fact/session_dim + 幂等导入 | ✅ |
| M2b | 记忆库：档案/基线快照/备注/结论沉淀 + 双参照系 | ✅ |
| M3 | 指标 + 规则 + 报告生成器 + 缓存 | ✅ |
| M4/M4b | 测试 118 例（指标/规则/窗口/对齐/API/记忆/反硬编码） | ✅ |
| M5 | SQLite 真数据源 + `/ingest/sqlite` + 字段语义核对 | ✅ |
| M6 | 交付：start.bat / README / 端到端验收 | ✅ |
| v1.2 | 上行接口接收适配层 + P0 隐私（ID 脱敏、只监听本机） | 🚧 PR 审阅中 |

## 目录

- `app/`：包根（main=FastAPI 入口、config=口径配置加载、logging_setup；ingest/store/metrics/reports/memory/api）
- `scripts/`：mock 数据生成（gen_mock_data.py）、口径证据复算（verify_evidence.py）、真库探针（probe_real_db.py）
- `mock_data/`：A12 重造后的数据集（与 MVP `mock数据\` 分离）
- `tests/`：pytest 测试（conftest 提供隔离配置/临时库/种子助手）
