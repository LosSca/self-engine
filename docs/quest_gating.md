# 任务门控：前置、接受条件与失败

任务不再只是「目标 + 奖励」。一个任务同样可以声明**前置任务**、**接受条件**和**失败条件**，
全部是经过校验的数据（`Condition` / `Effect`），由规则与事件驱动，模型不参与判定。

## 数据模型（backend/app/models/game.py 的 QuestDef）

| 字段 | 类型 | 默认 | 含义 |
| --- | --- | --- | --- |
| `prerequisites` | `List[str]` | `[]` | 前置任务 id；全部 `completed` 后任务才可能激活 |
| `accept_requires` | `List[Condition]` | `[]` | 接受条件；全部成立后任务才可能激活 |
| `fail_conditions` | `List[Condition]` | `[]` | 失败条件；任一成立时任务立即 `failed` |
| `auto_accept` | `bool` | `True` | 为 `False` 时永不自动激活，只能用 `quest_accept` 效果启动 |

`QuestProgress.status` 的取值：`inactive` → `active` → `completed` / `failed`。
`failed` 是终态：`quest_complete` 效果会拒绝它（`quest '<id>' has already failed`）。

## 新增的两个效果

| 效果 | 目标 | 行为 |
| --- | --- | --- |
| `quest_accept` | 任务 id | `inactive` → `active`（已激活的任务保持原状）；已完成的任务会被拒绝；产生 `quest_accepted` fact |
| `quest_fail` | 任务 id | 置为 `failed`；已完成的任务会被拒绝；产生 `quest_failed` fact |

两者都要通过 `_validate_effect` 的任务存在性校验（`unknown quest '<id>'`）。

## 校验（WorldDefinition.validate_references）

| 情况 | 错误信息 |
| --- | --- |
| 前置指向自己 | `quest '<id>' lists itself as a prerequisite` |
| 前置指向不存在的任务 | `quest '<id>' requires unknown quest '<p>'` |
| 前置成环 | `quest '<start>' has a circular prerequisite chain` |
| 接受条件引用未知物品 / 门 / 地点 / 任务 / 实体 | `quest '<id>' accept condition: unknown ...` |
| 失败条件引用未知对象 | `quest '<id>' fail condition: unknown ...` |
| 效果指向未知任务 | `unknown quest '<id>'` |

校验发生在写库之前：`POST /worlds`、`PUT /worlds/{id}/quests/{quest_id}` 都返回 400 + 可读错误列表。

## 运行时行为（backend/app/engines/quest_engine.py）

- `initial_quest_progress(world)`：满足 `auto_accept and prerequisites_met(...) and accept_conditions_met(...)` 的任务开局即 `active`，否则 `inactive`。
- `advance_quests(...)` 每轮推进前先跑两个迁移：
  - `_activate_gated_quests`：`inactive` 且前置完成、接受条件成立 → `active`（补齐目标计数为 0）。
  - `_fail_doomed_quests`：`active` 且任一失败条件成立 → `failed`，**不发放奖励**。
- 只有 `active` 的任务会推进目标；`inactive` 与 `failed` 都被跳过。

## API

没有新增路由，沿用已有的：

- `PUT /api/v1/worlds/{world_id}/quests/{quest_id}`（upsert，含新字段）
- `GET /api/v1/campaigns/{campaign_id}/quest-progress`（返回 status 为 `inactive` / `active` / `completed` / `failed`）

## 前端（frontend/src/components/WorldStudio.jsx）

任务的「任务」标签页现在包含：

- **前置任务**：其他任务的可点选标签（不会把自己列进去）。
- **接受条件** / **失败条件**：复用 `ConditionRows`，目标下拉来自当前世界。
- **满足条件时自动接受** 复选框（对应 `auto_accept`）。
- 列表行摘要：`前置 N · 接受条件 N · 失败条件 N · 奖励 N · 需手动接受`。

## 测试

`backend/tests/engines/test_quest_gating.py`（19 条）：

- 校验：未知前置、自引用、成环、未知物品失败条件、未知任务效果目标。
- 初始状态：无门控 → `active`；有前置 → `inactive`；接受条件未满足 → `inactive`；`auto_accept=False` → 永不自动激活。
- 迁移：前置完成后激活；接受条件满足后激活；失败条件触发 → `failed` 且没有奖励。
- 效果：`quest_accept` 启动手动任务；`quest_fail` 结束任务；已完成的任务不能被再次接受；已失败的任务不能被 `quest_complete` 完成。
- 整回合：`ActionService.execute` 走完移动后 gated 任务不推进；前置补齐后任务变为 `completed`，奖励正好发放。

## 已知限制

- 失败条件只在 `advance_quests` 的回合末评估；如果某条规则在同一回合既完成又触发失败，完成优先。
- 没有「任务阶段」概念：多阶段任务需要用多条任务 + 前置串起来。
- 前端暂不支持拖拽调整前置顺序（前置之间是集合语义，顺序无意义）。
