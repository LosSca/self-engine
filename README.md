# 自指引擎 Self-Reference Engine

<p align="center">
  <strong>规则决定事实 · 事件记录变化 · 状态保存结果 · 模型只负责叙事</strong>
</p>

<p align="center">
  版本 0.5.0 &middot; MIT License &middot;
  <a href="#快速开始">快速开始</a> &middot;
  <a href="#核心特性">核心特性</a> &middot;
  <a href="#架构">架构</a> &middot;
  <a href="#api-概览">API</a> &middot;
  <a href="#测试与质量">测试</a> &middot;
  <a href="#已知限制">已知限制</a>
</p>

> 这里刻意没有横幅图片：仓库里原先的横幅是上游 Project Lunar 的旧图，与现在的项目不符，已随审查一并删除。
> 想加图就自己放一张进 `docs/`，再把本段替换为
> `<p align="center"><img src="docs/<你的图片>" alt="Self-Reference Engine" width="100%" /></p>`。

---

## 这是什么

自指引擎是一个**本地优先**（local-first）的文字冒险与叙事 RPG **引擎**，不是一个具体的游戏。
作者用结构化的世界文档（地点、物品、门、实体、事件、任务、剧情线、知识）写出一个世界，
玩家用自然语言或界面按钮行动，引擎给出确定的结果，语言模型只负责把已经结算的事实写成散文。

它解决的是 AI 文字冒险最常见的失败模式：**模型被当成裁判**。在这里，模型永远是叙述者——
它拿不到写状态的权限，叙事失败也不会让回合回滚。

**自指**指的是引擎的建模方式：世界规则以数据形式存在（条件、效果、事件、任务都是 JSON，
没有脚本、没有 `eval`），而引擎自身的判定过程同样被记录为可回放的事件流；同一回合内
事件、任务奖励与剧情推进按固定点收敛，"引擎如何得出结论"和"世界里发生了什么"是同一套记录。

- 当前版本：**0.5.0**（`backend/app/main.py`、`frontend/package.json`、`desktop/src-tauri/tauri.conf.json` 一致）
- 运行时：Python 3.10+ / FastAPI 后端，React 19 + Vite 7 + Tailwind 3 前端，SQLite 存储
- 语言：界面与内容中文优先；**叙事语言跟随世界文档的 `language` 字段**，未声明时为英文
- 规模：**123 条 API 路径**（159 个操作）。其中 `/api/v1` 规则链路 73 条，旧聊天链路 `/api/game` 24 条，
  情景与作者工具 `/api/scenarios` 12 条，角色库 `/api/characters` 6 条，供应商 `/api/providers` 3 条，
  健康检查 2 条，全局设置 `/api/settings`、偏好 `/api/prefs`、战役角色 `/api/campaigns` 各 1 条
- 代码：后端 77 个 Python 模块、127 个测试文件；前端 29 个组件，全部按路由懒加载
- 模型：**不配置任何密钥也能完整游玩**——确定性叙事器按世界语言产出文本

### 一句话能做的事

| 你想要的 | 怎么做 | 需要模型吗 |
|----------|--------|-----------|
| 一句话生成世界、知识、主线 | 世界工坊「剧本生成」页签，或 `POST /api/v1/scenarios/author` | 需要 |
| 借用已有作品的世界与角色 | 世界工坊「作品穿越」页签，六部内置作品，也可让模型补写 | 内置作品不需要 |
| 从零手做一套世界 | 世界工坊十三个页签：世界地图 / 剧本生成 / 物品 / 门 / 实体 / 事件 / 任务 / 行动 / 调试 / 作品穿越 / 记忆提炼 / 对话补全 / 世界演化 | 不需要 |
| 让玩家一句话被写进正史 | 世界演化页签：断言 → 提案 → 采纳，校验不过的不进世界 | 不需要 |
| 输入框旁边永远有三个可点的下一步 | `GET /api/v1/campaigns/{id}/suggestions`；规则链路自己算，不花模型调用 | 不需要 |
| 让作者草稿"长一点" | 角色库 / 情景工坊的「生成」「继续写」按钮；可勾选联网取材 | 需要 |
| 换个模型只换一个角色 | `AGENTS_NARRATIVE_MODEL` 等环境变量，或调试台「模型调用」页签看路由 | 不需要 |
| 换一个起步世界 | `GET /api/v1/world-presets`：奇幻村庄、雾镇、轨道站、荒野营地 | 不需要 |
| 让模型写得更好 | 设置面板选供应商与预设；上游拒绝时可自动改走本地模型 | 需要 |
| 用别家的兼容接口 | 设置面板「自定义兼容接口」，地址与密钥放 `.env` 的 `CUSTOM_LLM_*` | 需要 |
| 换个设备接着玩 | 设置统一存服务端 `/api/prefs`，换浏览器仍然生效（令牌不进去） | 不需要 |

### 代码地图

| 位置 | 内容 |
|------|------|
| `backend/app/engines/` | 规则与引擎层：行动目录、规则判定、事件与效果、任务、剧情、战斗、记忆、NPC 心智 |
| `backend/app/services/` | 编排层：`action_service` 是唯一写入口；另有解析、知识、叙事、世界编辑、AI 生成、作品库、草稿与联网取材 |
| `backend/app/api/` | 六个路由模块（引擎 / 知识 / 旧链路 / 情景 / 角色 / 作品） |
| `backend/app/db/` | 六个存储模块：情景、权威状态、事件、调用追踪、偏好、供应商凭据（后两者与情景库同库） |
| `backend/tests/` | pytest **1700 passed / 8 skipped**，验收走查在 `tests/api/test_acceptance_story.py` |
| `frontend/src/components/` | 游戏画布、侧边栏与工具坞、各工坊、面板与浮层 |
| `docs/` | 阶段设计、审阅矩阵、风险登记、交付审查与 `docs/review-2026-11-15.md` 全量审查 |

---

## 架构

旧的架构图描述的是单一聊天链路，已与实现不符并在审查中删除；下面是当前的文字框图（ASCII 不会过期）：

```
浏览器 (React 19 + Vite 7)
  ├─ App shell ─────── Sidebar（左侧导航 + 新手引导）+ 全页面设置/工坊入口
  ├─ GameCanvas ────── 剧情流 / 文字输入 / 下一步建议 / 迷你地图 / 工具坞
  ├─ WorldStudio ───── 地图·剧本生成·物品·门·实体·事件·任务·行动·调试·作品穿越·记忆提炼·对话补全·世界演化
  ├─ KnowledgeStudio ─ 知识·上下文模式·剧情线·模型预设
  └─ StorylineStudio / ScenarioBuilder / SetupWizard / SettingsPanel ...
        │  REST (fetch)
        ▼
FastAPI (backend/app/main.py)
  ├─ /api/v1   …… 规则链路（确定性）      routes_engine.py
  ├─ /api/v1   …… 知识·预设·AI 生成       routes_knowledge.py
  ├─ /api/v1   …… 作品库·世界穿越·角色挑选 routes_works.py
  ├─ /api/game …… 旧聊天链路（流式 SSE）  routes_game.py
  ├─ /api/scenarios …… 情景·作者草稿·联网取材 routes_scenarios.py
  ├─ /api/characters …… 角色卡与外观      routes_characters.py
  └─ /api       …… 全局设置·偏好·供应商·健康检查 main.py
        │
        ▼
ActionService.execute            唯一的状态写入口 (services/action_service.py)
  1 载荷校验 + request_id 幂等
  2 RulesEngine.evaluate         纯函数判定，无模型参与
  3 应用效果 → 事件固定点 → 任务 → 剧情线
  4 知识上下文（分层/剧情提升/聚焦/预算/NPC 可见性）
  5 单事务提交 state + request + domain events（版本 CAS）
  6 叙事                        模型只描述已提交的结果；失败只记录
  7 回写本轮叙事与摘要（不改版本号，不动已提交状态）
        │
        ▼
SQLite（路径均可用环境变量覆盖）
  scenarios.db   情景·故事卡·知识·剧情线·模型预设·角色·用户偏好·界面里填的供应商密钥  SCENARIO_DB_PATH
  game_state.db  世界·权威状态·请求记录·领域事件·快照·模型调用    GAME_STATE_DB_PATH
  events.db      旧链路事件流·记忆晶体                           EVENT_DB_PATH
  traces.db      LLM 调用追踪                                    LLM_TRACE_DB_PATH
        │
        └─(可选) Neo4j 知识图谱镜像 —— ENGINE_GRAPH_MIRROR=1 且配置密码时启用
```

