# 阶段 3：故事线默认模板与锁定语义（第 4 步）

## 目标

前一步引入了剧情线（lines）与事件包（packs）。这一步补上两件让作者真正能用起来的能力：

1. **默认模板**——新建场景不再是一条空故事线，游戏可以立刻开始；
2. **锁定/冻结**——已经定稿的剧情线和节点对编辑者只读，避免「已经在跑的故事被悄悄改写」。

锁定完全是编辑期约束：它不改变任何推进规则，运行中的战役照常按同一套确定性逻辑前进。

## 默认模板（backend/app/models/knowledge.py）

`default_storyline(language) -> Storyline`

- 一条主线 `trunk`，两个节点：`opening`（无前置，开局即进入）→ `first_turn`（依赖 `opening`，等作者填真实条件）。
- 未锁定、未停用，是普通可编辑数据。
- 中文与英文两套标题/说明，按场景语言选择。

`POST /api/scenarios/` 在创建场景后写入这份配置：`mode="storyline"` + `default_storyline(language)` + `max_depth=9`。

设计取舍：

- 之前 `PUT play-config {"mode": "storyline"}` 会把空故事线报「故事线模式至少需要一个故事线节点」；现在因为已经有了默认模板，只改 mode 会沿用模板，空故事线仍然会被拒绝。
- 模板只是起点，作者可以整体替换、删除节点，或把模式改回 `free`。

## 锁定语义

### 数据模型

```python
class StoryLineDef(_Strict):
    locked: bool = False   # 整条线定稿

class StoryNode(_Strict):
    locked: bool = False   # 单个节点定稿
```

### 保护规则（backend/app/engines/story_engine.py）

`locked_node_ids(storyline)`：锁定节点本身，以及锁定剧情线里的所有节点。

`protected_edits(previous, updated) -> List[str]`：比较两份文档，返回可读中文错误：

| 情况 | 错误 |
| --- | --- |
| 删除锁定剧情线 | `剧情线 'x' 已锁定，不能删除` |
| 修改锁定剧情线 | `剧情线 'x' 已锁定，不能修改` |
| 删除锁定节点 | `故事线节点 'n' 已锁定，不能删除` |
| 修改锁定节点 | `故事线节点 'n' 已锁定，不能修改` |
| 把未锁节点塞进锁定线 | `故事线节点 'n' 不能加入已锁定的剧情线 'x'` |

- 判定是纯函数：同样的两份文档永远得到同样的错误列表，便于单测与回放。
- 只在**编辑**时生效：`storyline_status` 与 `advance_storyline` 完全不看 `locked`，所以锁定不会让游戏卡住（有测试断言锁定与未锁定的推进结果一致）。
- 新建节点不受限制（只保护「已存在」的内容）。

### API

`PUT /api/v1/scenarios/{id}/play-config` 在原有校验之后追加 `protected_edits(current.storyline, config.storyline)`，有错误返回 400 与可读列表；与锁定内容无关的字段（`max_depth`、模型预设等）仍可单独修改。

`GET /api/v1/campaigns/{id}/story` 的节点里新增 `locked`，玩家侧/调试面板可以看到哪些内容已定稿。

### 编辑器（frontend/src/components/StorylineStudio.jsx）

- 剧情线面板新增「锁定」开关；锁定的线用琥珀色边框与说明条标出，名称、启用开关、移除按钮被禁用。
- 节点面板新增「锁定」开关与 🔒 徽标；锁定（或属于锁定线）的节点不能展开编辑、不能移除。
- 解锁随时可做：取消勾选后立刻恢复可编辑，再同步保存即可。

## 测试

- `backend/tests/engines/test_story_engine.py`：`locked_node_ids` 收集、未改动不算编辑、修改/删除锁定节点、修改/删除锁定线、把节点移入锁定线、锁定不改变推进（9 条）。
- `backend/tests/api/test_routes_knowledge.py`：新建场景带默认模板且校验通过、默认模板可切回 free、空故事线仍被拒绝、锁定内容被 API 拒绝而无关字段仍可修改。
- `backend/tests/api/test_routes_engine.py`：`/story` 暴露 `locked`。

## 已知限制

- 锁定不记录「谁在何时锁定」，也没有解锁审计；需要时再叠加事件记录。
- 事件包目前没有单独的锁定开关（可整体随剧情线版本管理）。
- 模板只有两个节点，不含事件、知识或事件包示例；示例世界仍放在 `frontend/src/data/worldTemplates.js`。
