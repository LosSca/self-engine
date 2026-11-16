# Changelog

本文件记录面向作者的可见变更。格式参考 Keep a Changelog，版本号遵循语义化版本。

## [Unreleased]

### Changed

- `services/game_session.py` 从 3513 行降到 3121 行：历史重建（`_rebuild_*`、`rewind`）拆到
  `services/session_rebuild.py`，静态 prompt 块拆到 `services/prompt_blocks.py`，仍是同一个
  命名空间（mixin），测试里对 `session._xxx` 的访问不受影响。
- 知识/记忆/叙事链路上的 `except Exception: pass` 收窄到具体异常类型并记日志，失败不再是静默的。
  `backend/app/api/routes_game.py` 的 28 处宽泛捕获降到 6 处（全是"这一层必须兜住"的边界，逐条写了理由）；
  `backend/app/services/game_session.py` 里不再有裸 `pass`——剩下 24 处都是后台任务或 LLM 调用边界，
  同样逐条标注。收窄过程暴露一个真 bug：Neo4j 驱动的 `ServiceUnavailable` 继承 `GqlError` 而不是
  `OSError`，按"网络类异常"收窄会漏掉它，图数据库离线时面板直接 500；现在按驱动的 `GqlError`
  基类捕获，并补了离线回归测试。

### Fixed

- **三选一按钮在战斗、敌人和任务面前也是对的**：上一轮让三条建议跟着状态走了，但还有五处它是瞎的。
  打起来之后它照旧劝你「打开气闸内门」「拿起反应堆燃料棒」——回合会被规则顶回来；隔壁的失控维修
  无人机被当成可以搭话的人，给出「向失控维修无人机开口，问这里发生了什么」；接了「稳定反应堆」之后，
  「打开反应堆门」这一步从来不会出现在按钮里，哪怕玩家就站在门前；来回踱步时三条建议会反复念
  「看」「查看对接舱」「观察四周」；而唯一的出口常常正好是刚走出来的那间房。
  现在 `services/suggestions` 的 `_room_facts` 多读五样东西：`in_combat`（`state.combat.active`
  或 `flags.combat_active`，与 `engines/rules_engine.py` 拒走时看的是同一对标记）、`hostiles`
  （敌对实体不再混进 `characters`）、`talked_to`（已经聊过的人不再被劝「问他这里发生了什么」，
  改成「接着问…」，而且这句话是把人当人来对待）、`quest`（按 `obtain_item`/`open_door`/
  `reach_location` 三种目标推导出「本房间能推进的下一个任务步骤」）与 `back_exits`（刚出来的那间房
  排到最后，只有在没有别的可写时才出现）。战斗中的顺序是打/防/撤，直接 return——这时候不该再劝你
  捡东西。新抽出的 `_fill()` 在截到三条之前，会把「有具体动词或具体对象」的建议排在「看」之前，
  所以填充项不会再挤掉真建议。
- **「观察X」「打量X」「查看X」不再是空动作**：`services/text_action_service` 的 `_resolve` 里，
  一个能解析到实体/物品/门的片段过去落回无目标的 `look`，句子的宾语被丢掉，这一回合什么也没发生；
  现在它会变成 `investigate <target_id>`（`观察四周`/`环顾四周`/`看` 仍然是无目标的环顾）。
  `engines/action_registry` 的动词表同步：「打量」进 look，「端详」进 investigate。原先
  `tests/services/test_text_action_parser.py` 里把 "look Chest" 断言成 `unsupported_target` 的
  两个用例，是把这个 bug 写成了规格，现在改成断言它真的调查了箱子/钥匙；"look <不存在的>" 仍然是
  拒绝，因为规则的「明确动作不许被猜」没变。
- **三选一按钮会跟着玩家的每一步变了**：这三条建议此前只读「世界文档写成什么样」，不读「玩家现在
  站在哪、门开没开、身边还有谁」，所以整局几乎不动，而且经常是点不动的死路——在「对接舱」站着，
  唯一的出口「气闸内门」还锁着，按钮给的是「走向内舱」（规则必拒），人物给的是隔壁走廊的
  「工程师艾拉」（规则必拒「这里一个人也没有」）。现在三条建议由本回合提交后的状态推导：
  挡路的钥匙在前（`拿起主管钥匙卡`），其次是没开的门（`打开气闸内门`），再是够得着的东西、真能
  走的出口、真正在场的人；已经拿走、已经打开的不再出现。规则链路的 SSE 与规则响应体
  （`api/routes_game._next_step_suggestions`）与开场快照（`api/routes_engine` 的 `/suggestions`）
  现在共用 `services/suggestions.suggestions_for_result` 这一个推导，两条链路不会再给出两种答案。
  `hints_from_state` 新增 `doors` / `item_locations` / `entities` 三个实时覆盖参数，不传时退回
  世界文档（旧调用方行为不变）。
