# 需求 ①–⑦ 交付与整体审查（第二轮）

本文件替换第一版 `docs/delivery_review.md` 的需求表。它记录**代码库里现在是什么**，
包括没有做到的部分，而不是计划。上一版把 m09500 的 ③④⑤ 记成了「任务编辑 / 叙事诊断 / 调试台」，
那三条是 m00001 阶段 3–5 的内容；本版按 m09500 的原话重新逐条核对，并把 ①⑦ 的实现方式如实标成
「策展优先」而不是「联网搜索」。

## 一、七条需求逐条核对

| # | m09500 原话 | 现在的实现 | 状态 | 提交 |
| --- | --- | --- | --- | --- |
| ① | 通过输入提示词一键生成世界和初始故事线 | 一次模型调用产出世界 + 知识 + 剧情线三件套，逐件独立校验，失败件进 `warnings`；`save=false` 只试运行；前台「剧本生成」页签 | 完成 | 3763e034 |
| ② | 角色可以在外部挑选 | 六部作品的策展角色档案 + 模型兜底；`POST /works/enter` 建世界并把角色写入 `character_pick` | 完成 | 92db8664 |
| ③ | 不断通过对话（提示词）补全与拓展世界、故事线和记忆 | **世界与记忆**：`/worlds/{w}/authoring/propose` 出提案、`commit` 落盘；记忆走 `POST /scenarios/{s}/knowledge/extract`。**故事线不在这条通道里**，仍由 ① 生成 | 部分完成 | aedd7b3b |
| ④ | 从文本中提取分类归纳信息与记忆 | `knowledge_extractor` 先确定性抽取（句子分类 + `X 是 Y` / `X 持有 Y` 句式），模型只做改写；写入只进知识库 | 完成 | 3c3faeb7 |
| ⑤ | 基于交互的体验系统 | 只读的交互预演：`/worlds/{w}/plan`、`/preview-actions`、`/explain`，判定全部来自真实 `RulesEngine` 与 `event_engine`，状态深拷贝不写回 | 完成 | 3c3faeb7 |
| ⑥ | nsfw 被动截断路由到本地 / 主动路由 | `content_policy`（standard/nsfw）、拒绝哨兵不转发、叙事层整条改走本地重试一次，流式同处理 | 完成 | e75e0661 + a3fdacb8 |
| ⑦ | 联网搜索作品生成世界和故事线 | **策展优先**：`work_catalogue` 六部作品的档案与关键词匹配，命中即用；未命中才调模型。**没有联网搜索** | 部分完成 | 92db8664 |

另外三条不属于 m09500 但已落地，用来支撑上面这些：

| 能力 | 入口 | 提交 |
| --- | --- | --- |
| 任务编辑与进度 | `GET|POST|PATCH /worlds/{w}/quests`、`POST /campaigns/{c}/quests/{q}/objectives/{o}/progress` | 2a32d3e7 |
| 叙事诊断 | `narrative.diagnostics`、调试台「叙事与上下文」、`GET /campaigns/{c}/requests` | b548e935 + e8157da8 |
| 叙事语言跟随世界 | `WorldDefinition.language` | 102cf4a1 |

## 二、③ 和 ⑦ 差在哪（不遮掩）

**③ 故事线。** 对话式补全目前只写世界文档与知识库；故事线节点由「剧本生成」一次性产出，
之后只能通过 `PUT /scenarios/{s}/play-config` 整份替换。所以「一边聊一边长出剧情分支」没有做到。
技术上不难（`story_engine.validate_storyline` 已经在），难在决定「对话产生的节点该不该进主线」，
需要作者保护和冲突合并的规则，本轮没有设计。

**⑦ 联网。** 本机网络受限（连 github.com 直连都被重置），而模型对具体作品的记忆也不稳定；
更根本的是，联网结果不可复现，测不出回归。所以选择策展档案：六部作品的档案是代码里的常量，
匹配是确定性的，`allow_model=false` 时零模型调用。真接搜索的话，应该接在
`work_catalogue.find_work` 未命中之后，把搜索结果写进同一份 `WorkProfile`，其余链路不用改。

## 三、架构落点（m00001 第三节职责划分）

