# 引擎 API 的响应契约：从 Dict[str, Any] 到具名模型

## 问题

审查报告 P1-4：`backend/app/api/routes_engine.py` 里有 22 个路由把响应声明成
`Dict[str, Any]` 或 `List[Dict[str, Any]]`。这意味着：

- FastAPI 不做任何出参校验，handler 少写一个键、改名一个键，前端只会安静地拿到 `undefined`；
- OpenAPI（`/openapi.json`、/docs）里这些接口的 schema 是空对象，写编辑器/调试台的人无法自省；
- 前后端没有一份可执行的约定，只有两边各自"以为"的形状。

## 现在

**模块里已经没有任何 `Dict[str, Any]` 响应声明**（有测试守着）。每个 handler 都指向一个具名模型：

| 路由 | 模型 |
| --- | --- |
| `POST /worlds`、`POST /worlds/generate` | `WorldWriteResponse` |
| `GET /worlds` | `List[WorldSummary]` |
| `GET /worlds/{id}/map` | `WorldMapResponse`（含 `MapLocation`/`MapExit`/`MapRegion`/`MapCounts`） |
| `GET /worlds/{id}/events` | `List[GameEventDef]` |
| `GET /action-types`、`GET /actions` | `List[ActionSpecView]` |
| `GET /campaigns/{id}/lane` | `LaneResponse`（`lane: Literal["engine","narrative"]`） |
| `GET /campaigns/{id}/export` | `CampaignExportResponse` |
| `GET /campaigns/{id}/story` | `CampaignStoryResponse` |
| `GET /campaigns/{id}/diagnostics` | `CampaignDiagnosticsResponse`（`narration: NarrationHealth`） |
| `GET /campaigns/{id}/snapshots` | `List[SnapshotView]` |
| `GET /campaigns/{id}/snapshots/prune` | `SnapshotPruneResponse` |
| `GET /campaigns/{id}/state-diff` | `StateDiffResponse` |
| `POST /campaigns/{id}/rollback` | `RollbackResponse` |
| `POST /campaigns/{id}/repair` | `RepairResponse` |
| `POST /campaigns/{id}/actions/preview` | `ActionPreviewResponse` |
| `POST /campaigns/{id}/text-actions` | `TextActionResponse` |
| `GET /campaigns/{id}/events` | `List[DomainEventView]` |
| `GET /campaigns/{id}/quest-progress` | `QuestProgressResponse`（含 `QuestProgressView`/`QuestObjectiveView`） |
| `GET /campaigns/{id}/event-executions` | `List[EventExecutionView]` |
| `GET /actions/{request_id}` | `RequestRecordResponse` |

设计取舍：

- **顶层严、嵌套松**。地图、诊断、执行记录里的引擎自有结构（effects、issues、diff）仍是
  `Dict[str, Any]`/`List[Any]`。契约先钉住"哪些键一定在、类型是什么"，而不是把每个领域对象再复制一遍。
- **刻意留空的字段**。`TextActionResponse.parsed` 是 `Optional`：一句话没解析出来时它确实是 `null`，
  拒绝原因在 `result` 里。把它写成 `{}` 会让"没解析出来"和"解析成一个空动作"看起来一样。
- **不透明 ID**。`DomainEventView.id` 是 `str`：它只是存储层的行键，不是业务事实，换后端不该变更契约。
- **preview 没有 request_id**。干运行永远不会登记请求，契约里就不给这个字段，避免有人拿它去重放。

## 测试

`backend/tests/api/test_routes_contracts.py`（7 条）：

1. `TYPED_PATHS` 里每个路由在 OpenAPI 中都有声明的 `properties`（数组则检查 `items`）。
2. `routes_engine` 源码里没有任何 `response_model=... Dict[str, Any]`。
3. `LaneResponse` 的字段集合与 `lane` 的枚举值被钉死。
4. 地图契约必须含 `locations/regions/items/doors/entities/counts`。
5. `ActionPreviewResponse` 不含 `request_id`（干运行不可能占号）。
6. 出参校验真的会拦：`LaneResponse.model_validate` 收到缺字段的 dict 直接抛。
7. `diagnostics.narration` 引用 `NarrationHealth` 模型而不是匿名对象。

原有 128 个 API 测试同时变成了回归网：任何 handler 开始返回计划外的形状，FastAPI 在序列化阶段就会 500。

## 结果

- 全量后端：**1352 passed, 8 skipped**（137s）。
- 前端 `npm run build` 通过（index 57.12 kB / GameCanvas 84.62 kB / WorldStudio 91.93 kB）。
- 修这条契约时暴露出的真实问题（都被测试当场抓住）：`/actions/{request_id}` 返回的是
  `{request_id, campaign_id, request, status, result, state_version, created_at}`，不是扁平的 action/target；
  地图响应里有 `counts`；domain event 的 `id` 是 UUID 字符串而不是自增整数。

## 后续

- `backend/app/api/routes_game.py`（旧叙事链路）与 `routes_scenarios.py` 仍有若干 `Dict[str, Any]`，
  等旧链路日落时一并收窄。
- 嵌套层（effects / issues / diff）可以在对应领域模型稳定后再逐个替换为具名模型。