**关键不变量**（见 `docs/review-overview.md`、`docs/delivery_review.md` 与 `docs/review-2026-11-15.md`）：

- 模型不写状态；叙事失败不回滚回合。
- **状态先落地，叙事后发生**：模型只看见已经提交的回合，所以散文无法夹带物品、开锁或任务完成。
- 条件、效果、后续事件都是校验过的数据，没有脚本或 `eval`。
- 事件、任务奖励、剧情推进在同一回合内按固定点收敛，同一事件每回合最多尝试一次。
- 地图点击、结构化按钮与自然语言共用同一个 `ActionRequest` 管线。
- 同一 `request_id` 重放返回同一份结果，不重复扣物品、不重复发奖励。
- 叙事、图谱镜像、摘要的失败只记录，不撤销已提交的回合。
- 所有写状态的操作（含调试回滚、修复）都带版本 CAS，冲突返回 409 而不是静默覆盖。

---

## 核心特性

### 确定性规则链路

内置行动是声明式目录（`backend/app/engines/action_registry.py` 的 `ActionSpec`），
每个行动声明 name / label / handler / target / verbs，`RulesEngine` 按动词表分发。

当前 **16 个行动**（`GET /api/v1/action-types` 可读到全部，含中文动词）：
`attack` 攻击、`flee` 逃跑、`defend` 防御、`take` 拾取、`open` 打开、`use` 使用、
`talk` 交谈、`move` 移动、`investigate` 调查、`look` 观察、`equip` 装备、
`unequip` 卸下、`buy` 购买、`sell` 出售、`choose` 选择，以及世界可扩展的 `custom`。

```bash
# 看当前构建支持的全部行动（含中文动词）
curl http://127.0.0.1:8000/api/v1/action-types
```

### 效果注册表：从分支链变查表

`backend/app/engines/event_engine.py` 的 `apply_effect()` 曾是一条 400 多行的
`if kind == ...` 分支链。现在是 13 行查表：

```python
kind = effect.kind
handler = EFFECT_HANDLERS.get(kind)
if handler is None:
    return AppliedEffect(effect, False, f"unsupported effect kind '{kind}'")
return handler(kind, state, world, effect)
```

每种效果都是独立函数，用 `@register_effect("give_item")` 登记；重复登记直接抛
`ValueError`，未知种类**失败关闭**并给出可读原因。当前 28 个效果种类对应 25 个处理函数
（货币、关系/警戒/技能三类各自合并），见 `docs/stage6_effect_registry.md`。

### 事件、任务与剧情线

- 事件支持触发条件、优先级、`once`/`always`、成功后续、失败后续、冷却、次数上限、
  条件分支（`branches[]`，无条分支兜底且必须在最后）与同回合级联。
- 事件可挂起玩家选项（`offer_choice` / `resolve_choice`），`pending_choice` 写入权威状态，
  `choose` 行动原子应用选项效果并清除挂起状态。
- 任务有前置任务、接受条件、失败条件与奖励；奖励与状态在同一事务内提交。
- 剧情线分主线/支线/独立线，可分组启停，节点带知识。

### 知识与上下文

`backend/app/services/knowledge_service.py` 负责把"作者设定"喂给叙事层：

- 分层、深度、上下文模式、剧情提升、聚焦标签、字符预算共同决定注入哪些条目。
- 检索分四层（`backend/app/services/retrieval/`）：确定性过滤（模式/深度/受众/预算）→
  **BM25** 词法召回（中英同一套分词器，中文切二元组）→ **多语言向量**语义召回（可选，
  未配置 embedding 时整层跳过）→ **知识图谱**关系召回（可选，按实体而非整句查询），
  三路结果用 **RRF** 融合，再由跨层重排按作者意图（剧情提升/聚焦标签/逐字命中/词项覆盖）定序；
- 每个通道都可单独失败：失败的通道记进 `ContextStats.retrieval_degraded` 并显示在调试面板，
  不再是"知识整块静默消失"；
- 打分只对**通过过滤、真的会进 prompt** 的条目做，条目再多也不会退化成平方级排序。
- 向量检索默认关闭；开启需设 `ENGINE_EMBEDDING_PROVIDER`（`openai` / `custom` / `local`）
  与 `ENGINE_EMBEDDING_MODEL`，向量缓存放在 SQLite（`ENGINE_EMBEDDING_CACHE`，
  默认 `ENGINE_STATE_DIR/embeddings.db`），换模型指纹会自动重建。
- 知识条目可设 `audience_npc_ids`：只有当前在场且命中受众的 NPC 才会看到该条目。
- 作者设定（canon）与已提交历史（history）走两条独立通道，防止模型把过去的叙事当成指令。

### 记忆与历史

- **4 层晶体金字塔**（`backend/app/engines/memory_engine.py`）：
  SHORT（约 4 个行动）→ MEDIUM（4 个 SHORT）→ LONG（4 个 MEDIUM）→ MEMORY（4 个 LONG，永久世界事实）。
- 晶体是结构化 JSON，严格保留专有名词与物理描述；NPC 专属事实带见证过滤。
- 规则链路每回合追加**有界滚动摘要**（上限 2000 字符，超出从最旧的回合开始丢）；
  配置 summary 预设后可每 4 轮用模型把它压得更短。

### NPC、战斗与经济

- NPC 心智记录感受、目标、对玩家看法与秘密计划（`npc_mind_engine.py`）；
  实体拥有阵营、关系、警戒状态；对话按作者台词游标顺序推进。
- 战斗采用**创造性评分**：`final_quality = coherence × 0.4 + creativity × 0.4 + context × 0.2`，
  结果分 `CRIT_SUCCESS` / `SUCCESS` / `FAIL` / `CRIT_FAIL`，并有反"元游戏"拦截
  （`combat_engine.py`）。战斗完全由规则打分决定，模型只描述结果。
- 物品支持拾取条件、使用效果、装备槽与攻击/防御加成、价格；商人实体可配置商品，
  `buy`/`sell` 原子结算货币。
- 每个战役有自己的 `NpcMindEngine` / `JournalEngine` / `InventoryEngine` 实例：
  同时开两个战役，NPC 记忆与背包不会互相串味。

### 世界与地图编辑

- `WorldStudio` 有**十三个页签**：世界地图、剧本生成、物品、门、实体、事件、任务、行动、调试、
  作品穿越、记忆提炼、对话补全、世界演化。每次保存都跑完整世界校验；不通过的编辑不会落库。
  调试页提供事件校验与模拟、执行树、状态诊断、快照回滚与存档导入导出。
- 地图是地理视觉画布：地形分区、道路、地点标记、门锁标记，支持缩放/拖拽与坐标写回；
  `move` 可接受经 BFS 校验的路线逐段移动。
- 内置世界预设（`backend/app/services/starter_worlds.py`）：**奇幻村庄**、**雾镇档案**、
  **轨道站事故**、**荒野营地**；前端另有三份可直接落地的模板（`frontend/src/data/worldTemplates.js`）。

### AI 生成（需要已配置模型）

`backend/app/services/ai_generator.py` 提供三处一键生成：

| 位置 | 输入 | 输出 | 接口 |
|------|------|------|------|
| 知识库 | 提示词 | 结构化知识草稿 → 预览 → 全部写入 | `POST /api/v1/scenarios/{id}/generate/knowledge` |
| 故事线 | 提示词 | 完整 Storyline 草稿（含校验提示） | `POST /api/v1/scenarios/{id}/generate/storyline` |
| 世界工坊 | 提示词 | 校验引用后直接保存为新世界 | `POST /api/v1/worlds/generate` |

生成接口统一走已配置模型或指定预设；未配置模型返回 **503**，非法 JSON/引用返回 **502**。

### 作者草稿与联网取材