- 世界与实体：`backend/app/services/world_service.py`、`world_editor.py`（校验读改写）、`backend/app/models/game.py` 的 `WorldDefinition`
- 规则：`backend/app/engines/rules_engine.py`（`RulesEngine.evaluate`，按 `action_registry` 的 `spec.handler` 分派）
- 事件与效果：`backend/app/engines/event_engine.py`（条件/效果注册表，未知种类安全失败）
- 任务：`backend/app/engines/quest_engine.py`（`advance_quests` 消费 Fact）
- 统一行动入口：`backend/app/services/action_service.py`（幂等 `_replay`、`expected_version` 比较写入、单事务 `commit_action`、提交后 `_notify_settled`）
- 叙事：`backend/app/services/narrative_service.py`（`LLMNarrator` / `TemplateNarrator`，诊断只读旁路）
- 抽取与提案：`backend/app/services/knowledge_extractor.py`、`authoring_session.py`
- 预演：`backend/app/services/interaction_planner.py`
- 权威存储：`backend/app/db/game_state_store.py`（SQLite）；知识图谱只是只读镜像通知，失败不影响回合
- 编辑器：`frontend/src/components/WorldStudio.jsx`（12 个页签）；调试：`frontend/src/components/DevtoolsPanel.jsx`（3 个页签）

`backend/app/services/game_session.py`（旧链路）仍在，但不参与 `/api/v1` 行动回合；`LEGACY_APPLY_ITEM_TAGS` 默认 0。

## 四、验收标准（m00001 第九节 1–14）

全部由 `backend/tests/api/test_acceptance_story.py` 与相邻套件覆盖，`ENGINE_NARRATOR=template` 下无需密钥：
建世界与上锁的门、钥匙拾取、无钥匙被拒（`code=missing_item`）、有钥匙通过、门状态与 `keeper_alert` 事件、
任务目标完成、叙事基于已确认结果、重开 `game_state.db` 后一致、同 `request_id` 返回 `duplicate` 且库存与 xp 不变、
叙事失败仍 `status=applied`、非法事件 422/400 带字段名、`simulate` 前后 version 不变。

全量后端：**1493 passed, 8 skipped**（132s）。

## 五、仍然存在的问题

1. **前端从未经过 `vite build`**。沙箱里 DSH 预览钩子让 `vite.config.js` 报 `Cannot find module E:\shujia\.dsh-tmp\config-module.mjs`，只能用 esbuild 逐文件语法检查。前端功能没有在真实浏览器里被自动化验证过。
2. **旧 LLM 文本标记通道尚未删除**：`backend/app/services/game_session.py:64` 的 `_legacy_item_tags_enabled()` 与 `frontend/src/api.js` 的剥标记逻辑仍在，默认不写权威状态。
3. **`routes_game.py` / `routes_scenarios.py` 仍返回 `Dict[str, Any]`**；`/api/v1` 已全部类型化并有契约测试。
4. **③ 故事线、⑦ 联网**：见第二节。
5. **旧 `/api` 与 `/api/v1` 并存**，两套行动入口，只有后者会写入诊断与领域事件。
6. **事件条件/效果不是通用脚本语言**（刻意）；`Effect.kind` 现 28 种，加新种类要同时改 `event_engine` 注册表与前端 `EffectRows`。
7. **两个历史偶发失败**：`test_timeskip_logs_world_changes_in_journal`、`test_delete_traces_for_unknown_campaign_returns_deleted_count` 曾因仓库内 DB 文件只读/锁竞争失败（`backend/app/db/trace_store.py:176`），单跑必过，本轮全量未复现。

## 六、本地验证

```bash
cd backend
TEMP='E:/shujia/.tmp' TMP='E:/shujia/.tmp' .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
```

只跑验收走查与本次新增：

```bash
cd backend
TEMP='E:/shujia/.tmp' TMP='E:/shujia/.tmp' .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider \
  tests/api/test_acceptance_story.py tests/api/test_routes_authoring.py \
  tests/services/test_authoring_session.py tests/api/test_routes_planner.py \
  tests/services/test_interaction_planner.py tests/api/test_routes_knowledge_extract.py
```

手工走一遍（无需密钥）：`python launch.py --debug` → 新建场景 → 绑定起步世界（奇幻）→
世界工作台「对话补全」加一条小路并确认写入 → 「交互预演」看当前房间哪些动作可行 → 
游戏里观察/拾取铁钥匙/开门 → 调试台三个页签分别给出调用追踪、叙事上下文与行动记录。