- **自由输入的句子不再被当成错句**：文本链路的解析器只认动词表里的动词 + 能对上的目标，玩家写
  "蹲下来检查地上的裂缝"、"把门推开"、"sneak past guard" 一律得到「这句话没能对上眼前的任何东西」。
  现在解析顺序是：确定性解析 → （可选）意图顾问 → **尝试**。认不出动词的句子变成 `improvise`：
  规则记录这次尝试、花掉两分钟、**不改变任何状态**，玩家在行动记录里看得到自己干了什么。
  三条边界刻意保留：只写一个名字（"iron"、"Sentry"）不算尝试；把动词拼错（"openly iron_key"、
  "guardrail"）仍然是解析错误而不是"试了一下"；写对了动词但目标不存在（"拿起月亮"）仍然拒绝，
  "明确动作不许被猜"这条不变量没变。地点名也放宽了：世界是英文的（Forge），玩家说"我去铁匠铺"
  也能对上，中文别名只在地点唯一命中时才采用。
- **「继续」不再是规则链路上的一个错句**：在绑定世界的战役里，双击空格 / Ctrl+Enter / 空输入提交
  一直发的是叙述者语法 `[CONTINUE]`，而规则链路把这串东西当成一句话去匹配动词，自然对不上任何
  东西，于是玩家收到「这句话没能对上眼前的任何东西」。现在规则链路前台发的是一句正常的中文
  「等待」（世界因此走一段、NPC 照常各过各的），旧叙述者链路仍然发 `[CONTINUE]`；即使有条目
  只有标签，拒绝提示也不再把它原样念回去。
- **NPC 心智面板在规则链路里终于有内容**：面板一直从 `NPC_THOUGHT` 事件重建，而只有退休的
  `GameSession` 会写这些事件，规则链路跑得再热闹，面板也是空的。现在每个玩家回合结束、NPC
  回合走完之后，会把镇上每个有日程的 NPC 的当前活动/位置/需求记成一条 `NPC_THOUGHT`
  事件（`witnessed_by` 只写他自己），面板因此读得到真实状态，而不是猜。
- **设置面板里「自定义兼容接口」现在真的能填完**：以前有三处断点，任何一处都足以让人配不上。
  ① 面板只提交密钥和地址，模型名从不落库，于是 `/api/settings` 的 `custom_model` 永远是空；
  ② 「自定义」这个选项本身只在 `custom_route_available` 为真时出现，而那个值要"地址 + 密钥都已配置"
  才为真——没配置的人看不见入口，也就永远配置不了；③ 只填地址、密钥留空时，`apply_credentials()`
  把 key 变量清空，路由判定为不可用，面板却显示"已保存"。现在：模型名跟着地址一起存；「自定义」
  常驻选集；无密钥端点（Ollama、内网网关、按地址鉴权的厂商）用 `keyless` 标记单独记账，判定
  与 `status()` 都认它；自定义路由的地址/密钥/模型改为**调用时读环境**，面板一保存就生效，不用重启。
  顺手修掉「测试连接」：`custom` 是面板条目而不是路由供应商，探测时直接 `LLMProvider("custom")` 会报
  `'custom' is not a valid LLMProvider`；现在先映射到 OpenAI 传输再探测，并改用 512 token 预算——
  推理模型（如讯飞星火 X2）在 16 token 下会把预算全花在思考上，正文为空，而旧实现把这种空回复
  当成"连接成功"。
- **启动器不再把浏览器指到别人的页面上**：以前只要 5173 端口有人监听，`launch.py` 就判定
  「前端已在运行」并直接打开 `http://127.0.0.1:5173`，于是另一项目（常驻 5173）的页面被当成了
  self-engine，表现为“双击 start.bat 只打开个网页、游戏没起来”。现在启动器会先确认该端口上是不是
  本项目的页面（`#root` + `/src/main.jsx` 或项目标识），不是就自动顺延到 5174、5175…，并在输出里
  写明实际端口与原因；vite dev 也不再传 `--strictPort`。

### Added

- **单端口窗口**：`python launch.py --app` 会把构建好的界面和后端放在**同一个端口**上，再用
  浏览器的 app 模式打开一个无地址栏的窗口。之前窗口需要"前端 5173 + 后端 8000 + 一条反代规则"，
  这三样只要有一处不同步就是白屏；现在只有一个进程在回答两边，`/api` 与界面同源。静态托管是
  **可选开关**（`SELF_ENGINE_DESKTOP_DIST`），普通启动完全不受影响：前端路由（`/studio`）
  回落到 `index.html`，静态资源按真实 MIME 返回，路径穿越读不到构建目录之外的任何文件。
  打包成安装程序仍然走 `desktop/` 的 Tauri 壳（`npm run build`），这条路径没有改动——
  `--app` 是「没有 Rust 工具链时的单端口预览」，不是它的替代品，也不产出安装包。
  顺带修掉一个会让 Tauri 窗口空转的缺陷：`launch.py` 起后端时从不设置 `LUNAR_CORS_ORIGINS`，
  而 Tauri 页面来自 `tauri://localhost`，跨端口请求会整片死在 CORS 上；现在启动器在子进程环境里
  追加这几个来源（用户自己的 `LUNAR_CORS_ORIGINS` 照旧保留）。
