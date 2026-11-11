# 自指引擎 · 13 项系统完整性审计（阶段检查）

> **已被 `docs/review-overview.md`（v0.3.0，2026-10-31）取代。** 本文保留为 2026-10-28 的历史快照。

审计对象：E:\shujia（Project Lunar 主体 + 阶段 0/1 与知识/模式/预设改造后的工作树，未 commit）。
审计时间：2026-10-28。审计方式：阅读实现 + 现有测试 + 真实 HTTP 探测；不依据文档声明。

判定口径（“完整可运行”）：
- **✅ 完整可运行**：数据模型 + 写入口 + 校验 + 自动化测试 + 已接入一条真实游戏链路。
- **⚠️ 部分可用**：核心后端存在且可验证，但缺编辑器/前端入口，或只接在旧链路/只接在新链路的其中一条。
- **❌ 缺失**：没有可用实现。

---

## 一、结论速览

| # | 系统 | 状态 | 一句话结论 |
|---|------|------|-----------|
| 1 | 世界与地图 | ⚠️ | 世界/地点/门/出口模型与 CRUD、校验完整；无地图编辑器与地图视图，实时游玩仍是叙事驱动 |
| 2 | 实体与 NPC | ⚠️ | NPC 人格/记忆/关系在旧引擎完整；v1 无统一实体表，玩家/NPC/物品未统一建模 |
| 3 | 行动与交互 | ⚠️ | v1 六类确定性行动闭环 + 幂等完整；真实游玩仍走旧流式链路；地图点击未接行动 |
| 4 | 事件 | ⚠️ | 条件/效果/优先级/重复策略/级联/验证/模拟完整；无事件编辑器；跨故事线调用缺 |
| 5 | 规则与条件 | ⚠️ | 判定确定性、条件 Pydantic 校验、无 eval；规则本身写在代码里，不能由配置新增 |
| 6 | 任务与故事线 | ⚠️ | 任务目标/完成/奖励推进与故事线引擎、Studio UI 完整；任务无 CRUD API 与编辑器 |
| 7 | 物品与角色属性 | ⚠️ | v1 物品确定；旧链路仍靠 LLM 文本标签改物品；无 HP/属性模型 |
| 8 | LLM 调度与叙事 | ✅ | 多 provider（含本地 NInfer）/模板降级/事实约束/调用追踪可用；意图识别仍待结构化 |
| 9 | 知识与上下文 | ✅ | 5 层知识 + 深度链 + 模式范围 + 预览 + 双链路注入 |
| 10 | 记忆与历史 | ✅ | 记忆晶体/日记/事件流/领域事件齐全；聊天文本不写权威状态（v1） |
| 11 | 世界状态与存档 | ⚠️ | 事务 + request_id 幂等 + 版本 + 事件日志；缺快照、一致性检查、v1 回滚 API |
| 12 | 模型与游戏配置 | ⚠️ | provider/温度/token/模型预设可配；旧链路不读预设；提示词不可在线编辑 |
| 13 | 编辑器与调试 | ⚠️ | 情景构建器、知识 Studio、Devtools、LLM traces 可用；缺世界/实体/事件/任务编辑器与 v1 行动调试视图 |

> 后续增量（文本行动、诊断/回滚/快照、知识 Studio、角色包、故事线、验收走查）见 docs/delivery_phase1.md；最新测试基线 492 passed / 9 failed / 8 skipped。

测试基线：`482 passed, 14 failed, 8 errors`（新增 7 个知识注入测试全绿；14 failed 与 8 errors 与导入基线完全一致：Neo4j 未启动 8 个、旧流式用例 10 个、旧 journal/世界 tick 4 个）。

---

## 二、逐项证据

### 1 世界与地图系统
- 模型：`backend/app/models/game.py` 的 `WorldDefinition` / `LocationDef` / `DoorDef` / `ExitDef` / `GameEventDef` / `QuestDef`，`WorldDefinition.validate_references()`（:245 起）校验重复 id、起点、出口、门钥匙、任务目标与事件条件/效果引用。
- 接口：`backend/app/api/routes_engine.py:123` `POST /api/v1/worlds`、:135 `GET /worlds`、:144 `POST /worlds/validate`、:150 `GET /worlds/{id}`。
- 引擎：`backend/app/engines/rules_engine.py:26 evaluate()` 用 `_move`（:113）按出口与门状态判定可达。
- 缺口：没有 `WorldMapModal` 之外的地图视图，而 `WorldMapModal` 展示的是 Neo4j 知识图谱而非地点图；无地点增删改 UI；旧游玩链路没有地点状态。
- 优先级 P1。

### 2 实体与 NPC 系统
- NPC 人格/记忆/关系：`backend/app/engines/npc_mind_engine.py`、`graph_engine.py`、`graphiti_engine.py`，前端 `NpcInspector.jsx`。
- 角色包（立绘/差分）：`scenario_characters` / `character_looks` + `routes_characters.py`。
- 缺口：v1 的“实体”只有物品与门（`item_locations` / `doors`），没有统一的 entity 表；NPC 不在 v1 权威状态里，无法参与确定性判定（例如 NPC 是否在场、是否可对话）。
- 优先级 P1。