`POST /api/scenarios/draft` 一次只生成**一个字段**（`character` / `world` / `location` /
`faction` / `item` / `lore`），`POST /api/scenarios/draft/expand` 接着作者已有的一段往下写。
两个接口在角色库与情景工坊的每个文本字段旁边都挂了一组按钮（`QuickDraft`：「快速生成」/「联网生成」/
「继续扩展」），**没有配置模型时返回模板文本**，按钮永远不会是死的。

点「联网」开关后，`backend/app/services/web_search.py` 会按 DuckDuckGo HTML →
DuckDuckGo lite → Bing 的顺序取检索结果（`SEARCH_BASE_URL` 可指向自建的 SearxNG）。
`POST /api/scenarios/draft/search` 单独列出这些命中（按钮旁边的「预览」），让作者先看清"模型将要读什么"
再决定花不花这次生成。

> **检索结果是不可信数据。** 它被包在 `<<<UNTRUSTED_SEARCH_RESULTS>>>` 围栏里进提示词，
> 页面里的文字无法提前闭合围栏；提示词也明确要求模型把它当引文而不是指令。

### 一句话生成剧本：世界 + 知识 + 主线

`POST /api/v1/scenarios/author`（`backend/app/services/scenario_author.py`）用**一次模型调用**返回三件套，
但**逐件独立校验**：世界过 `validate_references`，知识逐条过 `KnowledgeEntryCreate`，
剧情线过 `validate_storyline`。哪件不合法就进 `warnings` 而不是整单失败——一份能玩的世界
不该因为其中一条知识少写了标题就整个作废；三件全废才返回 502。

`save=false` 是**试运行**：只校验、不落盘，用于在作者确认前预览模型会给出什么。
落盘时世界走 `world_service.save_world`，剧情线必须经 `get_play_config → 改 .storyline → set_play_config`
写回（直接覆盖会重置 mode 与模型预设）。**它从不写权威游戏状态**——只创建配置。

### 下一步建议：不依赖模型的三条出口

`backend/app/services/suggestions.py` 从服务端已有的事实算出"现在能做哪三件事"：
当前地点的出口、在场的角色与物品、背包里一件可以试试的东西。
`GET /api/v1/campaigns/{id}/suggestions` 返回它，规则链路每次提交后也会把同一组建议塞进
`ActionResult`。结果是**建议性的**：不提交任何东西、不虚构不存在的名字。

这一条存在的理由很具体：以前建议只来自叙事 JSON 字段和一次额外模型调用，
模型一挂就双双消失，玩家只剩一个空输入框；规则链路则从来没有要过建议。现在最差情况也有三条能点的路。

### 作品穿越：借一个世界的骨架

`backend/app/services/work_catalogue.py` 内置六部作品档案（西游记、福尔摩斯、三体、流浪地球、
哈利·波特、指环王），每部带梗概、语气、地点（含地点注释）、开场提示与 2–4 个可选角色。
`POST /api/v1/works/enter` 会按档案生成一个**链式连通**的世界骨架（相邻地点双向出口），
把角色作为起点 NPC 写入，并记到战役的角色选择上。

这是**策展优先**设计而不是联网搜索：档案是确定性的、可测试的，模型只在内置作品未命中时才补写
（`allow_model=false` 时直接 404，不花模型调用）。选角色是「换一个视角看这个世界」，
不是绕过规则——进入后的每一步仍然走同一个规则链路。

### 对话式补全：一句话长出一个地方

`POST /api/v1/worlds/{w}/authoring/propose` 把一句「在广场东边加一条通往河边的小路，并写一条关于这条河的设定」
变成**可审核的编辑提案**：`reply` 一句回复、`ops` 通过校验的操作、`changes` 各地类数量前后对比、
`warnings` 被拒绝的部分及原因。**这一步不写任何东西。**

确认后 `POST .../authoring/commit` 才落盘。服务端不信任客户端回传的提案：世界部分重新按
`WorldDefinition` 解析并再跑一次引用校验，知识逐条重验，不合法返回 400。写入只涉及世界文档与知识库——
**游戏状态、任务进度、存档一律不动**。

每条操作都在世界文档的**副本**上走 `world_editor` 的校验读改写，所以一个会让引用悬空的提案当场被拒，
原因直接来自校验代码而不是模型的自述：

```text
add_door 未通过校验：door 'ghost_door' requires unknown key item 'nope'
```

提示词里附上当前世界已有的地点/物品/门/实体/事件/任务 id 与最近 12 轮对话，让模型复用已有命名而不是另起一套。
当前只支持新增，不支持改字段与删除；**故事线不在这条通道里**，见 `docs/delivery_review.md`。

### 从叙事里抽记忆：抽取是确定性的，模型只改写

`POST /api/v1/scenarios/{s}/knowledge/extract` 读已提交的叙事回合（`narrator.response` 的 `payload.text`），
把句子分类成人物/事件，并识别 `X 是 Y`、`X 持有 Y` 这类整句事实。**先确定性抽取，模型只做改写**（`refine=true`），
模型挂了就退回原文加一条 warning，不会让整批丢失。

它是**提案通道**：`apply=false` 时完全只读且幂等，`apply=true` 只写 `scenario_knowledge`。
没有任何规则读知识库，所以一条抽错的记忆只会让叙事偏一点，不会污染存档。

### 交互预演：先看这一回合会怎样

`POST /api/v1/worlds/{w}/plan` 列出当前房间每个候选动作与真实判定结果（放行/拒绝及原因）；
`POST .../preview-actions` 预演一条动作链，逐步给出效果、触发事件与状态字段前后值；
`POST .../explain` 回答「为什么不行」，并给出可行路径（例如「先到森林边缘取钥匙，距离 1 步」）。
战役侧还有 `POST /api/v1/campaigns/{id}/actions/preview`，玩法相同。

判定全部来自真实的 `RulesEngine.evaluate` 与 `event_engine`，状态是深拷贝，**没有任何写回**。
预演若自己实现一套规则，迟早会和真实链路漂移；若真写进去，就违反「只有行动服务能改权威状态」。
所以它只读。

### 世界演化：断言变成提案，对话长成世界

玩家或模型说出的「事实」不再直接进正史。`backend/app/services/world_evolution.py` 把一句话变成
一条**提案**（`kind` + `confidence` + `payload` + `evidence`），先做冲突检查（世界里已有同名地点就不再建一个），
再把通过检查的正史类提案丢给 `authoring_session.stage_ops` 在**世界副本**上校验读改写；校验不过的提案
保持 `pending` 或转 `rejected`，**不会**被记进「已采纳」。传闻、猜测这类个人类断言永远不进正史。

```text
POST /api/v1/worlds/{w}/evolution/propose            只草案，commit 默认 false
GET  /api/v1/worlds/{w}/evolution/proposals          审计轨迹：每条断言与它的下场
POST /api/v1/worlds/{w}/evolution/proposals/{p}/approve
POST /api/v1/worlds/{w}/evolution/proposals/{p}/reject?reason=...
```

「已采纳」只在 `applied` 里真的出现这条提案时才写，所以报告不会把一次失败说成成功；
采纳时会重新校验整份世界文档，引用悬空就 400。世界工坊的「世界演化」页签就是这套接口的界面：
提交一句话、看提案、逐条采纳或驳回，驳回原因就留在记录里。

### 角色路由：模型按角色选，不写死在代码里

`backend/app/engines/agent_router.py` 让引擎按**角色**（`narrative` / `system` / `memory`）找模型：

```env
AGENTS_SYSTEM_MODEL=...        # 回答 system 角色的模型
AGENTS_SYSTEM_PROVIDER=...     # 可选供应商覆盖
AGENTS_SYSTEM_ENABLED=0        # 整个角色关掉
AGENTS_SYSTEM_TIMEOUT=45       # 单次调用秒数
AGENTS_NARRATIVE_* / AGENTS_MEMORY_*   # 同样的开关
```

每读一次环境、每个角色独立，未配置的角色退回主模型——所以一个从没听过「角色」的部署行为不变。
`AgentRuntime.ask()` 永不抛异常：调用失败返回带 `error` 的空答案并降级到下一个角色，
一回合不会因为模型挂了而失败。每次调用写进 `game_state.db` 的 `agent_calls` 表，
调试台的「模型调用」页签读它，`GET /api/v1/agents` 只看配置与可用性，**从不回显密钥**。

### 时间只在玩家行动时走

