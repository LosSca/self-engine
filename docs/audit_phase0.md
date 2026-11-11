# 阶段 0 审计报告：从 LLM 驱动到规则驱动的文字 RPG 引擎

审计对象：Project Lunar（master 分支，2026-09-07 导入，基线提交 fc0d73d）。
审计结论以仓库实际代码为准；本报告中所有路径、行号均来自本次阅读。

## 1. 审计范围与方法

- 阅读 README.md（29.7 KB）、PLANO.md（42.0 KB）、docs/ 下的设计与阶段记录（fase0_baseline.md、fase3a_ab.md、fase3b_ab.md、plans/、tickets/、narrative_coherence_plan.md、setup_wizard_plan.md）。
- 仓库中**没有 AGENTS.md，也没有 CLAUDE.md**；只有 `.claude/burro.json` 与 `.claude/prompts/memory-improvements.md`，不包含开发流程约定。
- 检查 git 状态（导入后为干净基线，用户无本地改动）、目录结构、依赖、启动脚本（install.bat/install.sh、start*.bat、cli.sh）、测试配置（backend/pytest.ini：asyncio_mode=auto、testpaths=tests）。
- 追踪 `POST /api/game/action` 的完整链路、SQLite 三张库的写入方式、物品副作用、LLM 调用边界、前端状态管理与 API 封装。

## 2. 技术栈核实（以实际代码为准）

| 层 | 实际实现 | 位置 |
| --- | --- | --- |
| 前端 | React 18 + Vite + Tailwind + Zustand | frontend/src/store.js:1 `import { create } from 'zustand'`，store.js:11 `export const useGameStore = create(...)` |
| 前端 API | `fetch` + `res.body.getReader()` 手写 SSE 解析（无 EventSource） | frontend/src/api.js:100（POST /game/action）、api.js:118 |
| 前端组件 | 16 个组件，已含场景编辑器、世界地图弹窗、Devtools、物品面板 | ScenarioBuilder.jsx 37.1 KB、GameCanvas.jsx 28.6 KB、WorldMapModal.jsx 17.0 KB、DevtoolsPanel.jsx、InventoryPanel.jsx、NpcInspector.jsx、MemoryInspector.jsx |
| 后端框架 | FastAPI + Pydantic v2 + uvicorn | backend/app/main.py、backend/app/api/ |
| 持久化 | `sqlite3`（标准库，`check_same_thread=False` + `threading.Lock`） | backend/app/db/{event_store,scenario_store,trace_store}.py |
| LLM 路由 | 自研 `LLMRouter` 包装 litellm，按 provider 路由 | backend/app/engines/llm_router.py |
| 知识图谱 | `neo4j.AsyncGraphDatabase` | backend/app/engines/graph_engine.py:6、:36 |
| 前端构建 | vite.config.js、Tailwind | frontend/ |

FastAPI 路由规模：`routes_game.py` 24 个端点、`routes_scenarios.py` 12 个、本次新增 `routes_engine.py` 12 个（/api/v1），合计 48 个。`main.py:33-36` 的三处 `include_router` 即挂载点，新增路由器不影响旧前缀。

## 3. 现有行动执行链路（审计重点）

`POST /api/game/action`（backend/app/api/routes_game.py:331-388）：

1. `_ensure_session(campaign_id)` 取/建内存会话；按请求覆盖 provider/model/temperature/max_tokens，然后返回 `StreamingResponse`（SSE）。
2. `game_session.process_action(...)` 组装上下文（角色设定、永久上下文、记忆、剧情卡、物品、NPC、日志），调用 LLM 生成叙事，并 **边生成边 yield 给玩家**（backend/app/services/game_session.py:1584 与 :1909）。
3. **叙事文本先到玩家，状态再被自然语言修改**：随后才解析 `[ITEM_ADD:...]`、`[ITEM_USE:...]`、`[ITEM_LOSE:...]` 标签（解析器在 game_session.py:3142-3146），逐条 `_apply_inventory_event` 改库存并向前端 yield `[INVENTORY]`（:1589-1592 与 :1902-1905）。
4. 最后才 `_event_store.append(NARRATOR_RESPONSE, ...)` 记录叙事事件（:1608 与 :1925），其余状态变更各自 `append`。
5. `EventStore.append`（backend/app/db/event_store.py:109-146）每次单独 `INSERT` + `commit`，**事件与其投影无法同事务提交**。

