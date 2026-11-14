# Hybrid World Simulation — 架构推进计划

> 适用范围：`backend/`。本文件是本次改造的边界契约：超出「架构准入原则」的模块进不了代码。

## 0. 准入原则（每个新模块必须逐条回答）

新增模块前必须能回答，答不全就不建：

1. 它让世界更真实吗？
2. 它让玩家或 NPC 能做更多合理行动吗？
3. 它让世界变化更可控、更可追溯吗？
4. 现有模块组合能不能实现？

并且必须写明：**解决的缺口**、**为什么现有模块不能承担**、**是否引入重复状态或执行路径**、
**怎么测出它的模拟价值**、**不引入时的替代方案**。

硬约束：复用 `ActionService` / `RulesEngine` / `EventEngine`（`EventStore`）/ `GameStateStore` /
`ActionRegistry` / `GameClock` / `NpcMindEngine`；不建第二套权威写入口；NPC 不另开执行链路；
不重复 EventStore 或 GameClock；Affordance 是 World Definition 的数据；Action Planning 扩
`TextActionService`；NPC 决策先做 Utility AI；旧 `GameSession` 保留兼容但不得成为新模拟的执行基础。

## 1. 现状（2026-11-15 代码实测）

| 能力 | 现有实现 | 结论 |
| --- | --- | --- |
| 行动目录 | `app/engines/action_registry.py`，16 个 ActionSpec（attack/flee/defend/take/open/use/talk/move/equip/unequip/buy/sell/choose/custom/investigate/look） | 原语已存在，缺 `create`/`destroy`/`dismantle`/`combine` 一类 |
| 规则判定 | `app/engines/rules_engine.py` `RulesEngine.evaluate(world, state, action)`，纯函数、确定性种子 `_seed()` | 可复用，无需改判定模型 |
| 权威写入 | `app/services/action_service.py` `ActionService.execute(request)`，带 `request_id` 幂等、状态版本/CAS、`_retry_on_conflict` | 唯一写入口，NPC 也必须走它 |
| 文本解析 | `app/services/text_action_service.py` `TextActionParser`（动词表 + 候选解析） | 自由行动的扩展点 |
| 状态 | `app/db/game_state_store.py`（`GameStateStore`） | 唯一权威状态 |
| 事件 | `app/db/event_store.py`（`EventStore`，`EventType` 枚举，`witnessed_by`） | 只有"已发生"，没有"将发生" |
| 时间 | `app/engines/game_clock.py` `Clock`，读 `state.day`/`state.minute`，`advance()` 只在提交玩家行动的事务内被调用 | 只随玩家行动推进，无调度 |
| NPC | `app/engines/memory_engine.py` `NpcMindEngine`（思想/记忆），`game_session.py` 的 `_update_npc_minds` | 只有"想法"，没有行动循环 |

**关键结论**：统一行动模型、规则判定、权威写入、事件记录**全部已存在**，不需要新建第二套。
真正缺的只有三件事：

1. `ActionRequest` 里没有 `actor_type`，NPC 无法以"自己"的身份提交行动；
2. 时间不能独立推进，也没有"将发生"的事件表；
3. NPC 只被动产生想法，没有感知→目标→计划的循环。

## 2. 缺口与替代方案

### 缺口 A：行动主体身份

- **现状**：`ActionRequest.actor_id` 默认 `"player"`，没有 `actor_type`；`RulesEngine` 假定行动者是玩家。
- **为什么现有模块不能承担**：不是能力问题，是数据模型缺字段。加一个字段即可，不能靠外部包装。
- **是否引入重复路径**：否。仍然只有 `ActionService.execute` 一条写入路径。
- **测法**：同一初始状态 + 同一 seed，玩家发起与 NPC 发起同样的 `take`，权威结果应完全一致；
  `actor_type="npc"` 的请求在 `_replay`/`_reject` 记录里能追溯。
- **不引入的替代**：让 NPC 复用 `actor_id="player"` —— 会污染事件来源与权限判断，不可接受。

### 缺口 B：世界时间与到期事件

- **现状**：`Clock` 是 `GameState` 上的两个计数字段，只在玩家行动事务内 `advance`；`EventStore`
  只记录已发生的事实。
- **为什么现有模块不能承担**：EventStore 的语义是"事实"，把"计划"塞进去会破坏可追溯性
  （一个未触发的计划事件和一条真实历史无法区分）；`Clock` 只有读数没有驱动者。
- **是否引入重复状态**：会引入一张新表，但**只存未来**，触发后立即落成 `EventStore` 的
  Domain Event 并删除 —— 单一事实来源仍是 EventStore。时钟仍只有 `GameState.day/minute` 一份。
