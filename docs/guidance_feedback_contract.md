# 指导反馈与复审

## 提交评分

`POST /api/v1/athletes/{athlete_id}/guidance/feedback`

```json
{
  "report_id": "<已保存报告的 ID>",
  "granularity": "daily",
  "window_key": "daily:day:2026-11-26",
  "view": "coach",
  "rule_pack_version": "v1-trial-20261010",
  "source": "rule",
  "rating": 4,
  "will_follow": true,
  "reason": "安排可执行",
  "rules": ["advice_inner"],
  "actor_id": "desktop:coach01",
  "submitted_at_utc": "2026-11-26T03:00:00.000Z"
}
```

成功返回 HTTP 200 和 `{"id":"<feedback-id>","accepted":true}`。同一报告和评分人再次提交会覆盖评分并沿用 ID。报告必须仍可读取，且所属运动员、粒度、窗口须与请求匹配。日报的 `YYYY-MM-DD` 可作为日历窗口键简写，存储使用报告的完整键。报告不存在或不匹配返回 404。

评分为 1–5 的整数，`will_follow` 为布尔值，`source` 为 `rule` 或 `model`。规则包版本须与报告绑定的版本一致，未绑定旧报告使用空字符串。规则列表可为空，重复条目去重，未知于该报告的规则返回 422。时间须带时区，统一存成毫秒精度的 UTC。

原因可省略，限一行和 120 字。可识别的身份证号、手机号、运动员姓名和视频路径会被拒绝。评分人使用桌面提供的技术标识，桌面按账号权限控制评分入口。引擎允许教练在两种报告视角之间切换后提交，`view` 不参与重复评分识别。

评分、规则命中和报告分别存储。反馈不写回报告。报告缓存失效或同窗重新生成后，已经记录的命中和评分仍纳入历史统计。升级时会从库中尚存的报告正文补录命中。

## 管理员统计

`GET /api/v1/guidance/feedback/summary?from=2026-10-01&to=2026-11-26`

使用 `Authorization: Bearer <ENGINE_RULE_PACK_ADMIN_TOKEN>`，与规则包发布共用权限。日期为 UTC 自然日，两端均包含。`to` 默认当天，`from` 默认从结束日往前算 90 日（含结束日）。两端都可显式指定，开始日期晚于结束日期返回 422。

| 字段 | 口径 |
| --- | --- |
| `total`、`avg_rating`、`will_follow_rate` | 期间提交的有效评分数、均分、会照做比例 |
| `rules[].hits` | 期间生成的报告中，该条规则命中的报告数。每份报告每条规则计一次 |
| `rules[].rated`、`avg_rating`、`will_follow_rate` | 对这些命中报告在期间提交并关联该规则的评分次数、均分、会照做比例。同一报告可由多位教练评分 |
| `rules[].low_streak`、`in_review` | 各规则包版本分别计算连续低分报告数，规则汇总采用其中最大值 |
| `low_reason_distribution` | 期间所有 1–2 分评分的原因频次，空原因用空字符串键 |
| `review_queue` | 同一规则、同一版本按提交时间倒序，遇到高于 2 分时中断。累计两份不同报告的连续低分才入队，附最近两份样本和这两份的均分 |

无评分时均分及会照做率返回 `null`。没有命中或复审记录时，相应数组为空。

## 解读门槛

非流式解读的 `quality_gate` 检查引用、下一步正文和数字一致性。任意项失败时，返回 `source=rule` 和 `degraded_reason=quality_failed`。具体字段见[非流式报告解读契约](guidance_interpret_contract.md)。