由此得到三个结构性问题：

- **事实来源错位**：物品/剧情状态由 LLM 输出文本中的控制标记决定，模型一旦"宣布"玩家拿到钥匙，系统就照做，没有规则层可以拒绝。
- **无幂等**：请求没有 `request_id`，同一请求重放会重复触发物品效果与事件。
- **无版本与原子性**：没有状态版本号；叙事已流式送达而状态写入失败时，玩家看到的文字与真实状态不一致。

## 4. 数据存储现状

- `events.db`：`events` 表（id, campaign_id, event_type, payload, narrative_time_delta, location, entities, created_at, witnessed_by）+ `schema_version` 迁移表（event_store.py:92-107）。
- `scenarios.db`：场景、剧情卡、campaign 元数据（scenario_store.py）。
- `traces.db`：每次行动的 LLM 调用追踪（trace_store.py），供 Devtools 面板展示。
- **没有权威游戏状态表**：状态是"事件 + 会话内存投影"，无快照、无版本号、无一致性检查；进程重启后只能从事件重放。
- Neo4j 只承担知识图谱与关系查询（graph_engine.py / memory_engine.py），未与 SQLite 竞争同一份权威状态——这一点需要保持。

## 5. 主要风险（按优先级）

P0-1 **LLM 文本直接改权威状态**（物品标签链路），违反"规则决定事实"。
P0-2 **无幂等、无版本号**：重复请求 = 重复副作用；无法判断响应是否过期。
P0-3 **状态与事件非原子**：流式中断会留下半提交状态，且没有回滚手段。
P1-4 世界/地点是场景 JSON 与提示词里的自由文本，没有可校验的地点、可达关系、上锁门数据模型。
P1-5 没有规则层，任何行动都不会被确定性拒绝。
P1-6 叙事生成失败与状态写入耦合，失败即污染。
P2-7 依赖无法按 requirements.txt 安装：`litellm==1.43.0` 在 PyPI 不存在（项目最新 1.106.5），`graphiti-core>=0.5.0` 要求 `pydantic>=2.8.2` 而文件钉死 `pydantic==2.8.0`；本次通过临时解钉安装（litellm 不钉版本、暂不装 graphiti-core）才跑通测试，**未修改仓库 requirements.txt**。
P2-8 测试基线存在既有失败（见第 7 节），非本次引入。

## 6. 必须保留的兼容接口

- `/api/game/*` 全部端点与 SSE 标签协议：`[DONE]` `[JOURNAL]` `[MODE]` `[CRYSTAL]` `[PLOT_AUTO]` `[INVENTORY]` `[IMAGE]` `[SUGGESTIONS]` `[TRUNCATE_CLEAN]` `[USAGE]` `[TRACE]`；前端 store.js/api.js 依赖这些标签。
- `PlayerActionRequest` 字段与流式响应形状。
- `EventStore` 的 `schema_version` 迁移链与 `events` 表列；`scenarios.db` 场景 JSON 格式。
- 前端组件与 Zustand store 结构。

新能力放在 `/api/v1`（本次已实现）并保持旧接口不变，避免一次性重写。

## 7. 测试基线（实测）

- 命令：`cd backend; $env:PYTHONPATH="E:\shujia\backend"; .\.venv\Scripts\python.exe -m pytest -q --no-header -p no:cacheprovider`
- 导入依赖后的基线：**315 passed / 14 failed / 8 errors**（失败集中在 game_session 的 inventory 标签流、journal、npc_mind 解析、timeskip 日志；errors 全部为缺少 Neo4j 服务）。
- 完成阶段 1 后：**384 passed / 14 failed / 8 errors**（新增 69 个测试全绿，既有失败与错误集合不变，无回归）。
- 后续增量（文本行动、诊断/回滚、知识/角色/故事线、验收走查）完成后：**492 passed / 9 failed / 8 skipped**。8 个 error 改为 skip（无 Neo4j 时跳过）；9 个 failed 全部为上游基线既有（已在 worktree 基线复现），明细见 docs/delivery_phase1.md §3。

