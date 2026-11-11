# 需求 ③④⑤⑥⑦ 交付与整体审查报告

本文件汇总 m09500 七条需求中本轮完成的六条，以及 m00001 验收标准 1–14 的当前状态。它记录的是**代码库里现在是怎样**，不是计划。

## 一、需求对照

| # | 需求 | 状态 | 入口 | 提交 |
|---|---|---|---|---|
| ① | 一句话生成世界与初始故事线 | 完成 | `POST /api/v1/scenarios/author`、前台「剧本生成」页签 | 3763e034 |
| ② | 角色外部挑选 | 完成 | `GET/POST /api/v1/works*`、前台「作品穿越」页签 | 92db8664 |
| ③ | 任务编辑与进度 | 完成 | `GET|POST|PATCH|PUT|DELETE /api/v1/worlds/{w}/quests*`、`POST /campaigns/{c}/quests/{q}/objectives/{o}/progress` | 2a32d3e7 |
| ④ | 叙事诊断与记忆可视化 | 完成 | `narrative.diagnostics`、调试台「叙事与上下文」页签、`GET /campaigns/{c}/requests` | b548e935 + e8157da8 |
| ⑤ | 编辑器与调试工具 | 完成 | `WorldStudio` 七个页签、`DevtoolsPanel` 三个页签 | e8157da8 + 102cf4a1 |
| ⑥ | 内容策略与本地回退 | 完成 | `POST /api/settings` 的 `content_policy`、`LLMRouter.complete_routed` | e75e0661 + a3fdacb8 |
| ⑦ | 世界穿越器 | 完成 | 同 ②，`work_catalogue` 六部作品 + 模型兜底 | 92db8664 |
| — | 叙事语言跟随世界 | 完成 | `WorldDefinition.language` | 102cf4a1 |

## 二、验收标准复核（m00001 第九节 1–14）

全部由 `backend/tests/api/test_acceptance_story.py`（5 项）与相邻套件覆盖，`ENGINE_NARRATOR=template` 下无需任何密钥即可复现：

1. 建世界（村庄/森林/仓库 + 上锁的门）—— `test_acceptance_world_key_door_quest_narrative_and_reload` 前半段
2. 钥匙与拾取条件 —— 同上（`iron_key` 定义在 `village_square`，门 `warehouse_door key_item_id='iron_key'`）
3. 进入地点发现并拾取钥匙 —— 同上，`take` 返回 `applied`、`effects_applied` 含 `give_item`
4. 无钥匙开门被拒 —— 同上，`open` 返回 `rejected`、`code='missing_item'`
5. 有钥匙通过判定 —— 同上，`open` 返回 `applied`
6. 更新门状态并记录事件 —— 同上，`state.doors['warehouse_door'].open is True` 且领域事件含 `keeper_alert`
7. 任务进度更新 —— 同上，`GET /campaigns/{c}/quest-progress` 的 `find_key`/`open_door` 均为 done
8. LLM 依据已确认结果生成叙事 —— 同上（叙事文本由 `outcome.effects` 生成），中文世界由 `test_acceptance_chinese_world_reads_in_chinese_even_without_a_model` 覆盖
9. 重载存档后状态一致 —— 同上，关掉 store 重开同一 `game_state.db` 读回
10. 重复请求不重复副作用 —— `test_acceptance_duplicate_requests_do_not_repeat_side_effects`（同 request_id 返回 `duplicate`，库存与 xp 不变）
11. 叙事失败不影响已提交状态 —— `test_acceptance_narrative_failure_keeps_committed_state`（`FailingNarrator` 下 `status='applied'`、`narrative.status='failed'`、状态可读）
12. 非法事件被拒且有可读错误 —— `test_acceptance_invalid_event_and_simulation_is_read_only`（422/400 detail 含字段名）
13. 事件模拟不改真实状态 —— 同上（`simulate` 前后 `GET /state` 的 version 不变）
14. 自动化测试稳定通过 —— 全量 `1451 passed, 8 skipped`，本轮连续两次全量运行均通过

## 三、架构落点（相对 m00001 第三节职责划分）

- 规则：`backend/app/engines/rules_engine.py`（`RulesEngine.evaluate`，按 `action_registry` 的 `spec.handler` 分派）
- 效果与事件：`backend/app/engines/event_engine.py`（条件/效果注册表，未知种类安全失败）
- 任务：`backend/app/engines/quest_engine.py`（`advance_quests` 消费 Fact）
- 行动统一入口：`backend/app/services/action_service.py`（幂等 `_replay`、`expected_version` 比较写入、单事务 `store.commit_action`、提交后 `_notify_settled`）
- 叙事：`backend/app/services/narrative_service.py`（`LLMNarrator` / `TemplateNarrator`，诊断旁路）
- 权威存储：`backend/app/db/game_state_store.py`（SQLite）；知识图谱是只读镜像通知（`settled_sink`），失败不影响回合
- 编辑器：`frontend/src/components/WorldStudio.jsx`；调试：`frontend/src/components/DevtoolsPanel.jsx`

`backend/app/services/game_session.py`（旧链路）仍在，但已不参与 `/api/v1` 行动回合；`LEGACY_APPLY_ITEM_TAGS` 默认 0。

## 四、仍然存在的问题

1. **前端从未经过 `vite build`**。本沙箱里 DSH 预览钩子让 `vite.config.js` 报 `Cannot find module E:\shujia\.dsh-tmp\config-module.mjs`，只能用 esbuild 做逐文件语法检查。前端功能没有在浏览器里被自动化验证过。
2. **旧 LLM 文本标记通道尚未删除**。`backend/app/services/game_session.py:64` 的 `_legacy_item_tags_enabled()` 与 `frontend/src/api.js` 的剥标记逻辑仍在，只是默认不写权威状态。彻底日落需要单独一轮。
3. **`backend/app/api/routes_game.py` / `routes_scenarios.py` 仍返回 `Dict[str, Any]`**。`/api/v1` 已全部有类型化响应模型并有契约测试，旧 `/api` 没有。
4. **写死在代码里的起步世界**仍有中英两份（`starter_worlds.py`），世界语言字段落地后它们是一致的，但未来加语言要改两处。
5. **两个历史偶发失败**：`test_timeskip_logs_world_changes_in_journal`、`test_delete_traces_for_unknown_campaign_returns_deleted_count` 曾因仓库内 DB 文件只读/锁竞争失败（`backend/app/db/trace_store.py:176`），单跑必过；本轮全量未复现。
6. **事件条件/效果不是通用脚本语言**，这是刻意的；`backend/app/models/game.py` 的 `Effect.kind` 目前 26 种，加新种类要同时改 `event_engine` 注册表与前端 `EffectRows`。

## 五、本地验证

```bash
cd backend
TEMP='E:/shujia/.tmp' TMP='E:/shujia/.tmp' .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
```

只跑验收走查：

```bash
cd backend
TEMP='E:/shujia/.tmp' TMP='E:/shujia/.tmp' .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider tests/api/test_acceptance_story.py
```

手工走一遍（无需密钥）：`python launch.py --debug` → 新建场景 → 绑定起步世界（奇幻）→ 观察/拾取铁钥匙/开门，调试台三个页签应分别给出调用追踪、叙事上下文与行动记录。