`backend/app/engines/game_clock.py` 的 `Clock` 由 `ActionService` 在应用效果时推进：
每个行动带 `duration_minutes`，回合结果多出 `minutes_spent` 与 `clock`，`GameState` 多出 `day` / `minute`。
暂停的战役不花时间，恢复时也不补算——因为没有墙钟时间可供追赶，离线世界模拟在结构上就不存在。

### 任务：定义可编辑，进度由事实推进

任务有目标、前置任务、接受条件、失败条件与奖励。`GET|POST|PATCH /api/v1/worlds/{w}/quests`
到目标与奖励级（`.../objectives/{id}`、`.../rewards/{id}`）都可编辑，每次编辑都跑完整世界校验，
**不通过的编辑不会落盘**。奖励没有 id，用效果种类当路径 id（同一任务同一效果种类只有一条）。

推进目标的是 `quest_progress` **效果**，不是任何一段文本。调试时的手动推进
（`POST /campaigns/{c}/quests/{q}/objectives/{o}/progress`）走的仍是 `ActionService` 的提交路径：
幂等、版本 CAS、单事务、领域事件齐全，只是在判定环节跳过规则。它受 `ENGINE_DEBUG_TOOLS` 保护，
关闭时返回 403。

### 叙事诊断：模型到底看到了什么

每回合的 `narrative.diagnostics` 记录**只读旁路**信息，不进入提示词、不影响提交：

- 路由：`provider` / `model` / `temperature` / `max_tokens` / `timeout` / `language` / `content_policy`
- 上下文：`blocks[]`（id、层、标题、160 字符摘要）与 `fact_counts`（按来源计数）
- 结局：`skipped`（未调用模型）或实际生效的策略；超时、异常、空文本、被拒**都带着这份记录**

失败时最需要诊断，所以 `narrative.failed` 事件也带 `diagnostics`。前端调试台「叙事与上下文」页签
把它读成卡片，「行动记录」页签列出该战役每个 request_id 的 `applied/rejected/duplicate`。

### 内容策略与本地回退

`content_policy` 有 `standard` / `nsfw` 两档（`POST /api/settings` 可改）。有两种路径：

- **主动**：直接选 `nsfw`，整条请求走本地模型（`nsfw_provider` / `nsfw_model`）。
- **被动**：上游返回拒绝时，路由器不把拒绝文本当正常输出转发，而是带 `⚠⚠REFUSAL⚠⚠` 哨兵返回；
  叙事层发现哨兵后整条重发到本地模型一次。本地也拒绝则本回合没有叙事，**状态照常提交**。

流式路径同样处理：`stream()` 累积后判定，命中就不再逐帧转发——否则玩家会看到半句拒绝文本。

### 模型路由

`backend/app/engines/llm_router.py` 支持多个供应商，可在设置面板或模型预设中切换：

- `deepseek` / `anthropic` / `openai`（云端，需对应 API key）
- `local`（本地 OpenAI 兼容服务，即 NInfer；无需云端密钥即可叙事，见 `docs/local_model_ninfer.md`）
- 任意 OpenAI 兼容的第三方服务（`.env` 的 `CUSTOM_LLM_*`）
- 可选经 CLIProxyAPI 走订阅代理（`proxy/README.md`）

预设为 SillyTavern 风格子集：`top_p` / `top_k` / `min_p`、重复/存在/频率惩罚、`seed`、
停止词、上下文窗口、预设系统提示；叙事/intent/dialogue/summary 四个角色可按场景覆盖。

**供应商密钥也可以直接填在设置面板里**：存进本机 `scenarios.db` 的 `provider_credentials` 表
（文件权限在打开时收紧到仅当前用户），接口只回显末四位，「测试连接」会真发一句 completion
并把上游报错原样显示出来，「清除」后回落到 `.env` 的同名变量。

### 叙事语言

`WorldDefinition.language`（`"zh"` / `"en"`，留空视为英文）决定**描述语言**，规则引擎从不读它。
起步世界自动写入，AI 生成的世界按提示词语言写入。三处都遵守它：

1. 确定性叙事器（`TemplateNarrator`）的句子与连接符；
2. 效果描述（拾取/失去/移动/开门/关门/标记）；
3. 交给模型时的 `language` 选项（场景预设已指定则预设优先）。

没有这一层时，中文世界会得到 `You arrive at 林间小路.` 这种句子——地点名是中文，句子是英文。

### 叙事不会带控制标记

旧聊天链路曾靠解析模型输出里的 `[ITEM_ADD:name|category|source]` 改库存。那条路现在默认不写状态
（`LEGACY_APPLY_ITEM_TAGS=0`），但标记能出现在引擎散文里本身就是风险：任何一处放宽的正则都会
把它变成副作用。`strip_prose_tags()` 因此在两条叙事路径上都把这类标记剥掉。

### 被拒绝时给一句能接着往下走的话

文本链路以前解析失败只回一个空的 `skipped` 叙事，玩家看到的是「（行动被规则拒绝）」。
`backend/app/services/rejection.py` 把同样的约束翻成现场可用的事实：此刻真正能去的出口、
在场的人物、能拿的物品与能开的门，最多三条。规则引擎的约束一条不改，只是讲清楚。

### 编辑器与调试台

- **世界工坊**（十三个页签）见上文；事件可校验、可模拟（模拟不改真实状态）；调试页有状态差异与快照回滚。
- **调试台**（四个页签）：调用追踪（旧链路的 trace）、叙事与上下文（`narrative.diagnostics`）、
  行动记录（request 列表与单条详情）、模型调用（哪个角色由哪个模型回答 + 每次调用记录）。
- 所有能改状态的调试能力都受 `ENGINE_DEBUG_TOOLS` 限制，生产环境默认关闭并返回 403。

---

## 快速开始

### 环境要求

- **Python 3.10+**、**Node.js 18+**
- 可选：**Docker**（只为 Neo4j 知识图谱镜像准备，没有也能完整游玩）
- 可选：一个模型 API key，或本机跑一个 OpenAI 兼容推理服务

最短路径（不需要任何配置）：装好依赖 → `python launch.py` → 首页选一个情景 → 绑定起步世界 → 直接玩。
叙事由 `TemplateNarrator` 按已结算效果生成，引擎的判定、事件、任务、存档全部照常工作。

### 安装

```bash
git clone https://github.com/LosSca/self-engine.git
cd self-engine

install.bat      # Windows
./install.sh     # macOS / Linux
```

脚本会建好后端虚拟环境（`backend/.venv`）、安装 `backend/requirements.txt` 与
`backend/requirements-dev.txt`、装前端依赖，没有 `.env` 时从 `.env.example` 复制一份。

```text
backend/requirements.txt        运行时依赖，区间约束（如 fastapi>=0.115,<0.163）
backend/requirements-dev.txt    测试依赖：pytest / pytest-asyncio / pytest-cov
graphiti-core                   可选：只在要 Neo4j 图谱镜像时另外装
```

> **Neo4j 可选，且脚本不再替你起容器**：安装脚本只装 Python 与前端依赖。
> 需要实体关系图谱时自己起一个 Neo4j 5 并把 `NEO4J_URI` 填进 `.env`；不想用就把
> `ENGINE_GRAPH_MIRROR` 设为 0——图谱只是镜像，不会因为不可用而回滚回合。

起图谱可以直接用自带的检查工具，它会告诉你当前状态并给出下一步命令：

```bash
python launch.py --neo4j               # 只看状态：连得上 / 认证失败 / 没在跑，各自给出修复命令
python launch.py --neo4j start         # 有 Docker 时起一个 neo4j:5 容器（首次会生成随机密码写回 .env）
python launch.py --neo4j password xxx  # 改密码后同步写回 .env
python launch.py --neo4j stop          # 停容器
```

没有 Docker 也可以：用 Neo4j Desktop 或官方压缩包起一个 5.x，把 `bolt://localhost:7687`
与密码填进 `.env` 即可；不改 `.env` 时把 `ENGINE_GRAPH_MIRROR` 设为 0 关掉镜像。

### 配置

编辑仓库根的 `.env`（字段与 `.env.example` 一一对应，下面只列常改的几组）：