- **混合式世界模拟 · Phase 5（小镇压力测试与修出来的缺陷）**：一座 5 地点、6 名 NPC 的小镇
  （`tests/fixtures/hollowmere_world.json`）作为端到端验证世界：`tests/api/test_town_simulation.py`
  用 9 条测试跑通「自由行动 → NPC 自主 → 世界时间 → 调度触发 → 7 日连贯 → 同输入同状态」。
  压力测试逼出并修掉一批真缺陷：`wait` 指定的分钟数被丢弃、时钟常量表缺 simulation 动词、
  旧会话的暂停标志让新链路的时钟永不前进、`take` 拿不了第二件同类物品（`combine` 因此
  永远缺料）、绝对分钟 0-based/1-based 混用导致第一天的日程排到"昨天"、
  `catch_up_minute` 与推进循环互相卡死（首个到期事件永不触发）、世界初始物品按地点覆盖
  （同一物品在两个地点时只剩后一个）。
- **混合式世界模拟 · Phase 4（LLM 高层决策，只提建议）**：`services/town_advisor.py` 让模型
  输出结构化建议（"120 分钟后安排一次换班"），解析后只调用 `schedule_event`——它没有写状态的
  能力，建议必须到期、经 handler、经规则验证才可能改变世界。默认关闭，设
  `ENGINE_TOWN_PLANNER=1` 打开；每个玩家回合最多询问一次，测试全用假模型，不联网。
- **混合式世界模拟 · Phase 3（NPC 自主行动）**：NPC 有了自己的日程、需求、个性和一天：
  作者在 `EntityDef.schedule` 里写"08:00 去铁匠铺上班"，`engines/npc_schedule.py` 把它编译成
  `npc.routine` 调度事件；`engines/npc_goals.py` 用一张常量权重表打分（饿了去吃、累了去睡、
  守卫见到异常去查），`engines/npc_brain.py` 只感知**自己目击过**的事件（`witnessed_by`），
  `services/npc_turns.py` 把选中的动机翻译成普通 `ActionRequest(actor_type="npc")`，
  交给同一个 `ActionService` 执行。没有第二条写路径，也没有第二份世界状态。
  新增 `travel` 与 `give_item` 两个原语（共 21 个），`EntityState` 新增 `needs` / `activity`。
- **混合式世界模拟 · Phase 2（世界时钟与事件调度器）**：新增 `scheduled_events` 表
  （迁移 5）与 `backend/app/engines/world_scheduler.py`——“18:00 关门”从此是一个**记住的**
  未来事件，而不是每回合重算一遍的推导。Domain Event 仍是“已经发生的事实”，Scheduled Event 是
  “还没发生、必须持久化且可恢复”的待办，两者分开存放。时钟不需要逐分钟轮询：
  `catch_up_minute` 直接跳到“下一个到期事件或目标时刻”，所以等待 8 小时是一次跳转，而不是
  480 次空转；有事件时按 `(due, priority, sequence_id)` 的因果顺序逐个执行。
  `ActionService` 在每次行动后自动结算这段时间内到期的事件，触发标记与状态、领域事件
  在同一个 CAS 事务里提交，任一失败都不会留下“事件已触发但状态没变”的半截世界。
  定时器 handler 抛异常不会中断时钟，而是记一条 `WORLD_TICK`（`status: failed`）。
- **混合式世界模拟 · Phase 1（统一行动模型与动作原语）**：`ActionRequest.actor_type`
  （`player` / `npc` / `environment` / `system`，默认 `player`）让玩家与 NPC 走**同一套**规则判定
  和同一条 `ActionService` 写入路径——NPC 没有独立执行链路，也不能直写权威状态。新增
  `dismantle` / `combine` / `wait` 三个动作原语（共 19 个），失败一定有 `code` 和原因
  （`cannot_dismantle` / `not_craftable` / `missing_parts` / `invalid_duration`）。
  物品的 `dismantles_to` / `made_from` / `craftable` 是**配置数据**，不是新系统；
  作者声明"这张桌子能拆成两块木板"，规则决定这次拆不拆得成。中文自然语言入口同步认识
  这三个动词（"我拆掉木桌"、"合成木盾"、"等半小时"）。
- **检索分四层**：`backend/app/services/retrieval/` 把知识/记忆/图谱召回拆成确定性过滤 → BM25 →
  向量 → 图谱，再用 RRF 融合、跨层重排。中英用同一套分词器（中文切二元组），修掉了
  `memory_engine` 旧关键词正则只认拉丁字母、中文查询得到空集的 bug；图谱召回改为按实体查询，
  不再拿事件流尾部整段文本去匹配。向量召回默认关闭，设 `ENGINE_EMBEDDING_PROVIDER`
  （`openai` / `custom` / `local`）与 `ENGINE_EMBEDDING_MODEL` 开启，向量缓存在 SQLite，
  换模型指纹自动重建。某个通道失败只会写进 `ContextStats.retrieval_degraded` 并显示在调试面板。
