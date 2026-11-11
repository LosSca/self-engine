# 内容策略与本地模型路由（NSFW 模式）

对应需求：m09500 第⑥条「增加 nsfw 模式，被动被上游截断后路由到本地模型，或者主动路由」。

## 结论先行

策略挂在 `LLMRouter` 上，而不是挂在某个 GameSession 上。所有调用方（叙事、世界生成、
知识生成、NPC 对话）都走 `LLMRouter.complete` / `LLMRouter.stream`，因此一个开关就覆盖全场。

## 主动路由

`LLMConfig.content_policy` 取 `standard`（默认）或 `nsfw`。`nsfw` 时 `with_content_policy()`
返回一个新的 router：

1. 配了 `nsfw_provider` / `nsfw_model` → 用它（不探测，显式配置优先）；
2. 否则本地服务可达（`LOCAL_LLM_BASE_URL` + `LOCAL_LLM_API_KEY` + socket 探测）→ 走 `local`，
   模型名取 `local_model` 或 `LOCAL_LLM_MODEL`；
3. 都不满足 → 保持原 provider，不报错（本地没起来时不该把回合打死）。

本地路由的调用失败会降级回主 provider，日志：

```
LLM policy route failed [<caller>] <错误类型>: <信息> - falling back to the primary provider
```

## 被动路由（上游截断）

截断有两种形态，两条都要接：

| 形态 | 识别点 | 处理 |
| --- | --- | --- |
| 上游用一句话拒绝（正常 200 响应） | `_looks_like_refusal()` | `complete` 里重试本地一次 |
| 上游报错（内容策略 / 审核错误） | `REFUSAL_ERR_RE` | 叙事层重试本地一次 |
| 流式输出 | `refusal_signal()` | 发一帧 `⚠⚠REFUSAL⚠⚠` 后结束，由调用方决定 |

`_looks_like_refusal` 只在答案短于 400 字符时才匹配，因为真实叙事是几段话，而拒绝是一句话；
角色台词里出现 “I can't help with that” 不会误判。

被动路由需要 `LLMConfig.local_fallback_on_refusal = True`（面板默认勾选），并且
`local_route_available()` 为真；否则保持原样，不会多付一次注定失败的调用。

叙事层（`LLMNarrator.narrate`）最多重试一次，且**超时不重试**：玩家已经等了一个超时时长，
再来一个等于把等待翻倍。

## API

```
GET  /api/settings
POST /api/settings
```

`POST` 新增可选字段：

| 字段 | 说明 |
| --- | --- |
| `content_policy` | `standard` / `nsfw`，大小写不敏感，未知值返回 400 并列出可选值 |
| `nsfw_model` | 显式指定 NSFW 模型名；空字符串表示清空 |
| `local_fallback_on_refusal` | 是否启用被动重试 |

省略字段 = 不改动。`GET` 额外返回 `content_policy`、`policy_choices`、`nsfw_provider`、
`nsfw_model`、`local_fallback_on_refusal`、`local_route_available`（后者是实时探测结果）。

## 前端

设置面板新增「内容策略」分区：标准 / NSFW（本地）二选一 + 「上游拒绝时自动改走本地模型」勾选框，
保存时调用 `updateContentPolicy()`（`frontend/src/api.js`）。开关同时写入 `localStorage`
（`nsfwMode` / `refusalFallback`），与其余设置一起恢复。

## 测试

```
backend/tests/engines/test_llm_router_policy.py      20 项：识别、主动路由、被动路由、流式哨兵
backend/tests/services/test_narrator_content_policy.py 4 项：叙事层重试与不重试
backend/tests/api/test_settings_content_policy.py     5 项：设置接口往返与非法值
```

全量后端：`1386 passed, 8 skipped`。

## 本地验证

1. 起本地模型（`E:\ninfer\start-local.ps1`），`.env` 配 `LOCAL_LLM_BASE_URL` / `LOCAL_LLM_API_KEY`。
2. `GET /api/settings` 应看到 `local_route_available: true`。
3. `POST /api/settings {"content_policy":"nsfw"}` 后再开一局，日志里模型名应为 `openai/<本地模型>`。
4. 关掉本地服务再发一次：日志出现 `LLM policy route failed ... falling back to the primary provider`，
   叙事仍然正常返回。

## 未做

- ~~流式 SSE 消费端目前只在收到 `⚠⚠REFUSAL⚠⚠` 时记录~~ —— 已补：`NarratorEngine._stream_with_policy_retry()`
  收到哨兵帧后不再转发，改为整条重发到本地路由；本地也拒绝时该回合没有叙事文本（宁可空，也不把 "REFUSAL" 字样塞进叙事框）。
- 没有任何「本地不可用时禁止发送敏感内容」的硬闸门；需求只要求路由，没有要求审计。