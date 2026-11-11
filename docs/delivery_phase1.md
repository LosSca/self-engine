# 阶段 0-1 交付报告：规则驱动的行动处理闭环

交付对象：E:\shujia（以 Project Lunar master 为基线，基线提交 fc0d73d）。
配套阅读：docs/audit_phase0.md（审计与架构判定）、docs/audit_systems_13.md（13 项系统完整性检查）。

## 1. 审计结论（阶段 0，摘要）

审计详情见 docs/audit_phase0.md，关键事实：

- 仓库没有 AGENTS.md/CLAUDE.md；开发约定只能从 README/PLANO.md 与代码推断。
- 旧主链路 `POST /api/game/action` 是"叙事先行"：LLM 边生成边 SSE 推给前端，随后解析叙事文本里的 [ITEM_ADD:...] 等标签修改库存，最后才写 NARRATOR_RESPONSE 事件。
- `EventStore.append` 每次单独 INSERT + commit，事件与其投影没有同事务保证。
- 旧 `game_state.db` 并不存在；权威状态分散在 events.db（"事件重放"）、scenarios.db（场景配置）与内存 GameSession 中。
- 结论：不动旧链路的前提是新增一条"规则 → 事件 → 任务 → 叙事 → 单事务提交"的权威路径，再逐步把旧入口接过去。

## 2. 本轮新增（阶段 1 及其后增量）

### 2.1 数据模型与校验（backend/app/models/game.py）

- `WorldDefinition`：world_id/scenario_id/start_location_id + locations/items/doors/entities/events/quests，全部 `extra="forbid"`。
- `LocationDef`（exits/door_ids/item_ids/npc_ids）、`ExitDef`（target_id/door_id/requires_item_id）、`ItemDef`、`DoorDef`（locked/opened/key_item_id/consumes_key）、`EntityDef`（npc/creature/object，含 location_id/dialogue/hostile）。
- `Condition`（flag/has_item/item_count/door_open/at_location/quest_status）、`Effect`（set_flag/give_item/remove_item/move_player/open_door/close_door/quest_progress/quest_complete/emit）。
- `GameEventDef`（trigger/conditions/effects/priority/repeat once|always/enabled）。
- `QuestDef`（objectives/required_count/rewards）、`QuestProgress`。
- `GameState`：campaign_id/world_id/version/turn/location_id/inventory/flags/doors/item_locations/quests/fired_events/story_nodes。
- `ActionRequest`（campaign_id/request_id/action/actor_id/target_id/params/narrate）、`ActionResult`（status applied|rejected|duplicate|conflict、code、message、state_version、effects_applied、events_fired、quests、narrative、state）。
- `WorldDefinition.validate_references()`：重复 id、未知出口/门钥匙/物品/任务目标、触发器动作缺失、任务奖励引用等都给出可读错误；未知 kind 由 Pydantic 直接拒绝。

### 2.2 引擎（纯函数，无 IO）

- `backend/app/engines/rules_engine.py`：move/look/take/open/use/talk 六类行动的确定性判定，返回 `RuleOutcome(ok, code, message, effects)`；无钥匙开门返回 `door_locked`，无出口返回 `no_exit` 等。
- `backend/app/engines/event_engine.py`：条件判定、效果应用（单事件深拷贝，失败整事件不落地）、按 `(-priority, id)` 排序、`repeat=once` 由 `fired_events` 守卫、级联最多 2 轮；`validate_event` / `simulate_event`（只读副本）/ `diff_states`。
- `backend/app/engines/quest_engine.py`：由已提交事实推进目标（obtain_item/open_door/reach_location/flag），完成判定与奖励发放（奖励也走 Effect）。
- `backend/app/engines/story_engine.py`：故事线节点（entry conditions、requires_nodes、fired_events）推进，自由模式无节点。
- `backend/app/engines/narrative_service.py`：`TemplateNarrator`（确定性，默认）与 `LLMNarrator`（只读已提交事实，超时/异常/空输出降级为 `failed`）。
- `backend/app/engines/item_proposals.py`：旧物品标签迁移层——LLM 文本中的物品操作先变成 proposal 事件（默认 rejected），只有 `LEGACY_APPLY_ITEM_TAGS=1` 才允许落地。