- **旧链路开始日落**：`POST /api/game/{id}/action` 只有规则链路一条自动路径，没绑定世界的战役返回
  409 并说明「重新绑定世界，或传 `compat_mode: true`」，不再静默切到旧 LLM 执行分支；面板读取
  （历史/日志/背包/NPC 心智/角色名册/记忆晶体/图谱搜索/回退）改为只读投影，不再为读一次而重建
  `GameSession`。
- **在设置里直接填模型密钥**（设置 → 「模型」→ API 密钥）：DeepSeek / OpenAI / Anthropic 只填 Key，
  本地或自建兼容接口再填地址（例如 `http://127.0.0.1:11434/v1`）。密钥存进本机 sqlite，写进
  `os.environ` 供 litellm 使用，接口只回显末四位；「测试连接」会真发一句 completion，失败就把
  上游的报错原样显示出来，「清除」后回落到 `.env` 里的同名变量。
- **叙事可调项**（设置 → 「叙事」页）：风格（简洁/均衡/文学/阴郁/武侠/通俗）、人称、篇幅、
  语气参考、是否点出判定结果、是否允许场外提示，以及 `top_p` / 存在惩罚 / 频率惩罚。
  这些都只是写给模型的语气提示，规则判定不受影响；空值表示跟随世界自己的设定。
- **设置全部存到后端**：新增 `user_prefs` 表与 `GET/POST /api/prefs`，主题、模型、采样参数、
  写作指令等换浏览器也还在（API 访问令牌仍然只留在本机，不上传）。
- `python launch.py --neo4j [status|start|stop|password]`：检查图谱连通性，并把「连不上 /
  认证失败 / 没在跑」翻成能直接照做的修复命令；有 Docker 时可以直接起 `neo4j:5`。
- **Tauri 桌面壳**（`desktop/`）：原生窗口 + 侧车进程，双击即可打开，不再需要浏览器和黑窗口。
  壳以子进程运行 `python launch.py --backend-only --no-window --no-browser`，健康检查通过后
  向页面注入 `window.__LUNAR_API_BASE__`；前端只改了 `frontend/src/api.js` 一行 BASE 取值，
  浏览器打开时的行为完全不变。构建方式见 `desktop/README.md`。
- 新增 `LICENSE`（MIT，与 README 声明一致）。

### Changed

- **纸白主题不再出现黑块**：实心按钮从纯黑改成中性深灰 `#4a453f`，侧栏与停靠面板改成
  暖灰 `#efece6`，白底卡片保持纯白；深色主题不受影响。

### Removed

- 清理一次性脚本与散落产物：`cli.sh`、`run_gen.sh`、`narrative_*.txt`、`tone_current.txt`、
  `opening_current.txt`、`scenario-sword-god.json.bak-item3`、`setup_wizard_plan.md`、
  `narrative_coherence_plan.md`、`PLANO.md`（葡语旧计划）、`scenario_clone_op/`（海贼王迁移一次性产物）、
  `.claude/`、`frontend/README.md`（Vite 模板残留）、`proxy/` 里未被 README 提到的旧 Python 代理。
- 文档去重：`docs/audit_phase0.md`（与 `docs/phase0_audit.md` 重复）、`docs/delivery_phase1.md`、
  `docs/audit_systems_13.md`、`docs/review_followups_2_to_5.md`、`docs/plans/`、`docs/tickets/`（葡语工单）、
  `docs/stage14_knowledge_extract_and_planner.md`（已被 `stage14_world_language.md` 取代）。
- `docker-compose.yml` 与安装脚本里的 Neo4j 容器步骤：Neo4j 本来就是可选镜像，改成在文档里说明
  自己起服务并填 `NEO4J_URI`。

从「模型驱动的聊天」推进到「规则驱动的引擎 + 模型叙事」：新增一句话生成剧本、作品穿越、
任务编辑与推进、叙事诊断、内容策略与本地回退，并让叙事语言跟随世界。

### Fixed（前端真的能在浏览器里渲染了）

- **`launch.py --prod` 生产托管**：先 `npm run build`（默认只在 `dist` 缺失时自动构建，`--rebuild` 强制），
  再由新增的 `scripts/serve-dist.mjs` 在 4173 端口托管 `frontend/dist`，并把 `/api`、`/media` 反代到后端。
  不依赖 `vite preview` 的中间件顺序，托管时过滤掉构建产物里残留的
  `<script type="module" src="/src/main.jsx">`（它会让浏览器把源码当模块再拉一次而白屏）。
- **桌面快捷方式默认 `--prod`**：受限环境里 `vite dev` 无法预打包依赖，裸导入 404 → 白屏；
  构建产物是一份打包好的 js，在任何环境下都能渲染。想热更新就把快捷方式里的 `--prod` 去掉
  （`launch.py` 没有 `--dev` 参数，不带 `--prod` 就是开发模式）。
- `launch.py` 的 `_temp_dir()` / `log_path()` 回退链、前端端口按模式取 4173 或 5173、
  `--prod` 只要求 node 而不要求 `frontend/node_modules`。

### Added

