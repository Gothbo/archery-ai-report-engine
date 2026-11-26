# 非流式报告解读

`POST /api/v1/athletes/{athlete_id}/guidance/interpret` 读取一份已保存报告，在请求结束时返回规则指导或模型解读。调用不会生成、重算或写回报告。

## 请求

```json
{
  "granularity": "weekly",
  "window_key": "2026-W47",
  "view": "athlete",
  "report_id": "已保存的报告 ID"
}
```

`granularity` 接受 `daily`、`weekly`、`monthly`、`quarterly`、`yearly`。窗口键使用报告正文的 `window_key`。日报支持单场 `daily:<session_id>` 和日历 `daily:day:YYYY-MM-DD`。省略 `report_id` 时，读取该运动员和窗口最近保存且已有正文的报告。`view` 默认 `athlete`，也支持 `coach`，桌面应按当前账号权限传入。

非法粒度、窗口格式和视角返回 400。请求缺少必填字段或字段类型错误时，沿用引擎的 422 校验响应。运动员不存在、窗口没有已保存正文，或报告 ID 与运动员及窗口不匹配时返回 404。

## 响应

| 字段 | 内容 |
| --- | --- |
| `source` | `model` 表示模型输出已通过护栏，`rule` 表示使用规则指导 |
| `degraded_reason` | 模型成功为 `null`。规则兜底给出下表的原因 |
| `text` | 完整模型解读，或规则摘要与每条建议按行拼接。规则兜底始终非空 |
| `sections` | 能解析时返回五段 `{heading,text}`。标题解析失败时保留通过护栏的完整 `text`，`sections` 返回空数组。规则兜底也返回空数组 |
| `references` | 本次输入的来源清单，每项含 `type`、`ref`、`title` |
| `rule_guidance` | 所引用报告的确定性指导，含 `source: "rule"`、`degraded_reason: null` |
| `rule_pack` | 所引用报告的版本、状态和来源等元数据。旧报告未绑定规则包时为 `null` |
| `context_version` | 本次模型输入的指纹。快照和可见引用保持一致时，重复调用保持一致 |
| `model_version` | 模型成功时返回配置中的版本，规则兜底为 `null` |
| `generated_at_utc` | 本次解读的 UTC 时间，精确到毫秒 |

报告生成和报告正文读取均返回带来源标记的 `rule_guidance`。历史报告的标记可在读取时补齐，存储内容保持原样。

## 降级

有效报告的模型降级统一返回 HTTP 200。

| 原因 | 含义 |
| --- | --- |
| `disabled` | `llm.enabled=false` |
| `busy` | 问答或指导已占用同一个模型任务位 |
| `timeout` | 模型调用、首个片段、片段间等待或总生成时间超时 |
| `guardrail_failed` | 输出命中禁语、超长、含输入外数字、含医疗建议，或加入报告没有支持的判定 |
| `empty` | 上游未返回可显示文字 |
| `error` | 其他模型异常，或生成过程中可见引用发生变化 |

模型返回后，服务重新核对报告和可见引用。报告在模型生成期间失效时返回 404。引用发生变化时按当前可见范围返回规则指导。现有 `guidance/stream` 保留 SSE 事件和 503、409 行为。

## 引用与模型输入

| 类型 | 引用格式 |
| --- | --- |
| `metric` | `metric:avgScore`、`metric:mcrT`、`metric:dispersionMm`、`metric:hrVolatility`。只列本报告已有数值的指标 |
| `window` | `window:<granularity>:<window_key>` |
| `note` | `note:<note_id>`。只列该视角选中的可见备注 |
| `rule` | `rule:<source>`。来自规则指导条目的 `source` |
| `baseline` | `baseline:rolling`。仅有可用基线且视角允许时返回 |
| `memory` | `memory:<id>`。来自可引用的历史结论记录 |

本接口复用流式指导的模型、提示词、护栏与超时机制。模型接收报告正文、指标、判定、规则包版本和当前视角可见的证据。备注、历史结论及建议模板的自由文本可能夹带个人信息，模型只接收这些内容的类型或来源引用。运动员视角也会排除教练附注和专属证据。报告中的确定性指导仍按原有权限返回给桌面展示。
