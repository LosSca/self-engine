# 阶段 3：故事线事件包（小事件包）

## 目标

把故事线从「主干 + 分支 + 独立剧情三种节点」扩展成**可配置的小事件包**结构：

- 主干线：推进节奏的主节点。
- 附属分支：依赖主干的旁支（`kind="branch"`）。
- 独立分支：无前置、靠条件/事件/事件包触发的旁支（`kind="side"`）。
- 小事件包：一组世界事件的集合，满足自己的触发条件时才「启用」，一次性包在任一事件触发后不再启用。
- 节点可以要求「先触发某个事件包」（`requires_event_pack`）。

所有判定都是确定性的：包是否启用、节点能否进入，只看权威 `GameState` 与 `WorldDefinition`，不看 LLM 输出。

## 数据模型（backend/app/models/knowledge.py）

```python
class StoryEventPack(_Strict):
    id: str                        # 1-64
    title: str                     # 1-200
    summary: str = ""
    event_ids: List[str]           # 组成该包的世界事件
    requires: List[Condition]      # 全部满足才算 armed；空 = 开局即 armed
    knowledge_ids: List[str] = []  # 启用时优先注入叙事上下文的知识
    enabled: bool = True
    once: bool = True              # 任一事件触发后不再 armed

class StoryNode(...):
    kind: Literal["main", "branch", "side"]
    line: str = ""                       # 所属剧情线
    requires_event_pack: Optional[str]   # 需要先触发的事件包

class Storyline(...):
    lines: List[StoryLineDef]
    packs: List[StoryEventPack]
```

## 规则（backend/app/engines/story_engine.py）

`pack_status(storyline, state, world) -> (armed, fired)`

- `fired`：包内任一事件已在 `state.fired_events` 中。
- `once=True` 且已 fired → 不再 armed；`once=False` 可重复启用。
- `enabled=False` 的包完全不参与。
- armed = `requires` 全部 `condition_holds`。

`validate_storyline` 新增校验（返回可读中文错误，未知引用安全失败）：

| 情况 | 错误 |
| --- | --- |
| 包 id 重复 | `事件包 id 重复：'x'` |
| 引用不存在的事件 | `事件包 'x' 引用了不存在的事件 'y'` |
| 引用不存在的知识 | `事件包 'x' 引用了不存在的知识条目 'y'` |
| 空包 | `事件包 'x' 没有包含任何事件` |
| 节点依赖不存在的包 | `故事线节点 'n' 依赖的事件包 'x' 不存在` |

独立剧情（side）节点的放行条件相应放宽：没有前置、没有条件、没有 `requires_event`、也**没有** `requires_event_pack` 时才报错——因为此时它开局就会立即进入。

## 知识注入（backend/app/services/knowledge_service.py）

- `build_context(..., armed_packs=[...])`：已启用包的 `knowledge_ids` 与已进入节点的知识一起被提升排序。
- `armed_pack_ids(storyline, state, world)`：给 API 层复用同一个判定。
- `ActionService._narrate_applied` 在提交后的状态上计算 armed 包并传给叙事上下文——叙事能看到「此刻活着的事件包」，但不能决定它。

## API

- `GET /api/v1/campaigns/{id}/story` 新增：`armed_packs`、`fired_packs`、`storyline.packs`、节点上的 `requires_event_pack`。
- `GET /api/v1/scenarios/{id}/context/preview?campaign_id=...`：按该战役权威状态解析 armed 包；不传 campaign 时只给作者视角（不猜测）。
- `PUT /api/v1/scenarios/{id}/play-config` / `POST /scenarios/{id}/knowledge/validate`：沿用既有校验，事件包错误与节点错误一起以 400 返回。

## 编辑器（frontend/src/components/StorylineStudio.jsx）

- 新增「小事件包」面板：ID、名称、摘要、事件 id 列表、触发条件（复用 `ConditionRows`）、启用/一次性开关。
- 节点编辑器新增「需要先触发的事件包」下拉；节点列表显示事件包徽标。
- 删除事件包会同时清掉节点上的引用。
- `GameCanvas` 头部显示当前剧情节点与已启用事件包数量，每次回合后刷新。

## 测试

- `backend/tests/engines/test_story_engine.py`：包启用/触发/一次性/禁用/校验（12 条新增）。
- `backend/tests/services/test_knowledge_service.py`：armed 包知识提升、未启用不提升、`armed_pack_ids` 读权威状态。
- `backend/tests/api/test_routes_engine.py`：`/story` 暴露 packs/armed/fired，且行动后 armed 变化。
- `backend/tests/api/test_routes_knowledge.py`：preview 按 campaign 的权威状态解析 armed 包。

## 已知限制

- 事件包不单独持久化执行记录，`fired` 由事件触发历史推导（单一事实来源是 `state.fired_events`）。
- 包内事件仍需由世界事件定义提供，本层不新建事件。
- 尚无「锁定/冻结」编辑语义（第 4 步）。