- **一句话生成剧本**：`POST /api/v1/scenarios/author`，一次调用返回世界 + 知识 + 剧情线，三件各自独立校验，
  失败件进 `warnings`；`save=false` 为试运行。世界工坊新增「剧本生成」页签。
- **作品穿越**：`GET/POST /api/v1/works*`，内置六部作品档案（西游记、福尔摩斯、三体、流浪地球、哈利·波特、指环王），
  按档案生成链式连通的世界骨架并可挑选角色；`allow_model=false` 时不花模型调用。
- **任务编辑**：`GET|POST|PATCH /api/v1/worlds/{w}/quests` 到目标与奖励级，每次编辑跑完整世界校验；
  `POST /campaigns/{c}/quests/{q}/objectives/{o}/progress` 在调试开关下按真实提交路径推进目标。
- **叙事诊断**：`narrative.diagnostics` 记录 provider/model/采样参数/策略/上下文块与 `fact_counts`，
  失败出口同样携带；`GET /campaigns/{id}/requests` 列出请求记录。调试台扩展为三个页签。
- **内容策略与本地回退**：`content_policy`（standard/nsfw），上游拒绝时以哨兵标记且不转发拒绝文本，
  叙事层整条改走本地模型重试一次；流式路径同样处理。
- **叙事语言**：`WorldDefinition.language` 决定描述语言，确定性叙事器、效果描述与模型调用三处都遵守。
- **起步世界预设**：奇幻村庄、轨道站、雾镇、荒野营地，中英双语，`GET /api/v1/world-presets`。

### Added（角色库：角色不再属于世界）

- **全局角色库**：`GET /api/characters` 列出不属于任何世界的角色，`POST /api/characters` 直接创建，
  `POST /api/characters/{id}/library/import` 把世界里的角色复制一份收进库（幂等，源行不动）。
  `scenario_store` 迁移到 13：`scenario_characters.scenario_id` 允许空串表示库角色，行按 `SELECT ... INSERT ... DROP ... RENAME` 原样搬迁；
  `delete_scenario` 只清理属于该世界的行，删世界不清库。
- **跨世界选角**：`PUT /api/campaigns/{cid}/character` 删掉了「角色属于另一个世界」的 409，
  任何世界都能用库里任何一个角色开局；`GET /api/scenarios/{id}/characters` 改为「本世界的角色 + 库角色」。
- **开局叙事知道你是谁**：`GameSession` 新增 `player_character` 参数，`_build_character_setup_block()`
  在玩家没填自定义设定时也会把「玩家角色：名字 / 身份 / 描述」注入叙事上下文。
- **前端**：首页新增「角色库」页（`/characters`，`CharacterLibrary.jsx`：创建、改名、换身份、立绘差分的增删改、收录世界角色）；
  SetupWizard 的角色步骤同时列本世界角色与库角色并标「· 库」，没有立绘的角色不再被整步跳过；
  CharacterProfilePanel 的候选列表同样合并两处。
### Added（自定义 OpenAI 兼容供应商）

- **接任何兼容接口**：`app/config.py` 新增 `custom_llm_base_url` / `custom_llm_api_key` / `custom_llm_model`，
  设置面板出现「自定义兼容接口」一项（只在 `.env` 里 URL 与 KEY 都齐了才显示）。
  选中时前端提交 `provider: "openai"`，`apply_model_policy()` 发现自定义端点可用就跳过内置的
  `_AUXILIARY_MODELS` 表，让选定模型同时担任叙事与辅助两个角色。
  `LLMRouter._get_api_base()` / `_get_api_key()` 返回 `CUSTOM_LLM_*`，`_build_model_string()` 返回
  `openai/<model>`，litellm 把裸模型名 POST 到该 `api_base`。`OPENAI_PROXY_URL` 仍然优先。
- `GET /api/settings` 新增 `custom_route_available` 与 `custom_model`，**不返回密钥与 base URL**。
- 文档 `docs/custom_llm_provider.md`；`.env.example` 补 `CUSTOM_LLM_*` 注释。

### Added（第三轮：六个架构阶段）

- **角色路由**（阶段 1）：`app/engines/agent_router.py` 按角色（narrative / system / memory）选模型，
  全部走 `AGENTS_<ROLE>_*` 环境变量，代码里没有一个模型名；`AgentRuntime.ask()` 永不抛异常，
  失败降级到下一角色，并把每次调用记进 `agent_calls` 表。`GET /api/v1/agents` 只报配置与可用性，从不回显密钥。
  `app/engines/system_agent.py` 先做确定性分类，只有长断言才问模型。
- **NPC 档案与分层玩家上下文**（阶段 2）：`app/engines/npc_dossier.py` 让 NPC 变成每回合按数据拼出来的档案
  （性格、心情、秘密、知道与不知道的事）；`app/engines/player_context.py` 给上下文打
  `fact` / `canon` / `belief` / `guess` / `history` 标签。两个 NPC 共用一个引擎，各自只看到被允许知道的。