- **测法**：推进 7 个游戏日，商人按营业时间开店、铁匠持续生产、守卫换班；进程重启后未到期事件
  仍在；快进 3 天不丢事件；同一 seed 重放结果一致。
- **不引入的替代**：每个 NPC 每分钟轮询 —— 与"避免全量轮询"直接冲突，且无法持久化。

### 缺口 C：NPC 自主行动循环

- **现状**：`NpcMindEngine` 产出想法，通过 `NPC_THOUGHT` 事件持久化；没有感知、目标、计划、行动。
- **为什么现有模块不能承担**：`GameSession` 是"玩家回合"的编排者，它的执行是 LLM 驱动的
  自由叙事；把 NPC 行为塞进去等于新建第二条执行链路，且旧会话本身正在退役。
- **是否引入重复路径**：否，前提是 NPC 的每次行动都作为 `ActionRequest(actor_type="npc")` 交给
  `ActionService`。NPC 不直接改 `GameState`。
- **测法**：见第 6 节「最小验证场景」。
- **不引入的替代**：只给 NPC 写日程脚本 —— 能过"按时开店"，过不了"矿石被偷后重新规划"，
  即验收标准 4。

## 3. 分阶段交付

每阶段都必须可运行、有测试、不破坏现有测试（进入 Phase 2 时的基线是 1775 个）。

### Phase 1 — 统一行动模型 + 原语 + Affordance ✅ 已完成

落地内容（每一条都只加数据或加一个规则方法，没有新系统）：

- **行动主体**：`ActionRequest.actor_type: Literal["player","npc","environment","system"]`
  （默认 `player`），`ActionResult.actor_type` 回显，写入请求记录后 `_load_result` 与
  `_replay_text` 都按 actor 校验。`RulesEngine.evaluate` 签名未动——规则对任何主体一视同仁，
  NPC 拿不到额外权限，也不可能绕过 `ActionService` 直写状态。
- **原语**：`ActionRegistry` 新增 `dismantle` / `combine` / `wait`（共 19 个动词），
  每个只是 `ActionSpec` + 一个规则方法。`wait` 的分钟数从 `params["minutes"]` 读，
  缺省用目录里声明的 `minutes`——时钟推进仍由提交阶段做，规则只管"允不允许"。
- **Affordance 作为数据**：`ItemDef` 新增 `dismantles_to` / `made_from` / `craftable`。
  `dismantles_to` 为空 = 拆不开（`code="cannot_dismantle"`），这就是"affordance 只是尝试许可"
  的具体形态：世界声明能拆成什么，规则决定这次拆不拆得成。
- **失败原因**沿用 `RuleOutcome(code, message)`：`cannot_dismantle` / `unknown_part` /
  `not_craftable` / `missing_parts`（message 列出还缺什么）/ `invalid_duration`。
- **文本入口**：`TextActionService` 学会三个新动词，`combine` 的目标可解析未持有的可合成物，
  `dismantle` 只解析手上的东西；"我拆掉木桌""等一下"这类中文说法都落到同一个 `ActionRequest`。
- **可重放**：同 `request_id` 重放不重复发料，`state_version` 不变（已有 CAS + 幂等机制覆盖）。
- 测试：`tests/services/test_action_actor.py`、`test_action_crafting.py`、
  `test_text_action_crafting.py`、`test_text_action_actor.py`（共 20 条）。

### Phase 2 — World Clock + Event Scheduler ✅ 已完成

- **时钟驱动**：`app/engines/world_scheduler.py`。时钟仍然只有 `GameState.day/minute` 一份，
  驱动逻辑是纯函数：`catch_up_minute(store, campaign_id, now)` 返回「下一个到期事件或 `now`」，
  `plan_tick` 取 `due_events`，`run_tick` 在一份**副本**上执行，`commit_tick` 才落盘。
  **不逐分钟轮询**：空排程时一次跳到目标时刻，有事件时逐段前进，事件因此在"它自己的那一分钟"生效。
- **持久化调度**：`game_state_store.py` 迁移 5 新增 `scheduled_events`
  `(due_abs_minute, priority, sequence_id, kind, payload_json, status, created_abs_minute, fired_at)`，
  `sequence_id` 全表单调递增——同分钟的两个事件按"谁先排"执行，这是场景可重放的前提。
  API：`schedule_event` / `due_events` / `next_due_minute` / `cancel_event` /
  `reschedule_event` / `mark_event_fired` / `list_scheduled_events` / `has_pending_event`。
- **两种事件严格分开**：`EventStore` 里只有**已经发生**的 Domain Event（触发后写一条
  `WORLD_TICK`，与其它事件同一张表、同一个事务）；`scheduled_events` 里只有**还没发生**的计划。
  触发时把该行 `status` 置 `fired` 而不是删除——「这个计划为什么没生效」仍然可查。