```env
# 至少配一个：云端密钥，或下面的本地服务
DEEPSEEK_API_KEY=
ANTHROPIC_API_KEY=
OPENAI_API_KEY=

# 本地 OpenAI 兼容推理服务（NInfer 等）
LOCAL_LLM_BASE_URL=http://127.0.0.1:8094/v1
LOCAL_LLM_MODEL=qwen3.8-27b
LOCAL_LLM_CONTEXT_WINDOW=49152

# 叙事：auto = 配了模型就用模型，否则退回模板文本；template = 始终确定性文本
ENGINE_NARRATOR=auto
ENGINE_LLM_PROVIDER=

# 图谱镜像；不接 Neo4j 时设为 0
ENGINE_GRAPH_MIRROR=1
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=

# 内容策略：standard = 正常；nsfw = 整条请求走本地模型
# 也可在设置面板切换，或 POST /api/settings
# CONTENT_POLICY=standard
# NSFW_MODEL=
# 上游拒绝时自动改走本地模型重试一次
# LOCAL_FALLBACK_ON_REFUSAL=1

# 角色路由：让不同角色由不同模型回答（不写就全部退回主模型）
# AGENTS_NARRATIVE_MODEL=
# AGENTS_SYSTEM_MODEL=
# AGENTS_MEMORY_MODEL=
# AGENTS_SYSTEM_ENABLED=0
# AGENTS_SYSTEM_TIMEOUT=45

# 调试/作者工具：手动推进任务目标、回滚快照、修复状态都受它控制
# ENGINE_DEBUG_TOOLS=0

# 服务暴露面：不设 API_TOKEN 时，任何能连上端口的人都能驱动引擎
# API_TOKEN=
# 内置 CORS 白名单只有 localhost/127.0.0.1 的 5173 与 4173，其它来源要写进来
# LUNAR_CORS_ORIGINS=
```

**没有任何密钥也能玩**：`ENGINE_NARRATOR=template` 时由 `TemplateNarrator`
根据已结算的状态与效果生成确定性文本，不联网、零成本。

**大部分设置在界面里就能改**：设置面板改完的值存进服务端的 `user_prefs` 表
（`GET/POST /api/prefs`），换浏览器或换设备仍然生效。**API 访问令牌不在其中**——
它只留在当前标签页的 `sessionStorage`，关掉标签页即失效。

### 运行

```bash
start.bat           # Windows
./scripts/start.sh  # macOS / Linux
```

两端脚本都只是调用 `launch.py` 的薄封装，所以 `python launch.py` 等价。

只需这一条命令：`launch.py` 探测解释器 → 起后端（`127.0.0.1:8000`）→
起前端 → 等健康检查 → 打开浏览器。API 文档在 <http://127.0.0.1:8000/docs>。
关掉启动器窗口即停止两端，不需要自己开两个终端跑 `uvicorn` 和 `npm run dev`。

两种前端模式：

| 命令 | 前端 | 端口 | 适合 |
|------|------|------|------|
| `start.bat` / `python launch.py` | vite dev（热更新） | 5173（被占用则顺延到 5174、5175…） | 改前端代码时 |
| `start.bat --prod` | `npm run build` + 自带静态服务器托管 `frontend/dist` | 4173 | 演示、桌面快捷方式 |

> **端口被占用时不再打开别人的页面**：启动器会先确认 5173 上是不是本项目的页面，
> 不是就自动顺延，并在输出里写明实际端口与原因。

**桌面快捷方式默认走 `--prod`**：某些受限环境里 vite dev 无法在浏览器端预打包依赖，
裸导入会 404 导致白屏；`--prod` 交付的是一份已经打包好的 js，在任何环境下都能渲染。
想回到热更新就照上表左列直接用 `launch.py`（不加 `--prod`）。细节见 [`docs/launch.md`](docs/launch.md)。

常用参数（传给 `launch.py`，`--help` 有完整列表）：

| 参数 | 作用 |
|------|------|
| `--debug` | 后端 `--reload` + debug 日志 + `LUNAR_DUMP_LLM_CALLS=1` 转储模型调用 |
| `--backend-only` / `--frontend-only` | 只起一端 |
| `--no-browser` | 不自动开浏览器 |
| `--no-window` | 两个服务放在前台、日志写 `.logs/`、Ctrl+C 一并停（自检/CI 用） |
| `--host 0.0.0.0` / `--mobile` | 局域网/手机访问（会打印可用的局域网地址） |
| `--prod` / `--rebuild` | 托管构建产物 / 强制重新构建 |
| `--neo4j [status\|start\|stop\|password]` | 管理可选的图谱库，不启动前后端 |

### 手机 / 局域网访问与安全边界

`--mobile` 会让后端监听 `0.0.0.0`。此时**请同时设置 `API_TOKEN`**：
令牌关闭时，同一网络里任何设备打开那个地址都能读写存档、切换模型、消耗你的 API 额度。
CORS 挡不住这种情况——它只约束浏览器，而任何脚本都能直接发请求。
启动器在 `--host 0.0.0.0` 且未设 `API_TOKEN` 时会打印警告。

### 桌面端（可选）

`desktop/` 是一个 Tauri 2 壳：原生窗口里跑的就是上面这套前端，后端仍由 `launch.py` 拉起。
需要 Rust stable + Node 18+，构建与限制见 [`desktop/README.md`](desktop/README.md)。

```bash
cd frontend && npm install && npm run build
cd ../desktop && npm install
npm run build        # 产物在 desktop/src-tauri/target/release/bundle/
```

> Windows 上**必须用 MSVC 工具链**（`rustup default stable-x86_64-pc-windows-msvc` + Visual Studio
> "使用 C++ 的桌面开发"）。MinGW 能编译通过，但 WebView2 创建窗口时会返回
> `HRESULT 0x8000FFFF`——这是 Tauri 的限制，不是本壳的 bug。

不想装 Rust 的话，用 `start.bat --prod` + Chrome「创建快捷方式（以窗口形式打开）」也是同一效果。

---

## 界面与工作流

| 路由 | 界面 | 用途 |
|------|------|------|
| `/` | 首页 `Home` + `Sidebar` | 情景卡片、战役列表、进入各个工坊、新手引导 |
| `/create` | `ScenarioBuilder` | 新建情景（世界类型、语气、设定问答、开场方式、草稿生成） |
| `/play` | `PlayGate` → `GameCanvas` + `ToolDock` | 游玩：剧情流、行动输入、下一步建议、迷你地图、工具面板 |
| `/characters` | `CharacterLibrary`（以整页浮层打开） | 角色卡库、外观、草稿生成 |

**布局**：左边是固定侧边栏（`Sidebar`），右边是工具坞（`ToolDock`），中间是主内容。
工具坞取代了早期的浮层模态栈——同一时间只开一个面板，关掉就回到剧情流：

| 分组 | 面板 |
|------|------|
| 游玩 | 角色档案、背包、世界地图、任务日志 |
| 叙事 | 剧情进度、剧情生成、时间流逝、NPC 心智、记忆结晶 |
| 系统 | 世界工坊、LLM 调试台、设置 |

其它值得知道的界面行为：

- **迷你地图**（`MiniMap`）常驻在剧情流右上角，点一下展开完整世界地图。
- **新手引导**（`OnboardingGuide`）在侧边栏里逐条讲清系统与配置步骤，可折叠、可跳过。
- **行动预演**：提交前可先看判定结果（`POST /campaigns/{id}/actions/preview`），预演不写状态。
- **自然语言**：文字行动由 `text_action_service` 解析成同一个 `ActionRequest`，解析失败回到按钮，
  并给出此刻真正可用的出口与目标。
- **下一步建议**（`SuggestionList`）：输入框上方三个可点的选项，可以直接发送或先改再发。
- **调试台四个页签**：调用追踪、叙事与上下文、行动记录、模型调用。
- **内容策略**在设置面板切换；切到「NSFW（本地）」后叙事整条走本地模型。

典型流程：新建情景 → 设定问答（`SetupWizard`，在 `App.jsx:494` 挂载，答完写进
`POST /api/game/{id}/setup-answers`）→ 绑定世界 → 行动（文字或按钮）→ 看结算与叙事 →
调试台核对叙事上下文、行动记录与状态差异。