- **世界演化**（阶段 3）：`app/services/world_evolution.py` 把断言变成提案——
  `POST /worlds/{w}/evolution/propose`、`GET .../proposals`、`.../approve`、`.../reject`。
  提案在世界副本上经 `authoring_session.stage_ops` 校验，**没有真的写入就不叫已采纳**；
  传闻与个人猜测永不进正史。世界工坊新增「世界演化」页签（共 13 个）。
- **记忆来源**（阶段 4）：四层记忆晶体每条带 `source` / `observer` / `confidence` / `witnessed_names`，
  叙事提示里用 `provenance_label()` 标出「谁说的、多可信」。
- **游戏时钟**（阶段 5）：`app/engines/game_clock.py` 只在玩家行动时推进，暂停的战役不花时间；
  `ActionResult` 多出 `minutes_spent` / `clock`，`GameState` 多出 `day` / `minute`（都有默认值，旧存档照常打开）。
  新接口 `POST /api/game/{id}/pause`、`/resume`、`GET /api/game/{id}/clock`。
- **六个场景跑成测试**（阶段 6）：`backend/tests/services/test_architecture_acceptance.py`
  用真实入口与真实 SQLite 覆盖 NPC 对话、玩家补世界、世界长剧情、知识隔离、离开游戏、模型失败。

### Changed

- 数据库 `game_state.db` SCHEMA_VERSION 2 → 4：新增 `agent_calls`（v3）与 `evolution_proposals`（v4），
  纯增量、打开旧库时自动迁移，没有删除或改写任何历史行。
- 调试台新增「模型调用」页签（角色路由 + 每次调用记录）；前端接线由 `scripts/check-agent-wiring.mjs` 守住。

### Fixed

- 演化提案的「已采纳」只在 `applied` 里真的出现这条提案时才写：
  之前即使 `stage_ops` 因校验失败没写进草稿，报告也会说成功。

### Added（第二轮）

- **对话式补全世界**：`POST /api/v1/worlds/{w}/authoring/propose` 把一句话变成校验过的编辑提案，
  `POST /api/v1/worlds/{w}/authoring/commit` 才落盘；世界文档逐条走 `world_editor` 校验读改写，
  引用悬空的提案当场被拒并给出原因。世界工坊新增「对话补全」页签（共 12 个）。
- **从叙事文本抽取记忆**：`POST /api/v1/scenarios/{s}/knowledge/extract`，确定性句子分类与句式事实优先，
  模型只做改写，写入只进知识库；`apply=false` 为试运行且完全幂等。
- **只读交互预演**：`POST /api/v1/worlds/{w}/plan`、`/preview-actions`、`/explain`，
  判定全部来自真实 `RulesEngine` 与 `event_engine`，状态深拷贝，不写回。

### Changed

- `/api/v1` 全部路由改为类型化响应模型，并新增契约测试 `tests/api/test_routes_contracts.py`。
- 世界工坊页签增至 13 个（新增记忆提炼、对话补全、世界演化），调试台增至 4 个（新增模型调用）；
  README 重写（路由清单、行动结果形态、编辑器与调试入口）。

### Fixed

- 中文世界的叙事不再输出英文句子（地点名中文、句子英文）。
- 引擎回合的散文不再携带 `[ITEM_ADD:...]` 一类控制标记。

### Tests

- 后端：1635 passed / 8 skipped。验收走查见 `backend/tests/api/test_acceptance_story.py`，
  架构六场景见 `backend/tests/services/test_architecture_acceptance.py`。
- 前端：本环境无法 `vite build`，只做了逐文件语法检查；这一项仍未验证。
## [0.5.0] - 2026-11-01

作者工具升级：地图编辑器重做、更多世界预设、SillyTavern 风格模型预设，以及知识/剧情线/世界三处 AI 一键生成。

### Added

- **世界预设**：新增轨道站事故、雾镇档案、荒野营地三套完整世界（区域、坐标、物品、门、NPC、任务、事件），与原有村庄模板一起在工坊选择。
- **地图编辑器**：网格画布、区域虚线分区、节点卡片、曲线路线与门锁标记、滚轮缩放、空白拖拽平移、节点拖拽、缩放工具栏；选中节点后可把当前位置写回世界坐标。
- **SillyTavern 风格预设**：top_p/top_k/min_p、重复/存在/频率惩罚、seed、停止词、上下文窗口、预设系统提示；叙事调用会转发这些采样参数，未设置的参数不下发。
- **AI 生成管线**：
  - 知识库：提示词 → 结构化知识草稿 → 预览 → 全部写入。
  - 故事线：提示词 → 完整 Storyline 草稿（含校验提示）→ 载入编辑器继续修改。
  - 世界工坊：提示词 → 校验引用 → 直接保存为新世界并打开。
  - 生成接口统一使用已配置模型或指定预设；未配置模型返回 503，非法 JSON/引用返回 502。

### Changed

- 数据库 schema 升级到 v12（模型预设 `extras_json`）。
- 模型预设表单与编辑器扩展；新增 dialogue/summary 角色选项。
- 故事线拆分为独立的「故事线工坊」：首页情景卡直接进入，采用卡片与编辑面板；世界工坊不再包含故事线标签，KnowledgeStudio 只保留模式、预算与模型预设。
- 世界地图改为地理地图视觉：地形分区色块、道路/小径、地点标记、区域标题、指北针与比例尺；仍支持拖拽、缩放与坐标写回。