## 8. 阶段 1 已落地（本次提交范围）

| 文件 | 职责 |
| --- | --- |
| backend/app/models/game.py | 世界/地点/物品/门/任务/事件的条件与效果、`ActionRequest`、`GameState`、`ActionResult`、`RuleOutcome`；未知字段、未知 kind 由 Pydantic 直接拒绝，`validate_references()` 校验跨引用 |
| backend/app/engines/rules_engine.py | 纯函数规则：move/look/take/open/use/talk，产出 `RuleOutcome`（code + message + effects），从不改状态 |
| backend/app/engines/event_engine.py | 条件判定、效果应用（逐事件深拷贝原子性）、优先级与 `repeat` 去重、`validate_event`/`simulate_event`/`diff_states` |
| backend/app/engines/quest_engine.py | 由已提交事实推进任务目标、完成判定与奖励发放 |
| backend/app/db/game_state_store.py | 新库 `game_state.db`：worlds / campaign_state / action_requests / domain_events + schema_version；`commit_action` 把请求行、状态、领域事件放在**同一事务**，`request_id` 主键即幂等兜底 |
| backend/app/services/world_service.py | 世界校验与保存、初始状态构建、campaign 绑定 |
| backend/app/services/action_service.py | 统一行动入口：重放检查 → 规则判定 → 叙事（内存，无写权限）→ 原子提交；叙事失败只记录不撤销 |
| backend/app/services/narrative_service.py | `TemplateNarrator`（确定性、默认可跑）与 `LLMNarrator`（只吃已提交事实、禁改状态、超时/空/异常降级为 failed） |
| backend/app/api/routes_engine.py | `/api/v1`：worlds 建/查/校验、campaign 绑定与状态、actions、events 列表、事件 validate/simulate、request 详情 |
| backend/tests/{models,engines,services,db,api} | 69 个自动化测试 + 可配置示例世界 `tests/fixtures/village_world.json` |

关键设计决策：

1. **叙事在提交前于内存生成**：它的输入正是即将提交的状态，且没有任何写权限；叙事失败被记录进同一个事务（`narrative.failed` 领域事件），因此不存在"文字已发出但状态没存"的窗口。
2. **`request_id` 是 `action_requests` 主键**：重放命中即返回 `duplicate`；并发下由 SQLite 唯一约束兜底（`IntegrityError` 回滚整个事务后重放）。
3. **`narrate` 不参与请求指纹**：表现层开关不能让重放变成 `conflict`。
4. **模拟执行只读**：`simulate_event` 在 `model_copy(deep=True)` 上应用效果，条件不满足时不再展示效果差异，真实状态零改动。
5. **旧链路完全不动**：`/api/game`、SSE 标签、三个旧 SQLite 库与前端均未修改；阶段 1 是并行新增的权威状态路径，后续阶段再逐步把 `game_session` 的行动处理迁到 `ActionService`。

## 9. 后续阶段建议（按本轮计划）

- 阶段 2：把世界/地点/实体 CRUD 接到前端最小编辑器（复用 ScenarioBuilder 的世界树位置），并补 location 编辑器保存校验。
- 阶段 3：事件条件/效果扩展（时间、关系、阵营）、任务链前置、事件执行历史接口。
- 阶段 4：`LLMNarrator` 接入真实 `LLMRouter`（`ENGINE_NARRATOR=llm`）并复用记忆/剧情卡上下文；补超时与格式校验测试。
- 阶段 5：debug 面板展示行动 request → 规则结果 → 状态 diff → 事件 → 叙事。
- 阶段 6：端到端验收脚本（世界→钥匙→门→任务→重载→重复请求）与部署文档。