前端状态用 Zustand（`frontend/src/store.js`），接口在 `frontend/src/api.js`；页面按路由懒加载，
路由表共四条（`frontend/src/App.jsx:547-568`）。

---

## 目录结构

```
self-engine/
├── launch.py                    # 通用启动器（前后端编排、端口探测、健康检查、浏览器）
├── start.bat                    # Windows 入口（纯 ASCII，调用 launch.py）
├── scripts/serve-dist.mjs       # --prod 的前端静态托管 + /api 反代
├── scripts/start.sh             # macOS / Linux 启动入口（调用 launch.py）
├── scripts/start-dev.ps1        # PowerShell 开发启动脚本
├── scripts/fix-fragment-calls.mjs # 构建后把误拆的 fragment 调用拼回去
├── scripts/neo4j_tool.py        # --neo4j 的实现：状态检查 / 起停容器 / 改密码
├── install.bat / install.sh     # 一键安装（Python venv + 运行与测试依赖 + 前端依赖）
├── desktop/                     # Tauri 桌面壳（不重写界面，只包一层窗口 + 拉起后端）
├── proxy/                       # 可选的 CLIProxyAPI 订阅代理（外部二进制的壳，见 proxy/README.md）
├── .env.example                 # 环境变量样例，安装脚本会复制为 .env
├── AGENTS.md / README.md / CHANGELOG.md
├── docs/
│   ├── review-2026-11-15.md     # 全量审查：结论、P0–P3 清单、逐条修复状态与回归测试名
│   ├── review-overview.md       # 运行时结构、13 项验收矩阵、风险登记
│   ├── delivery_review.md       # 七条需求与 14 条验收的交付审查
│   ├── launch.md                # 启动器的端口探测、--prod 与日志行为
│   ├── stage6_effect_registry.md / stage6_api_contracts.md
│   ├── stage8_content_policy.md / stage9_world_crossover.md
│   ├── stage10_scenario_author.md / stage11_quest_editor.md
│   ├── stage13_narrative_debug_console.md / stage14_world_language.md
│   ├── phase0_audit.md … phase7_completeness_review.md   # 各阶段设计与审计
│   ├── local_model_ninfer.md    # 本地模型接入
│   └── postgres-migration.md    # SQLite → PostgreSQL 一次性导出
├── backend/
│   ├── requirements.txt / requirements-dev.txt / pytest.ini
│   ├── app/
│   │   ├── main.py              # FastAPI 入口、CORS、令牌门、/api/settings 与 /api/prefs
│   │   ├── config.py            # pydantic-settings，读取 .env
│   │   ├── api/                 # routes_engine / routes_knowledge / routes_works
│   │   │                        # routes_game / routes_scenarios / routes_characters
│   │   ├── db/                  # scenario_store / game_state_store / event_store /
│   │   │                        # trace_store / prefs_store / credential_store
│   │   │                        # （prefs 与 credentials 复用 scenarios.db 连接）
│   │   ├── engines/             # 规则与引擎层（24 个模块）
│   │   ├── models/              # game.py / knowledge.py / work.py
│   │   ├── services/            # 编排与服务层（action_service 是唯一写入口）
│   │   └── utils/
│   ├── scripts/sqlite_to_postgres.py
│   └── tests/                   # pytest，asyncio_mode = auto
├── frontend/
│   ├── package.json             # version 0.5.0
│   ├── scripts/check-hook-order.mjs / check-agent-wiring.mjs
│   └── src/
│       ├── App.jsx / api.js / store.js
│       ├── components/          # 画布、侧边栏与工具坞、各工坊、面板（29 个）
│       ├── data/worldTemplates.js
│       └── lib/                 # interpolate / theme / scenarioTypes
```