### Tests

- 后端：1238 passed / 8 skipped（跳过项为未启动 Neo4j 的图谱测试）。
- 新增 AI 生成单元/接口测试、预设采样器与 extras 持久化测试。
- 三套新增世界模板均通过后端世界引用校验。

### Known Limits

- AI 生成需要已配置的云端或本地模型；离线环境只显示明确错误。
- 地图编辑器是区域/节点画布，不是瓦片地形或碰撞网格。
- SillyTavern 子集只覆盖采样器与停止词，未实现其角色卡/世界书/上下文模板系统。

## [0.4.0] - 2026-11-01

补齐选择、角色化模型配置、稀疏检索、长期摘要、装备与经济，并给出可验证的 PostgreSQL 数据迁移导出。

### Added

- **玩家选择**：事件可直接定义至少两个选项（或引用世界 `choices` + `offer_choice` 效果）；触发后写入权威 `pending_choice`，`choose` 行动原子应用选项效果并清除挂起状态，游戏界面显示选项按钮。
- **按角色模型路由**：场景可配置 `intent_preset_id`，意图顾问优先使用该预设的 provider/model；原有叙事预设继续生效。
- **提示词模板**：场景可配置 `narrator_prompt`，追加在强制规则之前，不能推翻已结算结果。
- **稀疏向量检索**：知识上下文使用本地 TF-IDF 余弦相似度（英文词 + 中文字/二元组），与聚焦标签、剧情提升、预算共同排序。
- **长期摘要**：规则链路每回合追加有界滚动摘要（最近 12 条、2000 字符），并作为独立记忆块注入叙事。
- **装备与经济**：物品可设装备槽与攻击/防御加成、交易价值；新增 `equip/unequip` 行动与 `equip_item/unequip_item` 效果；商人实体可配置商品，`buy/sell` 按价格与货币原子结算；货币、经验、等级与对应条件/效果进入权威状态。
- **对话状态机与预设**：NPC 台词按实体持久游标顺序推进并循环；`talk` 叙事可使用场景 dialogue 预设。
- **LLM 长期摘要**：配置 summary 预设后每 4 回合压缩近期摘要，失败保留确定性滚动摘要。
- **PostgreSQL 数据导出**：`backend/scripts/sqlite_to_postgres.py` 生成分 schema 的 SQL，见 `docs/postgres-migration.md`。

### Changed

- 数据库 schema 升级到 v10（`intent_preset_id`、`narrator_prompt`）。
- 时间线/调试面板显示挂起选择、装备、货币、经验与等级。
- 前端版本与后端版本统一为 0.4.0。

### Tests

- 后端：1228 passed / 8 skipped（跳过项为未启动 Neo4j 的图谱测试）。
- 前端构建与 hook 顺序检查通过。

### Known Limits

- PostgreSQL 目前只有一次性数据导出，运行时仍为 SQLite；连接、UPSERT、锁与迁移器适配未完成。
- 经济有地图购买按钮与文本买卖，没有独立商店/背包交易面板；装备只影响攻击/防御。
- 意图角色已可按场景路由，judge/dialogue 仍用全局模型。
- 知识是本地稀疏词向量（TF-IDF），不是 embedding 语义模型；长期摘要仅在配置 summary 预设时调用模型，否则保持确定性文本。
- NPC 对话是作者台词顺序推进，不是分支对话树。
- NPC 自主行为、可走格子/碰撞仍未实现。

## [0.3.0] - 2026-10-31

把探索、容器、关系与运维能力补齐：调查与路线移动进入统一行动管线，实体拥有权威关系/警戒状态，角色拥有技能，运行时可鉴权并可导入导出存档。

### Added

- **调查**：`investigate` 内置动作，可查看在场实体、物品、门与相邻地点；`调查/检查/查看/inspect` 不再被误判为观察或交谈。
- **路线移动**：`move` 接受经过校验的 `path`，逐段检查出口、门与钥匙；地图新增“沿路线前往”，由 BFS 生成路径后仍走规则链路。
- **关系与警戒**：实体新增 `faction`、运行时 `relations` 与 `alert`；新增 `entity_relation / entity_alert` 条件与 `adjust_relation / adjust_alert` 效果；攻击会提升警戒并在命中时降低对玩家的关系。
- **技能**：世界可设 `starting_skills`，角色状态保存 `skills`；新增 `skill` 条件与 `skill_change` 效果，调试面板展示技能、关系、警戒与实体持有物品。
- **事件执行树**：执行记录携带 `parent_event_id` 与 `depth`，调试历史按层级缩进并显示分支标签。
- **鉴权**：设置 `API_TOKEN` 后所有 `/api`（健康检查除外）要求 Bearer 或 `X-API-Token`，默认关闭。
- **存档导入导出**：`GET /campaigns/{id}/export` 与 `POST /campaigns/{id}/import`（覆盖需 `reset=true`），调试面板可下载/上传 JSON。
- **编辑器**：地点区域与坐标、实体阵营/容器物品、新条件与效果（含阈值 `count`）均可配置。

