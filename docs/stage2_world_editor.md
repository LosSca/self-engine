# 阶段 2 交付：世界工坊（World Studio）与读接口

本轮在「阶段 0 审计 + 阶段 1 确定性行动闭环」之上，补齐配置侧与调试侧，使世界、地点、物品、门、
实体、事件、任务都能在浏览器里配置，并能把配置好的世界绑定到正在进行中的冒险。

## 后端新增接口（`backend/app/api/routes_engine.py`）

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/v1/worlds/{world_id}/map` | 地图视图：地点 + 出口（含门/所需物品）、地点内物品与 NPC、门列表、计数；引用悬空时 `known=false` |
| GET | `/api/v1/campaigns/{campaign_id}/quest-progress` | 任务进度：每个目标的 count/required/done，以及 active/completed/failed 分组 |
| GET | `/api/v1/campaigns/{campaign_id}/event-executions` | 事件执行历史（`limit`/`status`/`after_version`），由已提交的领域事件反推 |

`backend/app/db/game_state_store.py` 新增 `list_domain_events(campaign_id, *, event_type_prefix, limit, after_version)`。
其余读写能力（世界 CRUD、地点/物品/门/实体/事件/任务子资源 PUT/DELETE、事件 validate/simulate、
状态快照/差异/回滚/修复、`/actions/{request_id}`）在此前阶段已就绪，本轮未改动其语义。

## 前端新增

- `frontend/src/components/WorldStudio.jsx`（约 60KB）：地图、地点、物品、门、实体、事件、任务、调试 8 个页签。
  - 地图页签读取 `/map`，展示出口与门、地点内物品/NPC，悬空引用标注「（悬空）」。
  - 事件页签可对单个事件做「校验」「模拟」，模拟结果只读展示，不写真实状态。
  - 调试页签：冒险绑定卡（把当前世界绑定到当前冒险，显示已绑定世界/版本/回合）、状态诊断、任务进度、
    事件执行历史、状态版本与回滚（逐版本列出 request_id，可「对比上一版」或「回滚到此版本」）、
    行动详情（点击 request_id 查看判定与结果）。
- `frontend/src/App.jsx`：情景档案卡片新增「世界工坊」入口（带 scenarioId）。
- `frontend/src/components/GameCanvas.jsx`：游戏内 HUD 新增地球按钮，打开同一个工坊并携带 campaignId。
- `frontend/src/api.js`：新增 25 个 v1 helper（世界/子资源读写、地图、校验/模拟、任务进度、事件历史、
  诊断、快照、差异、回滚、行动详情、冒险绑定）。

## 解析器改进

`backend/app/services/text_action_service.py`：候选名增加下划线/空格两种写法（`_aliases`），
并把玩家输入里的 `_` 归一成空格。作者写 `iron_key`、玩家输入 "iron key" 也能命中同一目标；
中文显示名匹配与「解析不到目标即安全拒绝」的既有行为不变。

## 测试与验证

- `backend/tests/api/test_routes_map_and_progress.py`（新增，6 passed）：地图、任务进度、事件历史三接口。
- `backend/tests/services/test_text_action_parser.py`（新增，4 passed）：ASCII id、下划线、中文名、未知目标。
- 全量：`cd backend && .venv\Scripts\python -m pytest -q` → **498 passed, 9 failed, 8 skipped**；
  9 个失败与上游基线 `fc0d73d` 完全一致（journal 直注入被重建覆盖 3、npc_mind 1、game_session mock 5）。
- 前端 `npm run build` 通过（757.87 kB / gzip 233.18 kB）。
- 真机冒烟（uvicorn + 真实 SQLite）：建世界 → 绑定冒险 → "pick up the iron key"（applied，v1）→
  "open the warehouse door"（applied，v2，任务 completed）→ 同 request_id 重放返回 duplicate →
  快照 `[{1,snap-take},{0,""}]` → 差异 v0→v1 正确 → `/actions/snap-take` 返回判定明细 →
  回滚到 v0 生成 v2 且库存清空。

## 本地验证

```bat
start.bat
```

浏览器打开 http://127.0.0.1:5173 → 情景卡片「世界工坊」→「从模板创建工作世界」→
在「调试」页签点「把这个世界绑定到当前冒险」→ 进入冒险输入 "pick up the iron key" 与
"open the warehouse door"，回到工坊调试页签查看版本、差异与事件历史。