### 3 玩家行动与交互
- v1：`ActionRequest`（move/look/take/open/use/talk）→ `ActionService.execute`（`action_service.py:166`）→ 规则 → 事件 → 任务 → 叙事 → 单事务提交（`game_state_store.py:231 commit_action`）。
- 交互入口：`POST /api/v1/campaigns/{id}/actions`（`routes_engine.py:265`）。
- 真实游玩：前端 `api.js` 的 `streamAction` 打的是 `POST /api/game/action`（旧链路），`ActionInput.jsx` 仍在文本里拼 `[DO]/[SAY]/[CONTINUE]`。
- 缺口：地图点击移动未接任何行动链路（地图本身不存在）；自然语言与结构化行动尚未合并到一条链路。
- 优先级 P0（统一入口是后续所有系统落地的前提）。

### 4 事件系统
- `backend/app/engines/event_engine.py`：`Fact`、`apply_effect`、`run_triggered_events`（:222，按 `(-priority, id)` 排序、单事件深拷贝原子性、`repeat=once` 由 `state.fired_events` 守卫）、`validate_event`（:285）、`simulate_event`（:305，只读副本）、`diff_states`（:345）。
- 接口：`GET /worlds/{id}/events`、`POST .../validate`、`POST .../simulate`（`routes_engine.py:310/329`），执行记录写进 `domain_events`。
- 缺口：无事件编辑器（KnowledgeStudio 只编辑知识/故事线/预设）；触发条件无“执行后跳转到其他事件/故事线”的显式配置；级联上限固定 2 轮。
- 优先级 P1。

### 5 规则与条件判定
- 判定全在 `rules_engine.evaluate`，输出 `RuleOutcome(ok, code, message, effects)`，无网络、无 LLM、可单测（`tests/engines/test_rules_engine.py` 证明纯函数）。
- 条件/效果是 Pydantic 模型（`models/game.py`），未知 kind 安全失败：`_validate_condition` / `_validate_effect` 报可读错误；全库无 eval/exec（grep 确认）。
- 缺口：行动类型与规则集合是代码常量，无法由场景配置新增行动或改判定公式；无规则调试面板。
- 优先级 P2（当前 6 类行动已覆盖验收）。

### 6 任务与故事线
- 任务：`models/game.py QuestDef/QuestProgress`、`quest_engine.advance_quests`（:48，3 轮不动点、目标 kind 映射、奖励经 `apply_effects`），`ActionService` 产出 `quest.completed / quest.progressed` 领域事件。
- 故事线：`story_engine.py validate_storyline(:33) / storyline_status(:99) / advance_storyline(:162)`，节点由前置 + 已触发事件 + 条件推进，完全不调模型；节点进入写 `GameState.story_nodes` 与 `story.entered` 事件；`GET /campaigns/{id}/story` 返回状态。
- UI：KnowledgeStudio「运行模式与故事线」页可编辑节点、前置、条件、关联知识并校验。
- 缺口：任务定义只能写在 world JSON 里，无 `/quests` CRUD 与任务编辑器；故事线只在 v1 链路推进，旧链路仅注入知识。
- 优先级 P1。

### 7 物品与角色属性
- v1：物品数量存 `GameState.inventory`，地面物品 `item_locations`，效果 `Effect(kind=gain_item|consume_item|set_flag|open_door|set_item_location...)`，重复行动被 request_id 幂等挡住（`tests/api/test_routes_engine.py` 覆盖）。
- 风险：旧链路仍从 LLM 文本解析 `[ITEM_ADD:..|..|..] / [ITEM_USE:..] / [ITEM_LOSE:..]` 直接改物品（`backend/app/services/game_session.py:3182` 起）——违反“自然语言不得作为修改权威状态的唯一依据”。
- 缺口：v1 没有 HP/属性/关系模型（`GameState` 无 hp 字段），战斗仍在旧引擎。
- 优先级 P0（标签副作用）/ P2（属性模型）。

### 8 LLM 调度与叙事
- 路由：`backend/app/engines/llm_router.py` 支持 deepseek/anthropic/openai/local，本地 NInfer 走 `openai/<model>` + 自定义 base_url，缺 key 有明确报错。
- 叙事：`services/narrative_service.py` `TemplateNarrator`（确定性降级）与 `LLMNarrator`（只喂已提交事实 JSON，system 明令不得虚构，超时/空输出/异常均返回 failed 且不影响已提交状态）。
- 观测：`trace_store.py` + `GET /api/game/{id}/traces`。
- 缺口：意图识别/物品副作用仍依赖模型文本标签；叙事 LLM 与规则结果之间只有提示词约束，没有二次结构化校验（例如统一 JSON schema 校验）。
- 优先级 P1。