- **与行动共用一次提交**：`ActionService._apply` 在 `advance_state` 之前调 `_tick_with_clock`，
  把这一段时间内到期的事件跑完；`_commit_turn` 在有事件被触发时走 `commit_simulation_tick`
  （状态、领域事件、`fired` 标记同一个 CAS 事务），否则仍是原来的 `commit_action`。
  时间只由调度器推进一次，外层不再重复累加（曾经 08:00 等 120 分钟变成 11:00，
  因为 `run_tick` 已经把时钟推到目标时刻又被加了一遍）。
- **失败不吞也不炸**：handler 抛异常 → 记一条 `WORLD_TICK`（`status: "failed"` + 错误类型），
  时钟继续走下去；这一行仍被标为 `fired`，所以不会每个回合重试同一个坏排程。
- **重复排程保护**：`has_pending_event(campaign_id, kind)` 让"上班/巡逻"这类循环计划
  每轮重新排时不会堆副本。
- 测试：`tests/services/test_world_scheduler.py`（13）、
  `tests/services/test_time_carries_events.py`（7）——覆盖空 tick 也推进时钟、
  提交后事件只触发一次、tick 写 request 行可重放、跨越睡眠时段的事件在正确分钟生效、
  handler 可以让世界再排下一个班次。

### Phase 3 — NPC Utility AI（先简单，不上 GOAP）

- 每个 NPC 一份状态：当前活动/地点/需求/长短期目标/计划/日程/行为状态/个性技能，
  存在 `GameState` 的扩展字段里（**不新建第二份世界状态**）。
- Level 1 确定性行为（日程/工作/休息/吃饭/简单移动）；Level 2 Utility 打分选目标；
  计划仍是原语序列，逐步交给 `ActionService`。
- 计划失效检测：每一步执行后重读权威状态，失败即重新规划。

### Phase 4 — LLM 高层决策（可选层）

- LLM 只输出结构化建议（目标、理由、拟行动作），经 `TextActionService`/规则验证后才执行。
- 测试用假的 router，不调用真实付费 API。

### Phase 5 — 小镇压力测试

- 见第 6 节。

### 旧 GameSession 的边界

- 继续保留 `compat_mode` 兼容读，但**不承载任何新模拟能力**：调度器、NPC 循环、原语都不放进
  `game_session.py`。它的规模只会减少，不会增加。

## 4. 不做什么

- 不新增 embedding/向量/记忆检索能力（已有四层检索，本次不动）。
- 不重写前端，不做多人服务，不引入外部消息队列或图数据库之外的基建。
- 不做 GOAP 完整实现、不做多 Agent 框架（Phase 3 先 Utility）。
- 不为 NPC 建独立执行器；不建第二套状态表。

## 5. 验收对照

| 验收项 | 落点 |
| --- | --- |
| 1 玩家可提出未预定义行动 | Phase 1 原语 + 组合 |
| 2 原语与规则组合处理合理行动 | Phase 1 |
| 3 所有行动经统一规则验证 | 全程只走 `ActionService` |
| 4 NPC 自主选择并执行目标 | Phase 3 |
| 5 NPC 有日程与持续行为状态 | Phase 2 + 3 |
| 6 NPC 行动真实改变状态 | Phase 3（经 ActionService） |
| 7 时间不再只靠玩家推进 | Phase 2 `world_scheduler.plan_tick/run_tick` |
| 8 Scheduled Events 正确触发与恢复 | Phase 2 `scheduled_events` 表 + `commit_simulation_tick` |
| 9 NPC 不获得未授权知识 | 复用 `witnessed_by` + `NpcMindEngine` 知识边界 |
| 10 关键状态变化可追溯 | Domain Event 记录 |
| 11 相同输入重现相同权威状态 | 现有 `_seed` + request_id |
| 12 测试不依赖付费 LLM | 全部用假 router |

## 6. 最小验证场景（小镇）

5 个地点：广场、铁匠铺、商铺、兵营、民居。
10 个 NPC、3 种职业（铁匠 ×1、商人 ×1、守卫 ×2、村民 ×6）、基础经济、昼夜与日程。

断言清单（每条都要有自动化测试）：

1. 玩家拆掉木桌 → 得到木板 → 用木板做盾牌（多步骤、逐步验证）。
2. 玩家偷走铁匠的矿石 → 铁匠发现减少 → 未目击时只能依据证据推断 → 调整生产计划 →
   通过 `buy` 向商人采购 → 守卫因报告开始调查。
3. 推进 7 天：商人营业时间正确、铁匠产出连续、守卫换班、事件按时触发。
4. 快进 3 天：无事件丢失，顺序正确。
5. 进程重启后未到期事件仍能触发。
6. 同 seed 重跑 → 权威状态字节级一致。
