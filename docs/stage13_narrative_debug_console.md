# 阶段 13：叙事诊断闭环与调试台

本阶段收尾需求 ④（LLM 叙事/知识/记忆的可诊断性）与需求 ⑤ 的调试台部分。

## 1. 为什么先做诊断而不是先调质量

叙事层的问题几乎都是「模型到底看到了什么」：一句突兀的对白，可能是上下文没进来、可能是采样温度被预设覆盖、也可能是上游拒绝后改走了本地模型。这三件事在原来的实现里都查不到：`narrator.response` 只存了文本和模型名，`narrative.failed` 只存了错误字符串。

所以本阶段把叙事调用做成可读、可回放的数据，再看前端如何呈现。

## 2. 后端改动

### 2.1 上下文来源计数 `fact_counts`

`backend/app/services/narrative_service.py` 新增 `count_context_kinds(knowledge)`：按块的 `kind`（没有则用 `layer`）计数，块不是映射或无来源名则跳过。

`capture_diagnostics` 的 `fact_counts` 参数改为「显式传入优先，否则由上下文自动统计」。原来这个字段恒为 `{}`，等于面板上永远显示「无上下文」，无法区分「真没给上下文」和「给了 6 段但没记」。

### 2.2 失败路径记录最终生效的内容策略

`LLMNarrator.narrate` 原来只有成功出口用 `model_copy(update={'content_policy': policy})` 记录最终策略，三个失败出口（超时、异常、空文本）用的是调用前捕获的 `diagnostics`，`content_policy` 停留在计划值。

现在三个失败出口统一写 `diagnostics.model_copy(update={'content_policy': policy})`。理由：上游拒绝后改走本地本地又超时，正是最需要看清一条「实际走了 nsfw」的回合。

### 2.3 `narrative.failed` 事件带上诊断

`backend/app/services/action_service.py` 的 `_apply` 在写 `narrative.failed` 时增加 `diagnostics: _diagnostics_payload(narrative)`。失败回合最需要这份捕获：路由、采样、上下文都在里面。

另外 `_narrate_applied` 的兜底 `except` 原来返回不带 diagnostics 的失败结果（模型调用都没发出去时），现在用 `capture_diagnostics(knowledge, options)` 生成一份并标 `skipped=True`，面板上显示为「未调用模型」，而不是整块空白。

### 2.4 行动记录索引

`backend/app/db/game_state_store.py` 新增 `list_requests(campaign_id, limit=50, after_version=0)`：从 `action_requests` 表按 rowid 倒序取 `request_id/campaign_id/status/state_version/created_at`。

`backend/app/api/routes_engine.py` 新增 `RequestSummaryView` 与 `GET /api/v1/campaigns/{campaign_id}/requests`。原来只能按 `request_id` 单条取（`GET /actions/{request_id}`），不知道有哪些 id；被拒绝的回合也在这张表里，所以调试台能看到「没发生的事」及其拒绝码。

## 3. 前端改动

`frontend/src/components/DevtoolsPanel.jsx` 从单页签改为三个页签：

- **调用追踪**：原有 `trace` 视图，不变。
- **叙事与上下文**：读 `GET /api/v1/campaigns/{id}/events`，筛出 `narrator.response` / `narrative.failed` 倒序展示。每张卡显示状态（失败为红框）、模型、版本号、错误（降级时标「已降级：」）、路由/预设路由/未调用模型/内容策略、温度与 `max_tokens` 与超时与语言、`fact_counts` 的来源分布（如「世界×3 · 历史×1」），并可展开「模型看到的 N 段上下文」（层、标题、160 字摘要）。
- **行动记录**：读新接口，列出每回合的 request_id、状态徽章（applied/rejected/duplicate/conflict）、版本、时间；点一行再用 `GET /actions/{request_id}` 展开完整请求 JSON、结果状态码与拒绝码、`effects_applied`。

`frontend/src/api.js` 新增 `fetchDomainEvents`、`fetchCampaignRequests`、`fetchActionRecord` 与 `narrativeDiagnosticsOf`。

`frontend/src/components/GameCanvas.jsx` 把 `activeCampaignId` 与三个读取函数传入 `DevtoolsPanel`，面板自行按页签加载，不需要再在画布层维护状态。

## 4. 设计取舍

- **诊断只读**：`diagnostics` 不进提示词、不参与提交，任何字段异常都不让回合失败（`capture_diagnostics` 对脏值一律回落空串/默认值）。
- **摘要而非正文**：块只留 160 字（`BLOCK_EXCERPT_CHARS`），完整正文仍在场景库按 id 可取，避免每个回合的事件行携带整篇设定。
- **复用既有事件表**：没有为叙事新开表，诊断跟着 `narrator.response`/`narrative.failed` 一起进 `domain_events`，所以重启后仍可查看，且与状态版本天然对齐。
- **调试台读接口而非推流**：SSE 的 `[TRACE]` 仍只服务调用追踪；叙事与行动记录按需拉取，避免把诊断塞进玩家可见的流。

## 5. 测试

新增/扩充：

- `backend/tests/services/test_narrative_diagnostics.py`：+4 项。`capture_counts_the_context_sources`（按 kind/layer 计数、非法块跳过）、`capture_accepts_explicit_fact_counts`、`narrator_timeout_records_the_answering_policy`、`narrator_empty_text_records_the_answering_policy`。
- `backend/tests/api/test_narrative_diagnostics_api.py`：+1 项。`test_a_failed_narration_row_carries_the_capture` 用一个固定失败的叙事器走完整 HTTP 行动，断言回合仍 `applied`，且 `narrative.failed` 行带着 provider / content_policy / fact_counts / blocks。
- `backend/tests/api/test_routes_contracts.py`：TYPED_PATHS 增加 `GET /campaigns/{campaign_id}/requests`；新增 `test_campaign_requests_list_the_recorded_turns`（应用与被拒两种回合都能列出，且 id 能打开详情）。

结果：

- 相关套件 17 → 22 项全过；契约测试 8 项全过。
- 全量后端：**1447 passed, 8 skipped**（128.76s）。
- 前端：本沙箱 `vite build` 不可用（`Cannot find module E:\shujia\.dsh-tmp\config-module.mjs`），用 esbuild 逐文件语法检查 api.js / store.js / App.jsx / GameCanvas.jsx / DevtoolsPanel.jsx / WorldStudio.jsx 全过，检查脚本已删除。**前端仍未经过真实构建。**

## 6. 仍未完成

- 没有为诊断做独立的版本化命名空间迁移（字段随 `NarrativeDiagnostics` 演进，旧行缺字段时前端按可选处理）。
- 上下文块只存摘要，面板不能点开原文；原文需另查 `GET /api/v1/scenarios/{id}/context/preview` 或知识库。
- 未做「按模型/按回合聚合的失败率」统计视图。
- 旧 `routes_game` 链路的 `[ITEM_ADD:…]` 等文本标记仍在 `frontend/src/api.js` 里被解析，属 m00001 要求迁移的历史机制，本阶段未动。

## 7. 本地验证

```bash
cd backend
TEMP='E:/shujia/.tmp' TMP='E:/shujia/.tmp' .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider \
  tests/services/test_narrative_diagnostics.py tests/api/test_narrative_diagnostics_api.py \
  tests/api/test_routes_contracts.py
```

手工验证：装一个场景→绑定战役→执行一个行动→打开「LLM 调试台」→「叙事与上下文」应看到一张带模型与上下文来源的卡片；「行动记录」应列出该 `request_id`，点开后能看到请求与结果。