### 9 知识与上下文
- 模型与存储：`models/knowledge.py`、`scenario_store` 迁移 7（`scenario_knowledge`、`scenario_play_config`、`model_presets`，`SCHEMA_VERSION=7`）。
- 组装：`knowledge_service.build_context`（:43）按模式/深度/层级/常驻/节点加成排序，返回 blocks + stats + 被排除原因；`validate_play_config`（:120）。
- 接口与 UI：`routes_knowledge.py` 的 CRUD / validate / context preview / play-config / model-presets，前端 `KnowledgeStudio.jsx`。
- 双链路注入：v1 经 `ActionService._load_play_context`；旧链路经 `routes_game._load_knowledge_context_for_campaign` + `GameSession._with_knowledge`（每回合重读，编辑立即生效），标签为「背景知识，不得覆盖已确认事实」。
- 验证：`tests/services/test_knowledge_service.py`（10）、`tests/api/test_routes_knowledge.py`（12）、`tests/services/test_game_session_knowledge.py`（7）。
- 缺口：检索是确定性的深度/关键词加成，没有向量召回；知识条目与旧 story_cards/lore 是两套并存的内容来源。

### 10 记忆与历史
- 旧：`memory_engine.py`（记忆晶体）、`journal_engine.py`、`event_store.py`（SQLite `events.db`，`append` :109，按类型回放重建历史/NPC/剧情锁/日记）。
- 新：`domain_events` 记录每次行动、事件、任务、叙事失败、故事节点进入。
- 边界：v1 里叙事文本只进 `ActionResult.narrative`，不参与状态；旧链路聊天记录仍是重建素材（可重放，但不是权威状态）。
- 缺口：长期记忆摘要在 v1 尚未接入（旧引擎有）；知识图谱写入失败只记录不重试。

### 11 世界状态与存档
- `game_state_store.py`：`campaign_state`（含 version）、`action_requests`（request_id 主键）、`domain_events`，`commit_action`（:231）在同一事务内按 请求行 → 状态 → 事件 顺序写入，重复 request_id 触发主键冲突整体回滚。
- 恢复：`GET /campaigns/{id}/state` 直接读库，无内存缓存；`tests/api/test_routes_engine.py` 覆盖重载一致与重复请求。
- 缺口：无状态快照表（当前直接存整份状态 JSON，可接受但无历史版本回看）；无一致性检查命令；v1 无“回滚到某版本”API；旧链路 `POST /api/game/{id}/rewind` 只删最后一对事件。

### 12 模型与游戏配置
- 全局：`config.py`（provider、模型、温度、token、本地 NInfer 地址/上下文窗口）、`.env`、`GET /api/settings`、前端 `SettingsPanel`。
- 每场景：`scenario_play_config.model_preset_id` + `model_presets` 表（4 个内置预设 + 自定义），v1 叙事读预设覆盖 temperature/max_tokens。
- 缺口：旧流式链路不读模型预设；提示词模板不可在 UI 编辑；超时/重试仅叙事层固定 30s，无全局配置。

### 13 编辑器与调试
- 已有：`ScenarioBuilder.jsx`（情景/角色包/卡片/问答）、`KnowledgeStudio.jsx`（知识/模式/故事线/预设）、`DevtoolsPanel.jsx`（traces 查看）、`MemoryInspector.jsx`、`NpcInspector.jsx`、`JournalPanel.jsx`、`PlotGeneratorPanel.jsx`、`WorldMapModal.jsx`（知识图谱）。
- 后端调试：事件 validate/simulate（返回 diff）、域事件列表、`GET /actions/{request_id}`、本地模型状态探测。
- 缺口：世界/地点、实体、事件、任务没有编辑界面；没有“一次行动的判定→效果→事件→任务→叙事”时间线视图；模拟与真实执行的结果对比只有 diff 字段，未在 UI 展示；调试接口未按环境开关限制（目前只靠无鉴权的本地服务）。

---

## 三、真正阻断“完整可运行”的三件事（建议顺序）

1. **P0 双链路并存**：`/api/game/action`（LLM 驱动）与 `/api/v1/.../actions`（规则驱动）同时存在。验收要求“地图点击与自然语言共用一条行动链路”，必须先让前端游玩走 v1，把旧链路降级为纯叙事/兼容接口。
2. **P0 旧链路的物品标签副作用**（`game_session.py:3182`）：模型文本仍可增删物品，与“LLM 不裁定事实”冲突。迁移方案：叙事返回的结构化事件只允许“建议”，由规则服务二次校验后才落库。
3. **P1 编辑器覆盖度**：世界/地点、任务、事件三类配置目前只能通过 API + JSON 编辑，与“可配置引擎”的要求还有差距。

## 四、本轮实际验证过的命令

```powershell
cd E:\shujia\backend
.\.venv\Scripts\python.exe -m pytest -q --no-header -p no:cacheprovider
# 482 passed, 14 failed, 8 errors（失败/错误集合与基线一致）

.\.venv\Scripts\python.exe -m pytest tests/services/test_game_session_knowledge.py -q
# 7 passed

cd E:\shujia\frontend
npm run build
# ✓ built in 1.96s（2970 modules，index-C-rarweZ.js 711.70 kB）
```