### 2.3 持久化（backend/app/db/game_state_store.py）

- 新库（默认 `backend/game_state.db`，env `GAME_STATE_DB_PATH`），SCHEMA_VERSION=2，表：`worlds`、`campaign_state`、`action_requests`、`domain_events`、`campaign_snapshots`（+ schema_version）。
- `commit_action` 在**同一事务**内写：action_requests 行（request_id 主键，幂等兜底）→ campaign_state（版本 +1）→ domain_events → 每版本快照；异常统一 rollback。
- 另有 `list_events`（按版本）、`list_snapshots`、`get_snapshot`、`commit_rollback`（回滚产生新版本，历史不删）。

### 2.4 服务层

- `backend/app/services/action_service.py`：统一行动入口 `execute(request)`，顺序为：重放检查（同 request_id 返回 duplicate；payload 不同返回 conflict）→ 规则判定 → 触发事件 → 任务推进 → 叙事（内存生成，无写权限）→ 单事务提交 → 返回 ActionResult。叙事失败只记录 `narrative.failed` 域事件，不撤销状态。
- `backend/app/services/text_action_service.py`：自然语言 → 结构化行动。规则可确定的部分（"pick up the iron key"、"open the warehouse door"）确定性匹配，匹配不到才调用 LLM；LLM 只输出 `{action,target_id,confidence}`，解析失败安全拒绝。
- `backend/app/services/world_service.py` + `world_editor.py`：世界保存/读取/校验、初始状态构建、campaign 绑定；world_editor 提供 location/item/door/entity/event/quest 的 upsert/delete（深拷贝 + 校验后返回）。
- `backend/app/services/state_diagnostics.py`：状态一致性诊断（计数器/任务/门/库存/事件版本）、修复（dry-run 默认）、快照与 diff 支持。

### 2.5 API（/api/v1，backend/app/api/routes_engine.py 等）

- 世界：`POST /worlds`、`GET /worlds`、`GET /worlds/{id}`、`PUT /worlds/{id}`、`POST /worlds/validate`；资源级 `PUT/DELETE /worlds/{id}/{locations|items|doors|entities|events|quests}/{rid}`。
- 游戏：`POST /campaigns/{id}/world`（绑定）、`GET /campaigns/{id}/state`、`POST /campaigns/{id}/actions`、`POST /campaigns/{id}/text-actions`、`GET /campaigns/{id}/events`、`GET /campaigns/{id}/story`。
- 事件：`GET /worlds/{id}/events`、`POST /worlds/{id}/events/{eid}/validate`、`POST /worlds/{id}/events/{eid}/simulate`（只读）。
- 调试：`GET /actions/{request_id}`、`GET /campaigns/{id}/diagnostics`、`/snapshots`、`/snapshots/{version}`、`/state-diff`、`POST /campaigns/{id}/rollback`、`POST /campaigns/{id}/repair`（`ENGINE_DEBUG_TOOLS=0` 时 403）。
- 知识/模式/预设：`backend/app/api/routes_knowledge.py`（knowledge CRUD、play-config、preview-context、model-presets）。
- 角色包：`backend/app/api/routes_characters.py`（scenario characters + looks + campaign character + 上传）。
- 旧入口兼容：`POST /api/game/action` 在已绑定 v1 world 时内部转 `ActionService`（若未绑定则保持旧流式链路），SSE 协议不变；`POST /api/v1/campaigns/{id}/text-actions` 供前端试玩自然语言。

### 2.6 前端

- 首页/情景构建器重做（世界类型预设、基调、开场、轻/暗主题 `lib/theme.js` + `styles/light.css`）。
- `KnowledgeStudio.jsx`：知识库（5 层/深度/常驻）、运行模式与故事线节点编辑、模型预设。
- `CharacterProfilePanel.jsx` + 角色差分；`SetupWizard.jsx` 开局向导；`GameCanvas`/`ActionInput` 接入统一行动链路（自然语言输入）。
- 尚未提供：世界/地点地图视图与可视化编辑器、事件/任务编辑器面板、v1 行动调试面板。

