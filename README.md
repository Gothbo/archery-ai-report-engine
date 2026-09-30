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

脚本自动完成：创建 `.venv`（缺省）→ 按 `pyproject.toml` 安装依赖（缺省）→ 启动 `http://127.0.0.1:8000`。

### 手动命令

```powershell
# 启动
.venv\Scripts\python -m uvicorn app.main:app --port 8000
# 测试（118 用例：指标/规则/窗口/对齐/库/API/反硬编码）
.venv\Scripts\python -m pytest -q
# 服务自检
.venv\Scripts\python -c "import app.main; print('ok')"
```

### 环境要求

- Python 3.10+（Windows 下时区库 `tzdata` 已入依赖）
- 首次导入真库前，按环境调整 `config.json → store.sqlite_source_path`

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

## 数据源

### Mock（开发/演示）

`mock_data/训练数据集_4周_张明.json`：13 场 × 30 箭，自带逐箭心率/风速/MCRT，周均环单调上升。导入即建档（姓名等写入 `athlete_profile`）。

### 真 SQLite（M5，display_sys 只读消费）

字段语义映射（源码摸底 + 探查核实）：

- `ShootingTime/CreateDate/CreateTime` 为**本地时间（Asia/Shanghai）无时区** → 转 UTC 存储（A1）
- 身份证号（18 位，含 X 校验位）→ 确定性雪花格式 `athlete_id`；真接入时由登录系统下发正式雪花 ID
- `Score=0.0` 视为脱靶（命中 = Score>0）；`IsGood` 是"好箭"标记，不是命中
- `ShotType` 1/3/7 语义待开发确认 → `config.store.shot_type_map` 可配（默认 1=试射、3/7=记分）
- `ProjectId → 弓种` 映射可配；未映射项走 `proj{id}` 兜底（不同项目基线不混用）
- 场次聚类：同运动员同日、箭间隔 >90 分钟拆为两场（`session_gap_minutes` 可配）
- 心率 30s / 风速 60s 窗口最近匹配，无采样置 NULL（缺测不出结论）

> 注意：真库 2009-2023 年为早期测试数据（含 41 条 6 位身份号），由 `sqlite_min_time=2024-01-01` 过滤。

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

## 目录

- `app/`：包根（main=FastAPI 入口、config=口径配置加载、logging_setup；ingest/store/metrics/reports/memory/api）
- `scripts/`：mock 数据生成（gen_mock_data.py）、口径证据复算（verify_evidence.py）、真库探针（probe_real_db.py）
- `mock_data/`：A12 重造后的数据集（与 MVP `mock数据\` 分离）
- `tests/`：pytest 测试（conftest 提供隔离配置/临时库/种子助手）
