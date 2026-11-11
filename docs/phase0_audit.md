# Phase 0：现状审计（Player-Centric 升级）

本文件是《Self-Engine 轻量化架构升级指导》第 14 节 Phase 0 的实际结果。所有结论来自
读代码与跑测试，不是按文档推断。

## 1. 基线

| 项 | 实测 |
| --- | --- |
| 后端测试基线（本轮开工前） | 1493 passed, 8 skipped |
| 本轮收尾 | 1575 passed, 8 skipped（137s） |
| 启动 | `python launch.py`，后端 FastAPI + 前端 Vite 独立进程 |
| 权威存储 | SQLite（`GameStateStore`，schema 版本化迁移） |
| 图 | Neo4j / Graphiti 仅用于知识图谱，不承载权威状态 |
| 前端 | React + Vite + Zustand，无测试框架 |

## 2. 已有功能（可直接复用）

- **唯一权威写入口**：`backend/app/services/action_service.py` `ActionService.execute()`。
  request_id 去重（`_replay`）、CAS 版本（`commit_action(expected_version=...)`）、
  request 行 + 状态 + 域事件 + 快照在同一事务提交、叙事在提交之后生成（叙事失败不回滚）。
- **规则与事件**：`engines/rules_engine.py`（1268 行，按 `action_registry` 的 ActionSpec 分派）、
  `engines/event_engine.py`（validate_event / simulate_event / diff_states）、
  `engines/quest_engine.py`、`engines/story_engine.py`。
- **记忆**：`engines/memory_engine.py` 四层晶体 SHORT→MEDIUM→LONG→MEMORY，
  且 `build_npc_knowledge_window(campaign_id, npc_name)` 已经按 `witnessed_by` 做 NPC 知识隔离。
- **NPC 心智**：`engines/npc_mind_engine.py`（NpcMind/NpcThought），**纯内存**，由事件重建。
- **模型调用**：`engines/llm_router.py`（provider 枚举、内容策略、nsfw 本地回落、调用日志）。
- **创作与抽取**：`services/authoring_session.py`（对话 → 提案 → 校验 → 提交）、
  `services/knowledge_extractor.py`（确定性分句与分类）、`services/ai_generator.py`。

## 3. 尚未具备的能力（本轮的真实缺口）

1. 没有「角色 → 模型」的路由：只有一个 primary/fallback/nsfw/local 体系。
2. 没有分层上下文的构建器：叙事把整份 facts 与知识一次性塞进 prompt。
3. NPC 只有 thoughts，没有档案（身份/性格/动机/情绪/关系/秘密）与隔离的知识视图。
4. 玩家给的文本没有确定性分类：传闻与事实无法区分。
5. 模型调用没有留下可审计的记录（谁、哪个角色、多少 token、多久、为什么失败）。
6. 玩家离开后，旧的 `GameSession` 仍会 fire-and-forget 世界 tick。

## 4. 职责重叠（本轮只划定边界，不做大搬家）

| 重叠 | 处置 |
| --- | --- |
| `services/game_session.py`（3370 行，旧链路，含 world tick）vs `/api/v1` 新链路 | 保留；只加 `pause()/resume()`，不迁移 |
| `engines/narrator_engine.py`（流式）vs `services/narrative_service.py`（LLMNarrator） | Narrative Agent 落在 `narrative_service`，旧流式保留 |
| `services/text_action_service.py` / `services/intent_advisor.py` vs System Agent | System Agent 只处理「复杂/不确定」情况，简单情况仍是确定性分类 |
| `services/authoring_session.py` / `knowledge_extractor.py` vs WorldEvolutionService | **不新建平行实现**：世界演化复用这两条既有路径 |

## 5. 需要新增的模块（本轮已实现 / 计划中）

| 模块 | 状态 |
| --- | --- |
| `engines/agent_router.py` | ✅ 角色配置、路由、降级、调用遥测 |
| `engines/player_context.py` | ✅ 分层上下文（fact/canon/belief/guess/history + 预算） |
| `engines/npc_dossier.py` | ✅ 从 EntityDef/EntityState/NpcMind 组装档案，秘密天然隔离 |
| `engines/system_agent.py` | ✅ 确定性分类优先，模型只在不确定时介入，载荷必须过校验 |
| `services/world_evolution.py` | ⏳ Phase 3（复用 authoring_session + knowledge_extractor） |
| 记忆元数据（source/confidence/observer） | ⏳ Phase 4 |
| GameClock（游戏内时间） | ⏳ Phase 5；本轮只做了「离线即暂停」 |

## 6. 兼容性风险与约束

- 模型名**绝不写死**：`AGENTS_<ROLE>_PROVIDER` / `AGENTS_<ROLE>_MODEL`；未配置的角色原样复用主路由，
  因此零配置时行为与升级前一致。
- `GameState` 未加任何字段；新增数据只落在新表 `agent_calls`（schema v3，独立事务、失败只告警）。
- 遥测失败不得让一个回合失败：`record_agent_call` 接受未知字段并只 debug 记录。
- 旧存档无需迁移：没有改任何既有列或 JSON 结构。

## 7. 每阶段的验证方式

```bash
cd backend
TEMP='E:/shujia/.tmp' TMP='E:/shujia/.tmp' .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
cd ../frontend && node scripts/check-agent-wiring.mjs
```

前端无法在本沙箱跑 `vite build`（DSH 预览钩子导致 config 解析失败），因此前端验证是
「esbuild 语法解析 + 接线检查脚本」，不是构建验证。这一条在交付时如实说明。