## 3. 测试与结果（实测）

命令：

```powershell
cd E:\shujia\backend
.\.venv\Scripts\python.exe -m pytest -q
```

结果：`492 passed, 9 failed, 8 skipped`（耗时约 20s）。

- 8 skipped：`tests/engines/test_graph_engine.py`（需要真实 Neo4j，新增 socket 探测后跳过而非 error）。
- 9 failed：全部是上游基线既有失败，已在基线 worktree（fc0d73d）复现同一批：journal/time skip 的 3 个用例依赖直接注入内存 journal（现在会被真实事件重建覆盖）、npc_mind 解析 1 个、game_session 的 5 个（mock 缺 `_llm`/异步方法，流程在断言前提前返回）。
- 本轮新增测试（全绿）：`tests/api/test_acceptance_story.py`（验收 1-13 的 HTTP 走查）、`tests/api/test_routes_engine.py`、`tests/services/test_action_service.py`、`tests/engines/test_event_engine.py`、`test_rules_engine.py`、`tests/db/test_game_state_store.py`、`tests/models/test_world_validation.py` 等。

验收标准对应（tests/api/test_acceptance_story.py 中逐条断言）：

1. 世界（village/forest/warehouse + 上锁门）：`test_acceptance_world_key_door_quest_narrative_and_reload`
2. 钥匙与拾取条件：同上的 fixture `tests/fixtures/village_world.json`
3. 发现并拾取钥匙：take applied，inventory 1
4. 无钥匙被拒：camp-blind → code=door_locked、版本 0、门仍 False
5. 有钥匙通过：open applied，门 True
6. 门状态与事件：keeper_alert 事件执行，flags.warehouse_opened=True
7. 任务进度：open_the_warehouse completed，objectives {find_key:1, open_door:1}
8. 叙事依据已确认结果：narrative.status=ok 且包含门名（模板叙事；LLM 走 ENGINE_NARRATOR=llm）
9. 重载存档一致：新 TestClient 读同一 SQLite，version/inventory/doors/quests 全部一致
10. 幂等：重复 request_id 返回 duplicate，不重复拾取、不重复发奖励
11. 叙事失败不撤销：`test_acceptance_narrative_failure_keeps_committed_state`（narrative.failed 事件 + 状态仍提交）
12. 非法事件配置被拒：`test_acceptance_invalid_event_and_simulation_is_read_only`（400 + 可读错误）
13. 事件模拟只读：同一测试，模拟前后 state 完全一致
14. 自动化测试：本文件 + 上述套件

前端构建：`cd frontend; npm run build` → 成功（vite 7，711.70 kB / gzip 222.08 kB）。

## 4. 本地验证方法

```powershell
# 后端
cd E:\shujia\backend
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000

# 前端
cd E:\shujia\frontend
npm install
npm run dev      # Vite 代理 /api 到 8000
```

手动走查（curl 或 httpie）：`POST /api/v1/worlds` 导入 `backend/tests/fixtures/village_world.json` → `POST /api/v1/campaigns/demo/world` → `POST /api/v1/campaigns/demo/actions` 依次 take/open → `GET /api/v1/campaigns/demo/state` → `GET /api/v1/campaigns/demo/diagnostics`。

## 5. 仍存在的问题 / 后续

- 9 个上游旧测试仍红（原因见 §3），建议单独一轮修 mock 与 journal 测试注入方式。
- 前端尚无世界地图与事件/任务编辑器；v1 行动调试面板未接入（后端 debug API 已齐备）。
- 旧 `game_session` 链路仅在未绑定 v1 world 时保留；物品标签仍以 proposal 形式记录，是否彻底停用取决于旧剧情数据评估。
- 战斗/时间流逝等旧系统尚未迁到规则层（`combat_engine` 仍由 LLM 裁定）。
- Neo4j 图同步设计为提交后的旁路，失败只记录不重试；重试队列待补。
