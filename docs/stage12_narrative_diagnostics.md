# Stage 12 — 叙事诊断：模型看到了什么，为什么这样说

## 问题

行动链路已经把叙事和权威状态分开了：模型只读已提交的结果，写不出物品、开不了门。但当模型说了一句意外的话，之前没有任何办法回答三个最要紧的问题：它拿到了哪些上下文、用的是哪个模型和什么采样参数、它失败或拒绝时到底发生了什么。`narrative.failed` 只存了一句错误文本，上下文只留了 id 列表。

## 这一阶段做了什么

叙事调用现在会带一份 `NarrativeDiagnostics`，和结果一起返回、一起存进 `narrator.response` 事件的 payload。它是**只读的旁路信息**：不影响提交、不进入提示词、任何一个字段异常都不会让回合失败。

### 数据模型（`backend/app/models/game.py`）

- `NarrativeBlock`：`id` / `layer` / `title` / `excerpt`（最多 160 字符，压缩空白）。
- `NarrativeDiagnostics`：`provider`、`model`、`temperature`、`max_tokens`、`timeout`、`language`、`content_policy`、`preset_routed`、`skipped`、`blocks`、`fact_counts`。
- `NarrativeResult.diagnostics`：`Optional[NarrativeDiagnostics]`，旧调用方不受影响。

### 采集（`backend/app/services/narrative_service.py`）

`capture_blocks(knowledge)` 和 `capture_diagnostics(knowledge, options, ...)` 把“即将发出去的东西”整理成可读记录：

- provider/model：场景预设一旦覆盖（对话槽位、剧情槽位），记录的是**实际路由到的**那一个（`preset_routed=True`）。
- temperature / max_tokens：记录实际发送值，没覆盖时回落 narrators 的默认值。
- content_policy：记录**最终**生效的策略，所以被上游拒绝后改走本地的那一次会显示 `nsfw`。
- `LLMNarrator`：成功、超时、异常、空文本四种出口都带上这份记录；`TemplateNarrator` 标 `skipped=True, provider='template'`，让调试面板能区分“没用模型”和“用了但没记录”。

### 写入（`backend/app/services/action_service.py`）

- `narrator.response` 事件 payload 新增 `diagnostics`（空 capture 不写，避免整行 null）。
- 叙事失败回退到 `render_fallback` 时，仍然保留模型那次的 capture：`error` 说明为什么被拒，`diagnostics` 说明它看到了什么。
- `_diagnostics_payload()` 负责序列化，块摘要是唯一还能解释“为什么它那么说”的现场记录。

## 为什么摘要是 160 字符

调试面板要回答的是“这段知识有没有进上下文”“进来的是哪一条”。完整正文留在场景库里，按 `id` 就能取回；把整段塞进每回合的事件行只会让事件表膨胀。

## 测试

- `backend/tests/services/test_narrative_diagnostics.py`（6 项）：块裁剪与空白压缩、预设覆盖优先与非法值回落、实际路由记录、失败仍保留 capture、拒绝改走本地后记录 `nsfw`、模板叙事标记为本地。
- `backend/tests/api/test_narrative_diagnostics_api.py`（1 项）：真实建世界 → 绑战役 → 走一次行动 → HTTP 响应里有 `narrative.diagnostics`，且 `narrator.response` 事件落库的 payload 里也有同一份记录。

## 未做

- 前端 `MemoryInspector` 还没有把这份诊断画出来（数据已经在 API 里）。
- `fact_counts` 目前只在显式传入时填充，行动链路尚未统计。
- 上下文正文的按需回取接口未做，面板目前只显示摘要。

## 本地验证

```bash
cd backend
TEMP='E:/shujia/.tmp' TMP='E:/shujia/.tmp' .venv/Scripts/python.exe -m pytest tests/services/test_narrative_diagnostics.py tests/api/test_narrative_diagnostics_api.py -q -p no:cacheprovider
```

手动验证：跑一个真实回合后 `GET /api/v1/campaigns/{id}/events`，看 `narrator.response` 里 `diagnostics.provider/model/content_policy/blocks`。
