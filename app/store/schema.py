# -*- coding: utf-8 -*-
"""事实层 + 记忆层 Schema（SQLite DDL）。

依据：工程化方案 D1（shot_fact/session_dim）+ 记忆系统方案 §3.1（4 张记忆表）。
缺测一律 NULL；基线快照单表承载 rolling/anchor（snap_type 区分，不建独立锚点表）。
"""
from __future__ import annotations

SCHEMA_DDL = """
-- 训练维度表（C4 可比性校验以本表为准）
CREATE TABLE IF NOT EXISTS session_dim (
    session_id        TEXT PRIMARY KEY,
    athlete_id        TEXT NOT NULL,
    session_time_utc  TEXT NOT NULL,          -- 训练时间（窗口对齐与跨场比较）
    distance_m        INTEGER NOT NULL,       -- 距离
    shot_count        INTEGER NOT NULL,       -- 箭数
    mode_composition  TEXT NOT NULL,          -- 构成（如"记分36+试射6"）
    avg_wind          REAL,                   -- 平均风况
    wind_stddev       REAL,
    site              TEXT
);

-- 逐箭事实表（每箭一行；弹着 + 心率 + 风速 + 轨迹预留）
CREATE TABLE IF NOT EXISTS shot_fact (
    fact_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id      TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    shot_seq        INTEGER NOT NULL,
    score           REAL NOT NULL,            -- 环值（0.0=脱靶，保留）
    hit             INTEGER NOT NULL,         -- 是否命中
    x_mm            REAL,                     -- 弹着坐标
    y_mm            REAL,
    mcr_t           REAL,                     -- 撒放用时（缺测 NULL）
    hr              INTEGER,                  -- 心率（缺测 NULL）
    wind_speed      REAL,                     -- 风速（缺测 NULL）
    wind_dir_deg    REAL,                     -- 风向（缺测 NULL）
    shooting_mode   INTEGER NOT NULL DEFAULT 1,  -- 0试射/1记分/2同分/3补射
    bow_type        TEXT NOT NULL,            -- 弓种（C3 基线键组成部分）
    video_ref       TEXT,                     -- scoreId→本地视频（M7 预留）
    shot_time_utc   TEXT NOT NULL,            -- UTC 毫秒（ISO8601）
    -- v1.2 接口来源字段（SQLite/mock 源为 NULL；旧库由 Database._ADD_COLUMNS 补列）
    shot_id         TEXT,                     -- dt2.shotId（幂等键，19 位雪花 ID 字符串）
    score_id        TEXT,                     -- dt2.scoreId
    lane            TEXT,                     -- 靶位
    release_time_utc TEXT,                    -- 离弦（锚点）
    hit_time_utc    TEXT,                     -- 中靶
    flight_time_ms  INTEGER,                  -- 飞行时间 ms
    inner_ten       INTEGER,                  -- X 环（1/0；未知 NULL）
    UNIQUE(session_id, shot_seq)              -- 幂等
);
CREATE INDEX IF NOT EXISTS idx_shot_athlete_time ON shot_fact(athlete_id, shot_time_utc);
CREATE INDEX IF NOT EXISTS idx_shot_athlete_mode ON shot_fact(athlete_id, shooting_mode, bow_type, shot_time_utc);

-- 静态档案（1 运动员 1 行，upsert）
CREATE TABLE IF NOT EXISTS athlete_profile (
    athlete_id    TEXT PRIMARY KEY,
    account_id    TEXT,                       -- 预留：登录系统账号绑定（可空）
    identity_id   TEXT,                       -- 已弃用（P0 隐私）：不再写入身份证号，恒为 NULL；旧库由迁移脚本清空
    name          TEXT,
    gender        TEXT,
    age           INTEGER,
    bow_type      TEXT,
    hand          TEXT,
    level         TEXT,
    updated_at_utc TEXT
);

-- 基线快照（滚动 + 锚点两类，均按 9B 口径；锚点不建独立表）
CREATE TABLE IF NOT EXISTS baseline_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id    TEXT NOT NULL,
    bow_type      TEXT NOT NULL,              -- 基线键组成部分（换弓种重算）
    snap_type     TEXT NOT NULL CHECK (snap_type IN ('rolling', 'anchor')),
    n_shots       INTEGER NOT NULL,
    avg_score     REAL,
    mcr_t         REAL,
    hr_volatility REAL,
    dispersion_mm REAL,
    source_session_id TEXT,                   -- 锚点可比性追溯；滚动可为空
    distance_m    INTEGER,                    -- 可比性校验用；锚点必填，滚动可为空
    mode_composition TEXT,
    avg_wind      REAL,
    collected_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snap_lookup ON baseline_snapshots(athlete_id, bow_type, snap_type, collected_at_utc);

-- 人工备注（伤病/目标/教练观察；软删 status→closed，保留审计）
-- B1-5 预留记忆写入闭环字段：provenance（来源：conversation/report/manual）、
-- quote_text（原始引文）、confirmed_at_utc（确认时间）——本轮只加字段不落库，
-- 确认写入闭环 B2 实现（D4：正式记忆必须人工确认后才生效）
CREATE TABLE IF NOT EXISTS memory_notes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id    TEXT NOT NULL,
    note_type     TEXT NOT NULL CHECK (note_type IN ('injury', 'goal', 'coach_note', 'other')),
    content       TEXT NOT NULL,
    author_role   TEXT NOT NULL CHECK (author_role IN ('athlete', 'coach')),
    actor_id      TEXT NOT NULL,              -- 录入人（雪花 ID，归属校验 + 审计）
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
    created_at_utc TEXT NOT NULL,
    closed_at_utc  TEXT,
    provenance    TEXT,                       -- B1-5：来源 conversation | report | manual（NULL=旧数据/manual）
    quote_text    TEXT,                       -- B1-5：原始引文（conversation 确认闭环用）
    confirmed_at_utc TEXT                    -- B1-5：正式记忆确认时间（NULL=未确认）
);
CREATE INDEX IF NOT EXISTS idx_notes_lookup ON memory_notes(athlete_id, note_type, status, created_at_utc);

-- 历史结论沉淀（报告生成时自动写入；judgement 类供历史引用，attribution 类仅随报告落库）
CREATE TABLE IF NOT EXISTS report_memories (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    athlete_id    TEXT NOT NULL,
    report_id     TEXT NOT NULL,
    granularity   TEXT NOT NULL CHECK (granularity IN ('daily', 'weekly', 'monthly', 'quarterly', 'yearly')),
    conclusion_type TEXT NOT NULL CHECK (conclusion_type IN ('judgement', 'attribution')),
    conclusion_key TEXT NOT NULL,             -- plateau | progress | regression | risk | wind...
    conclusion    TEXT NOT NULL,              -- 结论文案（带证据摘要）
    judge_basis   TEXT,                       -- 判定依据：锚点 id+值+mdc_source 版本+窗口箭数
    delta_value   REAL,                       -- 判定差值（锚点触发④依赖此字段；缺测判 NULL）
    evidence      TEXT,                       -- 证据结构（{type, ref, value}）
    generated_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_lookup ON report_memories(athlete_id, granularity, generated_at_utc);

-- 报告缓存（按钮触发生成后落盘；B10 失效规则反查）
CREATE TABLE IF NOT EXISTS report_cache (
    report_id        TEXT PRIMARY KEY,
    athlete_id       TEXT NOT NULL,
    granularity      TEXT NOT NULL,
    window_key       TEXT NOT NULL,           -- 如 weekly:2026-W36 / daily:S001
    mdc_version      TEXT,                    -- config mdc_source 版本（降级切判定后旧缓存不串版本）
    generated_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_lookup ON report_cache(athlete_id, generated_at_utc);

-- ===== v1.2 上行接口接收适配层（app/ingest/v12）=====
-- 已受理消息（按 messageId 精确去重，§4.3）；结构非法/本期不支持的消息不入此表（可修正后原 messageId 重发）
CREATE TABLE IF NOT EXISTS ingest_messages (
    message_id      TEXT PRIMARY KEY,
    data_type       INTEGER NOT NULL,
    payload_sha256  TEXT NOT NULL,            -- 只存摘要（不存原文，避免身份字段落库）；重复投递内容不同可识别
    received_at_utc TEXT NOT NULL
);

-- dt2 弹着（接收原始事实，已做单位换算/哨兵转 NULL/身份脱敏；shot_fact 由此按运动员+本地日重建）
CREATE TABLE IF NOT EXISTS v12_shot (
    shot_id          TEXT PRIMARY KEY,        -- 幂等键（同 shotId 不覆盖）
    score_id         TEXT UNIQUE,
    message_id       TEXT NOT NULL,
    athlete_id       TEXT NOT NULL,           -- athleteId 经 HMAC 脱敏
    lane             TEXT,
    shot_seq         INTEGER,                 -- 源 shotSeq（会话内递增；shot_fact 内序号另行编号）
    shot_time_utc    TEXT NOT NULL,           -- = releaseTime（全局锚点）
    local_date       TEXT NOT NULL,           -- config.timezone 下的本地日 YYYYMMDD（场次聚类键）
    release_time_utc TEXT,
    hit_time_utc     TEXT,
    flight_time_ms   INTEGER,
    score            REAL NOT NULL,
    inner_ten        INTEGER,
    x_mm             REAL,                    -- 源 x(cm) × 10
    y_mm             REAL,
    shooting_mode    INTEGER NOT NULL,
    bow_type         TEXT NOT NULL,           -- 内部弓种值（反曲弓/复合弓）
    target_type      TEXT,
    hr               INTEGER,                 -- 快照；0 → NULL
    wind_speed       REAL,                    -- 快照；-1 → NULL
    wind_dir_deg     REAL,
    offset_m         REAL,
    received_at_utc  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_v12_shot_day ON v12_shot(athlete_id, local_date);

-- dt1 心率 / dt4 风（实时流样本；带 shotId 的用于弹着快照缺测时的兜底归属，null 为背景流）
CREATE TABLE IF NOT EXISTS v12_sample (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      TEXT NOT NULL,
    data_type       INTEGER NOT NULL CHECK (data_type IN (1, 4)),
    sample_time_utc TEXT NOT NULL,
    shot_id         TEXT,
    shot_seq        INTEGER,
    lane            TEXT,
    hr              INTEGER,                  -- dt1；0 → NULL
    rr_intervals_ms TEXT,                     -- dt1 R-R 列表（JSON）
    rmssd_ms        REAL,
    wind_speed      REAL,                     -- dt4；-1 → NULL
    wind_dir_deg    REAL,
    temp_c          REAL,
    humidity_pct    REAL
);
CREATE INDEX IF NOT EXISTS idx_v12_sample_shot ON v12_sample(shot_id, data_type);

-- dt3 视频引用（只存路径，监控路不纳入分析）
CREATE TABLE IF NOT EXISTS v12_video (
    shot_id          TEXT NOT NULL,
    source           TEXT NOT NULL,           -- top / side
    video_path       TEXT NOT NULL,
    message_id       TEXT NOT NULL,
    release_time_utc TEXT,
    shot_seq         INTEGER,
    lane             TEXT,
    PRIMARY KEY (shot_id, source)
);

-- dt7 瞄准轨迹（元数据 + 原始点列；本期不参与计算）
CREATE TABLE IF NOT EXISTS v12_trajectory (
    shot_id              TEXT PRIMARY KEY,
    message_id           TEXT NOT NULL,
    release_time_utc     TEXT,
    shot_seq             INTEGER,
    sample_rate_hz       REAL,
    point_count          INTEGER,
    trajectory_length_mm REAL,
    dispersion_mm        REAL,
    offset_x_mm          REAL,
    offset_y_mm          REAL,
    post_hold_ms         INTEGER,
    stability_score      REAL,
    points_json          TEXT
);

-- dt5 拉力 / dt6 撒放时序：本期只校验 + 存原文（开发授权 PM 侧 mock 格式），不参与计算
CREATE TABLE IF NOT EXISTS v12_raw_message (
    message_id       TEXT PRIMARY KEY,
    data_type        INTEGER NOT NULL,
    shot_id          TEXT,
    release_time_utc TEXT,
    payload_json     TEXT NOT NULL
);
"""