### Changed

- 前端版本与后端版本统一为 0.3.0。
- 清理本轮涉及前端文件的 lint 错误；旧审计文档标注为已被 `review-overview.md` 取代。

### Tests

- 后端：1214 passed / 8 skipped；跳过项全部是未启动 Neo4j 的图谱测试。
- 区域/坐标与容器验收 11/11；此前的事件分支、NPC 知识、13 项验收保持通过。
- 前端构建与 hook 顺序检查通过。

### Known Limits

- 地图有区域、坐标和路线规划，但仍无可走格子、碰撞与自由寻路。
- 无装备栏、经济系统与技能成长曲线；技能只是可配置整数与检定阈值。
- 模型预设只覆盖叙事；intent/judge 未按角色路由，提示词模板不可在线编辑。
- 知识仍是标签/位置/剧情检索，没有向量召回；长期摘要只在旧聊天链路。
- SQLite 单写者；未提供 Postgres 迁移。

## [0.2.0] - 2026-10-30

首个完整的规则引擎审阅版本：事件、任务、行动、知识与界面统一到同一条确定性链路。

### Added

- **剧情线**：主线/分支/独立剧情三类节点、剧情线分组与整线启停、节点归属、多节点知识提升、进度与阻塞原因调试面板、编辑器入口。
- **事件**：成功后续事件、失败后续事件、冷却回合、最大触发次数、条件结果分支、同回合固定点级联、执行记录中的分支标签；校验与模拟同步支持。
- **知识**：按当前位置/在场 NPC/行动目标聚焦排序、字符预算与超限原因、限定 NPC 的可见边界、多节点联合提升、预览排除原因。
- **地图与背包**：地点可设区域与网格坐标，地图按坐标摆放并显示区域；地图内“当前位置/前往/在场交谈”规则导航；绑定世界的背包读取权威存档并可走规则使用物品。
- **容器与所有权**：实体可携带/容纳物品，新增 `give_entity`、`take_entity` 效果与 `entity_item_count` 条件；自然拾取可直接从在场容器取物，`look` 会列出容器内容。
- **模型**：场景预设的 provider/model 参与叙事调用；显式供应商选择不再回落到默认供应商。
- **文档**：新增 `docs/review-overview.md`（运行时结构、13 项验收矩阵、风险登记、路线图、文件索引）。

### Changed

- 文字输入、结构化按钮与地图点击统一进入 `ActionService`，共用规则、事件、任务与存档链路。
- 绑定世界的战役从权威记录重建历史；旧回溯接口返回 409，避免与权威状态分叉。
- 故事线模式使用全部已进入节点提升知识；自由模式不再受历史剧情节点影响。
- 数据库 schema 升级到 v9（新增 `context_budget_chars`、`audience_npc_ids`），旧库自动迁移。
- 前端版本与后端版本统一为 0.2.0。

### Fixed

- 条件运算符（`ne/gte/lte/gt/lt`）在门、地点、实体条件上被忽略；未知实体条件未失败关闭。
- 非法事件效果（如非数字生命值）导致整次行动崩溃且无记录。
- `once/always` 事件历史、失败后续、跨优先级级联与模拟结果不一致。
- 任务奖励部分提交、显式完成不发奖励、标记/数量目标不推进、接取绕过前置与条件。
- 文本行动被静默改写（`open iron_key` → take、`sneak past guard` → talk）、跨冒险重放泄漏、并发拒绝抛出完整性错误。
- 移动后的 NPC 无法被文字解析、战斗目标仍按初始位置判断、战斗中允许普通移动。
- 自由模式残留剧情知识提升；成功与拒绝动作的知识排序不一致。
- NPC 摘要按见证者并集扩散私密信息，永久记忆层绕过可见性过滤。
- 引擎叙事缺失场景语言/基调/开场、事件描述未进入叙事、引擎历史在界面上空白。
- 规则战役的“撤销上一步”只删聊天记录、不改权威状态。

### Tests

- 后端：1203 passed / 8 skipped。
- 13 项系统端到端验收：26/26。
- 事件后续/冷却/次数与知识聚焦/预算验收：16/16。
- 事件分支与 NPC 知识可见性验收：11/11。
- 区域/坐标与实体容器验收：11/11。
- 前端构建（含 hook 顺序检查）通过。

### Known Limits

- 地图已有区域分组与坐标，但仍是地点图：没有可走格子、碰撞与自动寻路。
- 实体已有容器/所有权与物品转移；NPC 警戒、关系与阵营仍不在权威规则层。
- 角色无技能成长、装备栏与经济系统；调查/潜行依赖自定义行动。
- 知识无语义/向量召回；模型超时与重试未按功能独立配置。

## [0.1.0] - 内部基线

早期世界/规则/知识/剧情线与编辑器的基础实现；早于本 changelog 建立，细节见 Git 历史。