**engines/**（24 个模块）：`action_registry`（行动目录）、`rules_engine`（纯函数判定）、
`event_engine`（事件与效果注册表）、`quest_engine`、`story_engine`、`combat_engine`、
`journal_engine`、`narrator_engine`、`memory_engine`、`npc_mind_engine`、`plot_generator`、
`world_reactor`、`inventory_engine`、`item_proposals`、`opening_generator`、`llm_router`、
`graph_engine`、`graphiti_engine`、`auditor_engine`。

**agent 层**（阶段 1–5）：`agent_router`（按角色路由模型，`AGENTS_<ROLE>_*` 决定用哪个，代码里没有一个模型名）、
`system_agent`（先确定性分类，只有长断言才问模型）、`npc_dossier`（NPC 是每回合按数据拼出来的档案）、
`player_context`（带 `fact` / `canon` / `belief` / `guess` / `history` 标签的分层上下文）、
`game_clock`（只有玩家行动才推进的时间）。

**services/**：`action_service`（唯一写入口）、`text_action_service`（自然语言 → `ActionRequest`）、
`rejection`（被拒绝时说人话）、`suggestions`（三步建议）、`knowledge_service`、`knowledge_extractor`、
`narrative_service`、`narrator_factory`、`summary_service`、`intent_advisor`、`interaction_planner`、
`world_service`、`world_editor`、`world_evolution`（断言 → 校验过的提案 → 可审核落盘）、
`authoring_session`（一句话变成编辑操作）、`scenario_author`、`scenario_service`、
`scenario_interpolation`、`starter_worlds`、`state_diagnostics`、`ai_generator`、`card_images`、
`draft_generator`（单字段草稿）、`web_search`（联网取材）、`work_catalogue` / `work_generator`、
`game_session`（旧聊天链路的会话）。

---

## API 概览

全部接口挂在 `/api` 下，共 **123 条路径 / 159 个操作**；设置 `API_TOKEN` 后除健康检查外都要求
`Authorization: Bearer <token>` 或 `X-API-Token`。完整清单与请求/响应结构见 <http://127.0.0.1:8000/docs>，
`/api/v1` 的每条路由都有类型化响应模型，并由 `backend/tests/api/test_routes_contracts.py` 守住。

| 前缀 | 路径数 | 用途 |
|------|--------|------|
| `/api/v1` | 73 | 规则链路、知识、预设、AI 生成、作品库、世界编辑 |
| `/api/game` | 24 | 旧聊天链路（流式 SSE） |
| `/api/scenarios` | 12 | 情景、战役、故事卡、作者草稿、联网取材 |
| `/api/characters` | 6 | 角色卡与外观 |
| `/api/providers` | 3 | 供应商密钥的写入 / 删除 / 连通性测试 |
| `/api/health` | 2 | 存活探针与 Neo4j 连通性 |
| `/api/settings`、`/api/prefs`、`/api/campaigns` | 各 1 | 全局设置、跨设备偏好、战役角色 |

### 规则链路 `/api/v1`（确定性游玩）

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/action-types` | 内置行动目录（16 个行动，含中文动词） |
| POST | `/campaigns/{id}/world` | 为战役绑定世界 |
| POST | `/campaigns/{id}/actions` | 提交结构化行动（唯一写入口） |
| POST | `/campaigns/{id}/actions/preview` | 干运行：只看判定，不写状态 |
| POST | `/campaigns/{id}/text-actions` | 自然语言行动，解析后走同一管线 |
| POST | `/campaigns/{id}/combat` | 战斗回合 |
| GET | `/campaigns/{id}/state` | 权威状态（含 `version`） |
| GET | `/campaigns/{id}/suggestions` | 此刻可以做的三件事（不调用模型） |
| GET | `/campaigns/{id}/requests` | 请求记录（含 `applied/rejected/duplicate`） |
| GET | `/actions/{request_id}` | 按幂等键查单条请求与结果 |
| GET | `/campaigns/{id}/agent-calls` | 该战役的模型调用记录 |
| GET | `/campaigns/{id}/events` | 领域事件（可回放，支持 `after_version`） |
| GET | `/campaigns/{id}/event-executions` | 事件执行记录 |
| GET | `/campaigns/{id}/quest-progress` | 任务进度（读权威状态拼装） |
| GET | `/campaigns/{id}/story` | 剧情线进度 |
| GET | `/campaigns/{id}/lane` | 时间线/调试视图 |
| GET | `/campaigns/{id}/diagnostics` | 状态一致性诊断 |
| GET | `/campaigns/{id}/snapshots` | 快照列表；`/{version}` 取单份 |
| DELETE | `/campaigns/{id}/snapshots/prune` | 裁剪旧快照（受 `ENGINE_DEBUG_TOOLS` 保护） |
| POST | `/campaigns/{id}/rollback` | 回滚到某个版本（版本 CAS，冲突 409） |
| POST | `/campaigns/{id}/repair` | 修复不一致状态（版本 CAS，冲突 409） |
| GET | `/campaigns/{id}/state-diff` | 状态差异（版本前后对比） |
| GET | `/campaigns/{id}/export` / POST `/import` | 存档导出与导入 |

### 作品库 `/api/v1`

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/works` | 内置作品档案列表 |
| POST | `/works/resolve` | 解析一个作品名（内置优先，可选模型补写） |
| POST | `/works/enter` | 按作品生成世界骨架并进入 |
| GET | `/works/{title}` | 单部作品档案 |

### 知识、预设与生成 `/api/v1`

| 方法 | 端点 | 说明 |
|------|------|------|
| GET / POST | `/scenarios/{id}/knowledge` | 列出 / 追加知识条目 |
| PATCH / DELETE | `/knowledge/{entry_id}` | 编辑 / 删除单条知识 |
| GET | `/knowledge/layers` | 知识分层与可用深度 |
| POST | `/scenarios/{id}/knowledge/extract` | 从已提交叙事里抽记忆（提案通道） |
| POST | `/scenarios/{id}/knowledge/validate` | 校验一条知识条目 |
| GET | `/scenarios/{id}/context/preview` | 预览这一回合会注入的知识 |
| POST | `/scenarios/{id}/generate/knowledge` / `/generate/storyline` | AI 生成知识 / 剧情线 |
| POST | `/scenarios/author` | 一句话生成世界 + 知识 + 主线 |
| GET / POST | `/model-presets`（含 `/{id}` PATCH、DELETE） | 模型预设 |
| GET | `/local-model` | 本地模型是否可用 |
| GET | `/agents` | 角色路由的配置与可用性（不回显密钥） |
| GET / POST | `/worlds`、`/worlds/generate`、`/worlds/validate` | 世界读写与校验 |
| PUT / DELETE | `/worlds/{id}/locations\|items\|doors\|entities\|events\|quests\|custom-actions` | 世界编辑（每次全量校验） |
| POST | `/worlds/{id}/plan`、`/preview-actions`、`/explain` | 房间预演与「为什么不行」 |
| POST | `/worlds/{id}/authoring/propose` / `commit` | 对话式补全的提案与落盘 |
| POST | `/worlds/{id}/evolution/propose`（含 `approve` / `reject`） | 世界演化 |

### 旧聊天链路 `/api/game`（流式，正在日落）

24 条路径：`action`（SSE 流）、`characters`、`clock`、`crystallize`、`generate`、
`graph-search`、`history`、`inject-npc-seed`、`inventory`、`journal`、`memory-crystals`、
`npc-minds`（含单条 PUT/DELETE）、`pause` / `resume`、`regenerate-opening`、`rewind`、
`scenario-view`、`settings`、`setup-answers` / `setup-state`、`timeskip`、`traces`、`world-graph`。

这些 URL 作为兼容层保留，但**背后的执行机制正在退役**：

- 写入：`POST /api/game/{id}/action` 只有一条自动路径——规则链路（`TextActionService` →
  `ActionService`）。没绑定世界的战役会拿到 409，响应里写明可以重新绑定规则世界，或传
  `compat_mode: true` 显式进入旧会话；**不会再静默分流到旧 LLM 引擎**。
- 读取：`history`、`journal`、`inventory`、`npc-minds`、`characters`、`memory-crystals`、
  `graph-search`、`rewind` 已改成只读投影（`app/services/compat_views.py` 与共享引擎），
  不再为了回答一个面板而重建 `GameSession`。
- 旧存档的 `EventStore` 记录只作为**可读记录**保留，不会被当作已验证事实导入权威状态。
- 仍待退役：`GameSession.process_action()` 本身（当前只经 `compat_mode` 可达）、
  旧库存写入分支、`inject-npc-seed` / `timeskip` 对活动会话的依赖。

这条链路默认不写权威状态（`LEGACY_APPLY_ITEM_TAGS=0`），用于未绑定世界的冒险。

### 情景、作者草稿与角色

| 方法 | 端点 | 说明 |
|------|------|------|
| GET / POST | `/api/scenarios/` | 情景列表与新建 |
| GET / DELETE | `/api/scenarios/{id}` | 单个情景 |
| POST | `/api/scenarios/draft` | 生成**一个字段**（character / world / location / faction / item / lore） |
| POST | `/api/scenarios/draft/expand` | 接着已有文字往下写 |
| POST | `/api/scenarios/draft/search` | 联网取材的命中预览 |
| POST | `/api/scenarios/preview-opening` | 预演开场白，不落库 |
| POST | `/api/scenarios/import`、`/{id}/export` | 情景导入导出 |
| POST / GET | `/api/scenarios/{id}/campaigns` | 战役的创建与列表 |
| GET / POST | `/api/scenarios/{id}/characters`、`/story-cards` | 情景内的角色与故事卡 |
| GET / POST | `/api/characters`（含 `/{id}`、`/looks/{id}`、`/upload`、`/library/import`） | 角色库与外观 |

### 全局设置、偏好与供应商

| 方法 | 端点 | 说明 |
|------|------|------|
| GET / POST | `/api/settings` | 供应商、模型、采样参数、`content_policy`、本地回退开关 |
| GET / POST | `/api/prefs` | 跨设备的界面与叙事偏好（白名单键，**不含**任何密钥） |
| GET | `/api/providers` | 各供应商是否已配置（只回显末四位） |
| POST / DELETE | `/api/providers/{provider}` | 写入 / 清除密钥 |
| POST | `/api/providers/{provider}/test` | 真发一句 completion 验证连通性 |
| GET | `/api/health`、`/api/health/neo4j` | 存活探针与图谱连通性 |
| GET / PUT | `/api/campaigns/{id}/character` | 战役里的玩家角色 |

### 一次行动请求的形态

```json
{"campaign_id": "camp-1", "request_id": "6f1c…", "action": "open",
 "target_id": "warehouse_door", "params": {}}
```

返回 `ActionResult`：`status`（`applied` / `rejected` / `duplicate` / `conflict`）、`ok`、`code`、
`message`、`state_version`、`effects_applied`、`events_fired`、`quests`、`story_nodes_entered`、
`narrative`（含 `diagnostics`）、`suggestions` 与提交后的 `state`；阶段 5 起还带
`minutes_spent` 与 `clock`（老客户端可忽略）。

`request_id` 是幂等键：同一个 id 重放返回 `duplicate` 与**同一份**结果，不会重复扣物品或发奖励。

---

## 测试与质量

```bash
# 后端全量（pytest，asyncio_mode = auto）
TEMP="$PWD/.tmp" TMP="$PWD/.tmp" \
  backend/.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider backend/tests

# 只跑验收走查（不需要任何密钥）
TEMP="$PWD/.tmp" TMP="$PWD/.tmp" \
  backend/.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider \
  backend/tests/api/test_acceptance_story.py

# 前端：静态检查 + 构建（build = check-hook-order.mjs && vite build && fix-fragment-calls.mjs）
cd frontend && npx eslint src && npm run build
```

> `TEMP` / `TMP` 必须设置：pytest 默认临时目录在部分 Windows 环境下不可写；
> `-p no:cacheprovider` 同理，避免 `.pytest_cache` 写入失败。

- 最近一次全量：**1700 passed / 8 skipped**（跳过项为未启动 Neo4j 的图谱测试），约 **30 秒**。
  前端模块 **2984** 个，构建约 2.5 秒；eslint 0 error（12 条既有的 `react-hooks/exhaustive-deps` 警告）。
- 验收走查集中在 `backend/tests/api/test_acceptance_story.py`（5 个用例），覆盖 14 条验收：
  建世界/钥匙/拾取/无钥匙被拒/有钥匙通过/门状态与事件/任务进度/叙事/存档重载/幂等/
  叙事失败不回滚/非法事件配置/模拟只读/中文世界读中文。
- 测试按 `backend/tests/{api,db,engines,models,services,scripts}` 分层（127 个测试文件），
  包括效果注册表对齐、事件分支、NPC 知识可见性、并发 CAS、幂等重放、内容策略与本地回退、
  角色路由与降级、NPC 档案的知识隔离、演化提案的「未写入不叫已采纳」、游戏时钟暂停即冻结等。
- 架构场景跑成测试：`backend/tests/services/test_architecture_acceptance.py`（10 个用例）——
  NPC 对话、玩家补世界、世界长剧情、知识隔离、离开游戏、模型失败、系统代理降级、
  「一条普通行动完全不调用模型」，每个都打到真实入口与真实 SQLite。
- 前端静态检查接在构建链里：`npm --prefix frontend run check:hooks`（hook 顺序）与
  `check:agents`（调试台与演化面板的接线，防止「静默无数据」）。
- 前端构建先执行 `frontend/scripts/check-hook-order.mjs`，防止 `useCallback` 未声明就进依赖数组；
  构建完再跑 `scripts/fix-fragment-calls.mjs`；页面全部懒加载，产物在 `frontend/dist`。
- 数据层：`scenarios.db`（情景、知识、剧情线、预设、角色、偏好、供应商密钥）、`game_state.db`、
  `events.db`、`traces.db`，前三者的 `SCHEMA_VERSION` 为 **13 / 4 / 2**（`traces.db` 无版本表），
  打开旧库时按版本增量迁移；路径分别可用 `SCENARIO_DB_PATH` / `GAME_STATE_DB_PATH` /
  `EVENT_DB_PATH` / `LLM_TRACE_DB_PATH` 覆盖。

---

## 已知限制

取自 `docs/review-overview.md` 的 13 项验收矩阵中标记为"部分"的缺口、风险登记，
`docs/delivery_review.md` 里如实记录的未验证面，以及 `docs/review-2026-11-15.md` 的全量审查结论：

| # | 系统 | 状态 | 主要缺口 |
|---|------|------|----------|
| 1 | 世界与地图 | 部分 | 无可走格子、碰撞与自由寻路；跨区域只能逐段移动 |
| 2 | 实体与 NPC | 部分 | NPC 无自主行为；无关系网络界面 |
| 3 | 行动与交互 | 部分 | 无内置潜行、键盘方向移动、自由寻路 |
| 4 | 事件系统 | 可运行 | 无通用 call/return 栈与多路结果表（条件分支已覆盖常见用法） |
| 5 | 规则与条件 | 可运行 | 无通用数值公式 DSL |
| 6 | 任务与故事线 | 可运行 | 玩家选择来自事件，而不是任务图内建节点 |
| 7 | 物品与属性 | 部分 | 无独立交易面板；装备只加攻防 |
| 8 | LLM 调度 | 部分 | 判定由确定性规则承担，没有 LLM judge |
| 9 | 知识上下文 | 可运行 | 语义召回需自备 embedding 后端；默认只走 BM25 |
| 10 | 记忆与历史 | 部分 | 无统一 NPC 知识库视图 |
| 11 | 状态与存档 | 可运行 | — |
| 12 | 模型配置 | 部分 | 超时/重试未按功能独立配置 |
| 13 | 编辑器与调试 | 可运行 | 条件树仍以行式编辑 |

矩阵之外还有三处结构性缺口，同样来自审查文档：

- **旧聊天链路正在日落**：`POST /api/game/action` 现在**只有规则链路一条自动路径**——
  没有绑定世界的战役会返回 409 并说明原因，不再静默切到旧 LLM 执行分支；旧会话只能通过
  `compat_mode: true` 显式指定。面板读取（历史 / 日志 / 背包 / NPC 心智 / 记忆晶体 / 角色名册 /
  图谱搜索）已改为 `app/services/compat_views.py` 的只读投影，不再为了看一眼面板而重建
  `GameSession`。规则链路已把历史重建与静态 prompt 块从 `services/game_session.py`
  拆到 `session_rebuild.py` 与 `prompt_blocks.py`。剩余待办：旧存档的数据迁移（旧聊天记录
  不当作已验证事实导入权威状态，只作为可读记录保留），以及在前端不再依赖旧状态写入接口后
  删除 `GameSession.process_action()`。
- **叙事语言**只支持中/英二选一；世界工坊未暴露 `language` 字段。
- **作品库**是六部内置作品 + 可选模型补写，没有联网检索；**世界演化**只能新增，不能改字段或删除，
  故事线不在演化链路里；**角色路由**只有 narrative / system / memory 三个角色，不能按 NPC 单独分配模型。

风险登记里的三条 P1：**服务默认无鉴权**（本机使用；需要共享时设 `API_TOKEN`，
设置面板里填的令牌只存在当前标签页的 `sessionStorage`，关掉即失效）、
**SQLite 单写者**（并发写会以版本冲突重试，不会静默覆盖）、
**语义召回依赖外部 embedding 服务**（未配置时自动退回 BM25，不会静默降质成空结果）。

**后端只支持单进程运行。** `backend/app/api/routes_game.py:111` 的 `_sessions` 是进程内字典，
挂载着每个战役的模型实例与内存态（`_ensure_session` 命中就直接复用），所以 `uvicorn --workers N`
或多副本部署会出现"同一战役落在不同进程"的脑裂：NPC 心智、登记过的实例与缓存各算一套。
`launch.py` 起 uvicorn 时从不传 `--workers`，默认单进程；确实要扩到多进程，得先把会话状态外置。

### 环境相关的四处坑

1. **依赖用区间约束**（`fastapi>=0.115,<0.163`、`pydantic>=2.8.2,<3`、`litellm>=1.60,<2` 等），
   对齐当前 `.venv` 实测可用的版本；`graphiti-core` 是可选项，首装不装也能跑。
2. **pytest 必须设 `TEMP`/`TMP` 并关掉缓存提供者**，见上一节的命令。
3. **受限环境里 `vite dev` 可能白屏**（浏览器端依赖预打包被拦，裸导入 404）。这不是本项目
   的代码问题，改用 `--prod` 即可；桌面快捷方式已经默认 `--prod`。
4. **Windows 上构建桌面壳必须用 MSVC 工具链**，MinGW 编出来的 exe 会在创建 WebView2 窗口时失败。

---

## 来源与致谢

本仓库的基线是 **[horizonfps/project-lunar](https://github.com/horizonfps/project-lunar)**
（Project Lunar）的 tarball 快照（见提交 `fc0d73d`），此后在同一棵树上演进，历史里保留了上游那批提交。
世界文档模型、四层记忆晶体、NPC 心智与叙事引擎的原始设计来自上游；
本仓库在此基础上把判定与叙事彻底分离，改为规则链路优先的架构，并重写了编辑器与启动流程。

同时致谢：

- **litellm**、**FastAPI**、**React / Vite**、**Zustand** —— 底层框架
- **Graphiti** —— 时序知识图谱概念（可选镜像）
- **CLIProxyAPI** —— Anthropic / OpenAI 兼容订阅代理
- **Inner-Self** —— NPC 内心与人格系统的灵感来源
- **AI Dungeon** —— 交互式 AI 小说与故事卡的先驱

---

## 贡献

欢迎提交 Issue 与 Pull Request。请遵守一条原则：**这是一个引擎，不是一个游戏。**
不要在引擎里硬编码某个世界的专有逻辑（题材关键词、等级体系、具体角色名）；
所有世界相关的信息都应该来自作者定义的世界文档、知识条目与语气指令。

改动前请确认不破坏下面四条不变量（`backend/tests/api/test_acceptance_story.py` 会检查）：

1. **模型不写状态**。任何状态变化都必须经 `ActionService` 提交，模型输出只进叙事。
2. **失败不回滚**。叙事、图谱镜像、摘要失败都只记录，已提交的回合保持有效。
3. **幂等**。同一个 `request_id` 重放只返回同一份结果。
4. **无脚本**。条件、效果、后续事件都是校验过的数据；不要引入 `eval` 或任意代码执行。

提交前跑一遍全量后端测试；改前端请至少跑 `npm --prefix frontend run build`。
本仓库的开发约定（提交方式、临时目录、构建命令）见 [`AGENTS.md`](AGENTS.md)。

---

## 许可证

MIT，见 [`LICENSE`](LICENSE)。
