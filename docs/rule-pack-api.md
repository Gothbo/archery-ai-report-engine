# 版本化判定规则包

规则包保存在引擎 SQLite 中。规则包只配置阈值、风档、最小样本和建议文字。指标方向、计算公式、判定流程、报告段落和安全护栏由引擎代码控制。

## 默认包

引擎首次打开数据库时会写入 `v1-trial-20261010`，重复打开不会覆盖。该包沿用原有 MDC、TE、SWC、风档、最小样本和建议模板，记录现有配置口径作为来源。迁移记录的生效时间为 2026-10-10，首次复审日期为 2027-01-10。状态为 `trial`，签署人和签署时间为空。当前配置选用该试行口径时，默认包自动生效。其他显式旧配置继续按原配置出报告，直到该数据库发布规则包。

## 管理接口

| 操作 | 路径 | 请求与返回 |
| --- | --- | --- |
| 列出版本 | `GET /api/v1/rule-packs` | 返回 `active_version` 和 `packs` |
| 读取版本 | `GET /api/v1/rule-packs/{version}` | 返回规则、来源、修改人和发布签署状态 |
| 保存草稿 | `PUT /api/v1/rule-packs/{version}` | 提交 `actor_id`、`rules`、`sources`、`review_date`，返回草稿 |
| 发布 | `POST /api/v1/rule-packs/{version}/publish` | 提交 `actor_id`，草稿立即生效为试行版本 |
| 签署 | `POST /api/v1/rule-packs/{version}/sign` | 提交 `actor_id`，登记试行版本的签署人和时间 |
| 读取审计 | `GET /api/v1/rule-packs/{version}/audit` | 返回 `events`，每项含 `version`、`action`、`actor_id`、`occurred_at_utc` |

规则包状态为 `draft`、`trial` 或 `signed`，分别对应草稿、试行、已签署。发布后的数值、来源和文案固定。编辑已发布的内容时，创建新版本。签署只更新签署状态和签署元数据，也可签署已被新版本替代的试行版本。重复发布或签署返回 HTTP 409，审计中保留首次成功操作。

规则包记录含 `version`、`status`、`rules`、`sources`、`review_date`、`updated_by`、`updated_at_utc`、`effective_at_utc`、`signed_by`、`signed_at_utc`。日期格式为 `YYYY-MM-DD`，时间为 UTC。来源数组的元素含非空 `title` 与 `reference`。

编辑器可将默认包的 `rules` 作为新草稿起点。提交完整规则，包含以下字段。

| 字段 | 规则 |
| --- | --- |
| `mdc` | 必须包含 `avgScore`、`mcrT`、`hrVolatility`、`dispersionMm`。允许非负的 `threshold`、`te`、`swc` 和按 `max` 升序的 `bands`。缺少阈值以 `null` 表示 |
| `wind_bands` | 非空。风档以 `[下限, 上限]` 表示，按顺序连续且无重叠 |
| `min_wind_band_shots` | 每个风档参与建议比较所需的最小箭数，至少 1 |
| `wind_gap_split_mps`、`min_wind_gap_shots` | 两档风差结论的风速分界与每侧最小箭数，分别大于 0 和至少 1 |
| `sample_threshold` | `daily`、`weekly`、`monthly`、`quarterly`、`yearly` 均为正整数 |
| `advice_templates` | 使用默认包的模板键和各模板已有的简单占位符，例如 `{inner}`。占位符对应已格式化的证据值，建议模板不声明签署状态 |

保存和发布会校验规则。非法值返回 HTTP 422 和原因。找不到版本返回 404，当前版本状态不允许请求操作时返回 409。

草稿和发布接口使用 `ENGINE_RULE_PACK_ADMIN_TOKEN`，签署接口使用单独的 `ENGINE_RULE_PACK_SIGNER_TOKEN`。调用受保护操作时发送 `Authorization: Bearer <token>`。未配置凭据、未提供凭据、凭据错误分别返回 503、401、403。桌面宿主在发送请求前校验已登录账号权限，由授权工具传入审计用 `actor_id`。凭据由部署方配置，不写入规则包或报告。第一版桌面编辑器只需接管理员发布能力。

## 报告和指导

- 新生成的报告在数据库中绑定 `rule_pack_version`。历史未绑定报告的列值为 `null`，报告列表保持原有返回结构。
- 报告正文和其中的 `rule_guidance.rule_pack` 返回同一套元数据。流式训练指导的终态 `final` 和 `guardrail_failed` 也返回所引用报告的 `rule_pack`。
- `rule_pack.version` 和 `rule_pack.status` 是生成时的版本与状态。当前签署徽标读取 `current_status`，并可展示 `signed_by` 和 `signed_at_utc`。生成时的版本、状态、来源、复审日期和正文保持原样。签署不更新历史报告行。
- 发布新版本后，生成接口使用新规则。旧版本的报告仍可按原报告 ID 查看。场次数据变更导致的报告失效继续生效。规则包上线前的报告和显式旧配置生成的报告沿用旧正文，没有生成时规则包元数据。
- 趋势判定历史按版本分开。报告的 `comparison_note` 提示用户跨版本判定不可直接比较。
