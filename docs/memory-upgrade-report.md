# 记忆与知识系统升级：实施报告（P0–P4）

本报告对应 `docs/memory-upgrade-audit.md`（源码审计）与 `docs/memory-upgrade-plan.md`（改造计划），
记录实际落地的改动、验收证据与未完成事项。任务书的十三个章节按下面的映射逐条对应。

## 一、总览

| 阶段 | 目标 | 状态 | 证据 |
| --- | --- | --- | --- |
| P0 | 语言处理统一、事实来源、记忆评分、永久记忆注入 | 完成 | `test_memory_tokenizer_consistency.py` 15、`test_memory_evidence_and_scoring.py` 15 |
| P1 | MemoryFact / Evidence / 状态机 / 写入管线 | 完成 | `test_memory_ingestion.py` 31 |
| P2 | 主题驱动合并、混合检索、实体索引、时间检索 | 完成 | `test_memory_retrieval_p2.py` 21 |
| P3 | NPC 独立认知与权限前置过滤 | 完成 | `test_memory_perspective.py` 10 |
| P4 | Context Pack 与 API | 完成 | `test_context_pack.py` 11、`test_routes_memory.py` 7 |
| 评测 | 独立数据集与指标 | 完成（语料 19 例） | `test_memory_evaluation.py` 9 |

全量回归：`1947 passed, 8 skipped`（`cd backend; .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`），
改造前基线为 `2 failed, 1819 passed, 8 skipped`。新增 128 个用例，既有失败 2 个已消除（见第五节）。

## 二、P0：现有系统修复

### 4.1 统一多语言 Tokenizer

审计结论与任务书假设不同：`memory_engine.py:13` 早已改用 `app.services.retrieval.tokenizer.tokenize_text`，
`knowledge_service.py:78` 同样如此。真正的第三套逻辑在 `game_session.py` 的故事卡打分，
它用 `re.findall` 只匹配拉丁字母三连以上，外加一份硬编码 EN/PT 停用词。

改动：`game_session.py:_extract_context_keywords` 与 `_score_card_relevance` 改走 `tokenize_text`，
过滤条件是 `not in _CONTEXT_STOP_WORDS and (has_cjk(token) or len(token) > 2)`。
三条检索路径现在共享同一词空间：中文二元组、日文假名与汉字、拉丁词归一化都对三处一致生效。

### 4.2 事实来源

`app/models/memory.py` 定义 `EvidenceType`：`engine_state`、`author_canon`、`committed_action`、
`npc_observation`、`npc_dialogue`、`player_statement`、`inferred_fact`、`llm_narrative`。
`EVIDENCE_TRUST` 给它们排序（1.0 / 1.0 / 0.95 / 0.80 / 0.50 / 0.40 / 0.30 / 0.20），
`evidence_type_of_event()` 把事件类型映射成证据类型。关键约束：叙事散文（`NARRATOR_RESPONSE`）
最高只能是 `llm_narrative`，因此它无论如何都到不了 `ACTIVE`——这是防止幻觉污染世界知识的机械保证，
不依赖 prompt 措辞。

### 4.3 记忆评分

保留 `_score_crystal` 作为 `legacy` 基准，新增 `hybrid` 模式（`MEMORY_SCORING`，默认 hybrid）：
`importance`、`confidence`、`observer_ids`、`temporal`、`task` 各自成为独立项，
`MEMORY_SCORING=legacy` 可一键回到旧权重。`test_memory_evidence_and_scoring.py` 的 15 个用例
逐项固定了新旧算法的差值（例如 confidence 打破平局时期望差值恰为 `_CONFIDENCE_WEIGHT * (1.0 - 0.2)`），
使「改了权重」这件事在测试里可见。

### 4.4 永久记忆注入

`build_context_window`（第 1038 行）与 `build_context_window_async`（第 1135 行）原本把
全部 MEMORY 层晶体无条件写进 `=== PRMNT_MEM ===`。现在两处共享 `_render_memory_tiers`，
MEMORY 层经 `_select_permanent_crystals` 按场景/人物/任务检索，只有 `always_include` 的晶体
无条件保留。`test_permanent_tier_is_no_longer_injected_whole` 固定了这一点。

## 三、P1：结构化事实与证据系统

### 新增表（独立 `memory.db`）

`backend/app/db/memory_fact_store.py`，`SCHEMA_VERSION = 1`，五张表：

| 表 | 作用 | 关键约束 |
| --- | --- | --- |
| `memory_facts` | 结构化三元组 + 有效性窗口 + 状态 | `UNIQUE(campaign_id, fingerprint)` |
| `memory_evidence` | 每条事实的来源 | `UNIQUE(fact_id, source_event_id, evidence_type, observer_id)` |
| `memory_entities` | 规范名与别名 | `UNIQUE(campaign_id, normalized)` |
| `memory_relations` | 实体间关系（带窗口） | — |
| `memory_ingest_cursor` | 消费进度 | 主键 campaign |

任务书建议的 `memory_events`、`memory_episodes` 没有新建：`events` 表与记忆晶体已经承担了它们，
重复建表会制造两个真相来源。`memory_versions` 同样不建——`supersedes_fact_id` 链已经表达版本关系，
并且旧事实不删除。`memory_embeddings`、`memory_clusters`、`memory_procedures` 留待有实际需求时再加。

### 状态机

`EXTRACTED → CANDIDATE → VERIFIED → ACTIVE → SUPERSEDED`，另有 `DISPUTED`、`REJECTED`、`RETRACTED`。
`update_status` 只允许**单跳**（`can_transition`），`advance_status` 才按 `transition_path` 逐跳走完。
这条区分是刻意的：多跳路径只用于 ingest 时把单跳序列走完，如果 `update_status` 也接受跳跃，
「候选必须先被验证才被相信」这个保证就失效了。

### 写入管线

`backend/app/services/memory_ingestion.py`：

```text
EventStore → collect(get_after 游标) → DeterministicExtractor + LLMFactExtractor
          → EvidenceValidator → ConflictDetector → commit(逐跳推进状态)
```

- 幂等：`add_fact` 按指纹去重，`add_evidence` 按唯一索引 `INSERT OR IGNORE`。
- 提取与提交分离：`propose()` 只产出 `FactProposal`，`commit()` 才写库；`dry_run` 完全不写。
- 失败可重试：LLM 异常只进 `extractor_errors`，游标按已处理事件推进。
- 原始事件只读：管线从不修改 `events`。
- 未知保持未知：提取提示词明确要求「不要推断补全」，证据类型来自事件映射。
- `INGESTIBLE_EVENT_TYPES` 不含 `MEMORY_CRYSTAL`，避免摘要回流让幻觉复利。

### 证据验证

`ValidationResult` 的四条分支都有测试：

| 情形 | 结果 |
| --- | --- |
| 三元组不完整 | `REJECTED` |
| scoped 事实但没有 observer | `CANDIDATE`（否则会泄漏给所有人） |
| `engine_state` / `author_canon` / `committed_action` | 可到 `ACTIVE` |
| 其余 | `CANDIDATE`，等佐证 |
| ≥2 个独立事件 + 最佳信任度 ≥ 0.80 + confidence ≥ 0.75 | `VERIFIED` |

## 四、P2：巩固与检索

### 5.1 主题驱动合并

`backend/app/engines/memory_consolidation.py`。`MEMORY_CONSOLIDATION` 默认 `theme`，
`time`/`legacy`/`old`/`0`/`false`/`off` 回到旧的「最老四条」。

按 `related_entities` 聚类成线索，一轮只合并**一条**线索（配额 2，线程更长时下一轮用同一 `theme_key` 继续）。
`always_include` 的晶体永不合并。无实体晶体按时间相邻挂到最近的线程，间隔阈值 `UNTAGGED_MAX_GAP_SECONDS = 6h`；
超过阈值的安静场景不挂，否则整局会被吸进唯一线程，合并悄悄退化成旧行为。

合并出的晶体记录 `theme_key`、`theme_keywords`、`source_memory_ids`，并随 `MEMORY_CRYSTAL` 事件持久化；
`session_rebuild.py` 重建时读回，`consumed` 改为按 `source_memory_ids` 命中推断，而不是按位置。

### 5.3 混合检索

`backend/app/services/retrieval/memory_retrieval.py`：

```text
Query → 实体解析(entity_index) → 并行召回 [bm25, alias, engine, fact]
      → RRF(rrf_k=60) → provenance rerank → 时间序输出
```

`engine` 通道把旧的 `_score_crystal` 排名当成一路信号，所以旧排名的胜出者仍然会胜出——
这是「不破坏既有行为」的具体做法，而不是把旧逻辑删掉。任一通道抛错只进 `degraded`/`errors`，
其余通道照常作答。`MEMORY_RETRIEVAL=score` 整层关闭。

### 5.4 实体与别名

`backend/app/services/retrieval/entity_index.py`：拉丁别名整词匹配 `(?<![^\W_])…(?![^\W_])`，
CJK 别名子串匹配，最长别名优先且不重叠。别名冲突时保留先登记者——把两个角色的记忆合并比多一条别名更糟。
`canonical()` 对未知名字返回空串而不是回显输入，否则查询里每个杂词都看起来像已解析实体。

### 5.5 时间检索

`find_facts(at=...)` 的语义是「那时是否成立」：带 `at` 时用 `status NOT IN (rejected, retracted)`，
并按 `valid_from <= at < valid_until` 裁剪；不带 `at` 才用 `LIVE_STATUSES = {verified, active, disputed}`。
`supersede()` 关闭旧事实的窗口而不删除行，所以「第二章是敌人」永远可查，只是不再是当前答案。

## 五、P3：NPC 独立认知

`backend/app/services/memory_perspective.py`。三分法的落点是三种数据结构而不是三段 prompt：

| 层 | 结构 | 语义 |
| --- | --- | --- |
| 世界真相 | `MemoryFact` + `observer_ids` | 引擎提交的记录，永不被 NPC 覆盖 |
| NPC 观察 | `MemoryCrystal.witnessed_by`（既有） | 该 NPC 实际在场并见证 |
| NPC 推断 | `Claim(stance=BELIEVES)` | 该 NPC 相信、可能是错的版本 |

`filter_facts_for_observer` 与 `build_perspective` 在**构建上下文之前**完成过滤，
`facts_for_prompt` 直接返回 prompt 行而不是事实对象——调用方拿不到未过滤的列表。
无 observer 的事实对所有人可见，这是为兼容既有数据刻意保留的口径：
在 observer 追踪出现之前写下的事实不能从所有 prompt 里消失。

`game_session.py` 新增 `_npc_knowledge_window`，两处调用点（第 2462、2516 行）都改走它；
provider 不支持 perspective 时回退旧的 witnessed 窗口，不会抛异常。

`build_npc_knowledge_window(campaign_id, npc_name, perspective=None)` 的第三参数是新增的，
不传时行为与改造前完全一致（既有 22 个 NPC 视野用例无需改动即通过）。

## 六、P4：Context Pack

`backend/app/services/context_pack.py`，输出结构对应任务书第七节：

```json
{ "campaign_id": "…", "mode": "narrator", "token_budget": 4000, "used_tokens": 83,
  "sections": { "current_state": { "items": [ … ], "dropped": 0 } } }
```

渲染顺序固定为 `scene → current_state → relevant_facts → unresolved_commitments
→ relevant_episodes → npc_perspective → sources`。当前权威 GameState 在记忆之上，
过期摘要无法与当前状态争论。

预算有**两道天花板**：整包余量，以及每段自己的份额（`SECTION_WEIGHTS`）。
只有前者时，一条长记忆会吃掉整包，下面的事实永远渲染不出来——这是实现过程中被冒烟测试抓到的真实缺陷，
改完后 `test_a_section_cannot_eat_the_whole_budget` 固定了它。

Token 估计用 `estimate_tokens`：CJK 一字一 token，拉丁四字符一 token。刻意不接真正的 tokenizer——
预算只需要保守且稳定，而在这里引入叙事层的 tokenizer 会为一个估计值增加一个依赖。

`dedupe_items` 去掉同段内重复行（保留首次出现）。渲染时预算不足的条目记入 `dropped` 并在 `notes` 里说明，
不静默截断。

## 七、API

`backend/app/api/routes_memory.py`，挂在 `/api/memory`：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/facts` | 默认只回 live 事实；`status=` 走审计视图，`at=` 问「那时」 |
| GET | `/facts/{id}` | 单条事实 |
| GET | `/facts/{id}/evidence` | 溯源，按时间 |
| GET | `/entities` | 实体索引 |
| GET | `/entities/resolve` | 任意别名 → 规范实体 |
| GET | `/overview` | 按状态计数 |
| POST | `/ingest` | **默认 `dry_run: true`**；`llm=false` 默认只用确定性提取器 |

`ingest` 默认 dry-run 是刻意的：操作员问「这批会提取出什么」时，不能顺手把事实写进正在运行的战役。
`test_ingest_defaults_to_a_dry_run` 断言了 dry-run 之后事实列表仍为空。

未知状态值返回 400 而不是被忽略——静默忽略会让调用方以为过滤生效了。

## 八、评测数据集与指标

`backend/app/services/memory_evaluation.py` 内置 19 例语料，按六类标签组织：
`fact_accuracy` 4 例、`long_term` 3 例、`temporal` 3 例、`permission` 3 例、`retrieval` 6 例。

每个用例声明 `expected`（必须出现）与 `forbidden`（不得出现），`run_cases()` 用真实回答函数回放。
指标函数：

| 指标 | 实现 |
| --- | --- |
| Recall@K / Precision@K / MRR | 复用 `app.services.retrieval.evaluation`，与知识检索同一套公式 |
| `leak_rate` | 含不该看到的秘密的 prompt 占比 |
| `compression_retention` | 压缩后存活的关键事实占比 |
| `tokens_per_injection` | 每次上下文注入的平均 token |

共用公式是有意的：两套测量召回的方法会让两个系统无法比较。
注：`precision_at_k` 的分母是实际返回条数而非 K，短排名不因不存在的文档受罚——
这是既有实现的口径，测试里按实际行为断言并在注释里写明。

## 九、环境变量总表

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `MEMORY_DB_PATH` | `backend/memory.db` | 事实库位置；删除该文件游戏照常运行，只是丢已验证事实 |
| `MEMORY_SCORING` | `hybrid` | `legacy` 回到旧权重 |
| `MEMORY_RETRIEVAL` | `hybrid` | `score` 关闭混合检索 |
| `MEMORY_CONSOLIDATION` | `theme` | `time` 回到「最老四条」 |
| `MEMORY_CONTEXT_PACK` | 未启用 | 保留开关 |

所有新依赖都可关闭；没有 Embedding、没有 Neo4j 时系统完整运行——
`test_memory_retrieval_p2.py` 里有无 fact store、无 graph 的用例。

## 十、验收对照（任务书第十三节）

| 要求 | 状态 | 落点 |
| --- | --- | --- |
| 1 记住长期重要事件 | 完成 | 混合检索 + 主题合并；`long_term` 语料 |
| 2 区分事实/观点/传闻 | 完成 | `EvidenceType` 信任度 + 状态机 |
| 3 关系随时间变化 | 完成 | `valid_from`/`valid_until` + `at=` 查询 |
| 4 按实体与语义召回 | 完成 | `entity_index` + alias/fact 通道 |
| 5 按 NPC 视角限制 | 完成 | `filter_facts_for_observer`，构建前过滤 |
| 6 从原始事件追溯 | 完成 | `memory_evidence` + `/facts/{id}/evidence` |
| 7 防幻觉污染 | 完成 | 散文 ≤ `llm_narrative`，到不了 ACTIVE |
| 8 有限预算内给相关记忆 | 完成 | Context Pack 双天花板 |
| 9 无向量库/无 Neo4j 可运行 | 完成 | 全部可选，异常降级 |
| 10 不破坏既有状态与存档 | 完成 | 独立 memory.db；1947 passed |

## 十一、尚未完成与已知限制

1. **Context Pack 尚未接入 `game_session` 的叙事路径**。工具层、测试、指标都就位，
   但 `_handle_narrative` 仍走 `build_context_window_async`。接入需要同时替换缓存区（FASE 2）
   的分层逻辑，属于一次独立改动，建议单独评审。
2. **没有 Cross-Encoder 精排**。任务书列为「后续可选」，未做。
3. **没有向量召回接入记忆晶体**。`vector_retriever` 存在且被知识层使用，记忆层未接；
   需要先有可用的多语言 Embedding 后端。
4. **NPC 信念只从显式 `belief` payload 读取**。刻意不从散文推断——那会重新引入这一层要消除的歧义。
   目前没有产出 `belief` 的写入方。
5. **`memory_procedures` / `memory_clusters` 未建**。程序记忆（任务书第三节第 4 类）还没有独立表，
   行为经验目前仍以事实形式存在。
6. **既有 2 个失败已消除，但方式是改测试隔离**：`test_llm_router_custom_route.py` 原先直接写
   `backend/scenarios.db`，本机沙箱拒绝；已加 `isolated_prefs_db` autouse fixture。这是测试卫生问题，
   不是产品缺陷修复。


## 十三、第二轮：对话可用性修复（2026-11-15）

第一轮改完之后，用户报了新问题：「严重问题：游戏根本不能由对话快速的有效推进，
游戏处于不可用的状态」，并指出系统给的选项和自己说的话对不上。这一轮不涉及记忆架构，
而是修玩家每轮都会经过的那条链路。

### 根因（三处叠加，都不是某一条规则写错）

1. **认不出的句子走 `_improvise`，而它 `effects=[]`。**
   `app/engines/rules_engine.py` 的 `_improvise` 原文注释就写着 "changes no state of its own"，
   返回 `code="improvised"` 与一句回显。玩家说什么，世界都不变——自由输入形同虚设。
2. **建议清单完全不看玩家说了什么。**
   `app/services/rejection.py` 的 `available_actions(world, state, chinese)` 没有 `text` 参数，
   因此每一轮都给出同一份「前往 X、对 Y 说话、拿起 Z」。玩家读到的是「系统没在听我说话」。
3. **语言判定读的是空字段。**
   `app/models/game.py:389` 的 `language` 默认 `""`，所有 starter world 预设都没填，
   于是中文世界也按英文渲染，并夹带 `(Names must match what already exists in this world.)`
   这种开发规范。

### 改了什么

| 位置 | 改动 | 为什么 |
| --- | --- | --- |
| `app/engines/rules_engine.py` `_improvise` | 改为真实动作：耗 2 分钟；在场者各得一条 `adjust_alert` 效果；玩家原话用「」引起来，不再由引擎改写 | 没有后果的动作等于没做；但绝不给物品、开门、发任务，自由文本不能成为万能钥匙 |
| `app/engines/rules_engine.py` 新增 `_blocked_door_message` | 门关着时说明「需要什么钥匙」以及「钥匙是否就在此处 / 是否已在身上」 | 原来只说「先想办法打开它」，玩家手里没有任何可继续的线索 |
| `app/services/rejection.py` `available_actions` | 新增 `text` 参数；先回应玩家点名过的出口/人物/物品，**只有**什么都没点名时才退回房间通用清单 | 这是「选项和自己的话对不上」的直接来源 |
| `app/services/rejection.py` 新增 `_who_is_where` | 句子里点了名、而那个人在别处时，直接回答「X 不在这里，在 Y」 | 「向守林人开口」曾被回成「这里没有叫这个名字的东西」，既没回答也没否认 |
| `app/services/rejection.py` `rejection_message` | 删掉 `（名字要和世界里现有的一致…）` 与英文对应的 `(Names must match…)`；不再复读玩家原句 | 前者是开发规范冒充玩家反馈，后者只是把玩家写的字退回去 |
| `app/services/narrative_service.py` `_localise` | 改用 `detect_language(world)` 推断，不再读空的 `world.language` | 中文世界输出英文的根本原因 |
| `app/utils/language.py` 新增 `detect_language` | 显式字段优先，否则统计世界里 CJK 与拉丁字母的比例 | 一次性修好所有读取语言的地方 |
| `app/engines/action_registry.py` | move / wait / talk 动词表补齐：走向、走过、进入、坐下休息、歇一会儿、打招呼、问候… | 「走向仓库」「坐下休息一会儿」原先落到 improvise |
| `app/services/text_action_service.py` | `_move_candidates` 加入当前所在地；`_loose_place` 增加近似名匹配（最长公共子串 ≥ 2） | 「去村子广场」对不上「村口广场」；「你已经在 X」被当成没听懂 |

### 行为对比（中文 fantasy 预设，村口广场，无 NPC）

| 输入 | 之前 | 现在 |
| --- | --- | --- |
| 去村子广场 | 拒绝 missing_target | 你已经在村口广场了。 |
| 走向仓库 | improvise，零变化 | 仓库门锁着，需要铁钥匙。它就在这儿。 |
| 坐下休息一会儿 | improvise，零变化 | 你等了大约 10 分钟。 |
| 我四处打听一下守卫的事 | 你试了试：四处打听…（无后果） | 你试着「四处打听一下守卫的事」。周围没有别人看见，村口广场还是老样子。 |
| 向守林人开口 | 这里没有叫这个名字的东西 | 守林人不在这里，在林边。 |
| 拿起铁钥匙（当前为空手） | 提示里不出现铁钥匙 | 拿起 铁钥匙 |

### 测试

新增 `backend/tests/services/test_conversation_progress.py`（8 例），把上面三条根因各钉成断言：
尝试必须耗时、在场者必须收到 `adjust_alert`、空房间必须直说没人看见、英文世界不得输出中文、
点名者的建议必须排在第一位、未点名时才退回房间清单。

调整了两条记录旧契约的用例：`test_text_action_parser.py` 原断言 `self_effects == []`
（正是旧设计），改为允许 `seed_entity` 与 `adjust_alert`，同时继续断言不移动、不进背包；
`test_rejection_hint.py` 原断言消息以 `I could not match` 开头、并包含玩家原句，
改为断言这两者都不再出现。这是有意的契约变更，不是为了让测试变绿。


### 第二轮补充：自查发现的五处问题

修完上面三条之后又做了一轮自查，发现的问题里有三处是**我自己上一轮改动留下的**：

| 问题 | 根因 | 处置 |
| --- | --- | --- |
| `adjust_alert` 没有对应叙事句 | 我让 `improvise` 产生警觉效果，但 `narrative_service.describe_effects` 不认识这个 kind：状态变了、文字没说，玩家视角等于没变 | 新增分支，输出「X 注意到了你。」 |
| 「这个人在哪」可能给出过期位置 | `_who_is_where` / `_people_nearby` 读 `EntityDef.location_id`，那是**作者写下的初始位置**；NPC 走开后的真实位置在 `state.entities[id].location_id`（`event_engine.entity_location` 就是这个规则） | 新增 `_entity_place(state, entity)`，运行时记录优先 |
| 谈话候选没跟上实际位置 | `_talk_candidates` 用 `EntityDef.location_id == state.location_id` 判定在场，已离开的 NPC 仍被算作在场，解析器指过去、规则再回「他不在这里」 | 新增 `TextActionParser._living_candidates(world, state, anywhere)`，统一走 `entity_location` + `entity_alive` |
| **LLM 兜底开关接在了没人用的路由上** | `ENGINE_INTENT_ADVISOR` 只注入 `routes_engine.py:2165`，而前端 `frontend/src/api.js:108` 只调用 `/api/game/action` → `routes_game.py:631` 构造服务时没传 `advisor` | `routes_game._unified_service` 补 `advisor=_build_advisor()`（惰性导入避免循环），新增 `tests/api/test_advisor_wiring.py` 守护 |
| `_all_entity_candidates` 不过滤死亡 | 默认分支走 `_entity_candidates`（有 `entity_alive`），anywhere 分支没有 | 合并进 `_living_candidates`，两条路径同一套判定 |

第一条是最值得记的教训：**效果改了、叙事没改，等于没改**。引擎状态变化如果没有一句话讲出来，
玩家看到的就是「什么都没发生」——这正是这整轮要解决的问题本身。

第四条说明另一个更普遍的陷阱：**开关存在 ≠ 开关生效**。功能注入到了 UI 从不调用的那条路由上，
配置齐全、测试全绿、玩家依旧是零响应。这类问题只有「沿着客户端真实入口走一遍」才能发现。


### 第二轮再补充：让尝试真的换来线索

「空房间里 improvise 仍然什么都没推进」是这一轮最后解决的一条。世界模型里没有「线索」实体，
`QuestDef` 只有 `obtain_item` / `open_door` / `reach_location` / `flag` 四种目标，所以诚实的推进方式是
**把引擎已经知道、玩家还不知道的事说出来**，而不是编造发现。

| 位置 | 改动 | 约束 |
| --- | --- | --- |
| `app/engines/rules_engine.py` 新增 `_improvise_reveals` / `_most_useful_lead` / `_objective_done` | 房间里没人时，若句子含「打听/寻找/检查/investigate/search」等标记，按「可开的门 → 锁着的门及钥匙 → 未完成任务下一步」顺序给出一条**世界已有**的信息 | 不引入任何新事实；无标记或世界无可说之事时保持原来的「还是老样子」 |
| `app/engines/rules_engine.py` `_talk` | `You talk to X.` / `There is nobody here to talk to.` 按世界语言输出 | 中文世界不再出现英文模板 |
| `app/services/narrative_service.py` `render_fallback` | 人名不再靠 `message.startswith('You talk to ')` 提取，改为取 effect 的 `target`，回退按世界里的名字匹配 | 原来依赖英文前缀，消息一本地化就必然失配（这一轮刚踩到） |

### 一处更正

我此前说 `adjust_alert` 是「只写不读的计数器」，**这个说法不准确**：`app/engines/event_engine.py:196` 的
`entity_alert` 条件与 `app/engines/npc_dossier.py:243` 都会读它，它是有效状态。准确的说法是：
目前没有**规则**消费它来改变 NPC 行为，它的价值还停留在可查询层面。


## 十四、第三轮：收尾（把「未完成」逐条关掉）

上一轮结束时留了五条未完成事项。这一轮把其中四条真正做完，第五条给出明确结论。

### 14.1 向量召回接进记忆晶体

`app/services/retrieval/memory_retrieval.py` 新增 `build_crystal_vector_channel(crystals, *, builder=None)`：
惰性导入 `app.services.retrieval_embedding.build_vector_channel`，因此导入记忆层永远不会导入模型栈；
没有后端时返回 `None`（"没有这条通道"），不是空结果，两者在 trace 里可以区分。
`hybrid_rank(...)` 新增 `vector=` 参数，通道排在最后：语义通道是唯一能"凭意思"加分的一路，所以它可以**带进**
关键词找不到的晶体，但 RRF 仍然让关键词与实体通道的共识压过一条孤立的余弦命中。

`app/engines/memory_engine.py` 新增 `_vector_channel(campaign_id, docs)`，按 doc-id 集合做进程内缓存
（`_VECTOR_CACHE_LIMIT = 8`，超出淘汰最早一条），并提供 `invalidate_vector_cache()`。缓存的空值是一个刻意的哨兵：
后端不可用时不会每回合重建一次索引。

验收：`tests/services/test_memory_vector_channel.py` 12 例——无后端时排序与旧版逐条一致、有后端能召回
字面不重合的晶体、关键词共识仍能压过同分余弦、通道抛错只记 `degraded` 不中断、引擎对同一语料只建一次通道。

### 14.2 Context Pack 接进叙事路径

新增 `app/services/narrator_pack.py`，把包里那七段映射回叙事提示词原本读的三块：
`current_state`（NPC 状态 + 背包）、`relevant_facts`（结构化事实，带 `(fact)`/`(candidate)` 前缀）、
`relevant_episodes`（晶体窗口 + 日志）。渲染出来的**标题一字不改**，改的只是"预算内留下什么、按什么顺序"。

`game_session.py` 新增 `_from_context_pack(...)` 与 `_live_facts(query)`：前者把三块折成一份包再拆回来，
任何异常都退回原始三块（布局不是玩法，不该让玩家丢一回合）；后者从事实库取活着的事实，按查询词命中数排序，
没有 memory.db 时返回空列表——这正是出厂状态。

开关：`MEMORY_CONTEXT_PACK=1` 打开（默认关闭），`MEMORY_CONTEXT_PACK_TOKENS` 可显式指定预算，
否则按上下文窗口的 10% 取并夹在 1200–6000 之间。默认关闭是刻意的：它改变的是提示词的**内容选择**，
而对大多数世界来说旧路径本来就装得下，先让需要它的人在长存档上打开。

顺手修的一处真实缺陷：`context_pack.render()` 只有在某段**全部**被丢弃时才写 note，部分丢弃是静默的。
现已在该段有任何 item 越预算时记录 `dropped N item(s) over budget`——"压缩不能静默丢信息"这条约束同样适用于布局层。

验收：`tests/services/test_narrator_pack.py` 21 例——开关语义、标题不变、活状态先于回忆、
`(candidate)` 不会被当成事实、预算紧张时丢的是长回顾而不是当前棋盘、丢弃有计数。

### 14.3 NPC 信念有了写入方

`app/services/memory_perspective.py` 新增 `BELIEF_KEY_PREFIX`、`belief_key(subject, predicate)`、
`remember_belief(minds, campaign_id, npc_name, subject_id=, predicate=, object_value=, turn=)`。
信念存进**该角色自己的 mind** 里，键形如 `belief:<predicate>:<subject>`，于是不需要第二套数据库，
也不会与别的角色混在一起。

两个刻意的选择：
- **信念不过期**（`decay_after_turns=0`）。"公爵依然以为她死了"这件事的价值就在于它一直留着。
- **写入不做验证**。传闻本来就是 RPG 里角色获知信息的主要方式；拒绝存储无法验证的说法恰好是反的——
  说法本身就是数据，验证是在它跟世界事实对比时才发生的（`build_perspective` 里降级为 `knows` 或保留为
  `believes` 并记进 `mistaken_fact_ids`）。

读回侧 `MemoryEngine.stored_beliefs(campaign_id, npc_name)` 接入 `build_npc_perspective`，
并通过新参数 `MemoryEngine(..., minds=)` / `set_mind_store()` 注入；`routes_game._attach_memory_layers()`
在启动时挂上 `NpcMindEngine` 与 `MemoryFactStore`（`MEMORY_FACTS_ENABLED=0` 可关，打不开就退化为没有）。

验收：`tests/services/test_npc_beliefs.py` 15 例——写入落进角色自己的 mind、空值不入库、
名字含冒号不至于撕裂键、写进去的信念能被读回成 claim、与世界观一致时降级为 `knows`、
无关角色看不到别人的私事。

### 14.4 迁移与回滚脚本

`backend/scripts/migrate_memory_facts.py`：`--status` 只读报告、`--apply [--backup]` 跑累积迁移、
`--rollback <bak>` 恢复备份。回滚刻意做成**文件恢复**：这里的迁移只增加表与索引，
对一次坏发布的诚实恢复是"把原来的文件放回去"，而不是一个可能把好事实改写成别的形状的 down-migration。
回滚前会拒绝小于 1KB 的备份（一次手误就能毁掉事实历史，而这个检查只值一次读取）。

`MemoryFactStore` 相应新增 `schema_version` 与 `db_path` 两个只读属性。
实测：`--status` 报 `version 0 → target 1`，`--apply` 报 `before 0, after 1`。

### 14.5 仍未做（结论而非遗留）

| 事项 | 结论 |
| --- | --- |
| Cross-Encoder 精排 | **不做**。当前 rerank 只重排不新增，词表大小为几十到几百；引入一个本地 Cross-Encoder 会把默认安装变成"要下模型才能玩"，与本任务书第 11 条第 9–10 项冲突。需要时它是 `reranker.py` 的一个替换实现，接口已经就位 |
| `memory_procedures` / `memory_clusters` | **不建**。程序记忆的世界模型里没有可执行行为的载体，簇则已经由 `memory_consolidation` 的 theme 表达；建空表只会制造"看起来支持了"的错觉 |
| 前端构建验证 | **环境限制，非代码问题**：`npm --prefix frontend run build` 在此沙箱报 `spawn EPERM`（esbuild 起子进程被拒），与 git 的 EPERM 同源 |
| 记忆评测的量化基线 | 语料 19 例与指标函数已就位（Recall@5/10、Precision@K、MRR、泄漏率、压缩保留率、token 量），但**没有跑出旧 vs 新的对比数字**——那需要一个固定的存档与一次真正的 LLM 会话，本环境跑不了 |

## 十五、第三轮改动清单

新增：

- `backend/app/services/narrator_pack.py`
- `backend/scripts/migrate_memory_facts.py`
- `backend/tests/services/test_memory_vector_channel.py`（12）
- `backend/tests/services/test_narrator_pack.py`（21）
- `backend/tests/services/test_npc_beliefs.py`（15）

修改：

- `backend/app/services/retrieval/memory_retrieval.py`（`CHANNEL_VECTOR`、`vector=` 参数、`build_crystal_vector_channel`）
- `backend/app/engines/memory_engine.py`（`_vector_channel`、`invalidate_vector_cache`、`stored_beliefs`、`minds=` 参数）
- `backend/app/services/context_pack.py`（部分丢弃也记 note）
- `backend/app/services/memory_perspective.py`（`belief_key` / `remember_belief`）
- `backend/app/services/game_session.py`（`_from_context_pack`、`_live_facts`、`_fact_query_tokens`）
- `backend/app/engines/npc_mind_engine.py`（公开 `ensure_mind`）
- `backend/app/api/routes_game.py`（`_attach_memory_layers`）
- `backend/app/db/memory_fact_store.py`（`schema_version` / `db_path` 属性）

回归：`tests/services + tests/engines + tests/api` = **1896 passed, 8 skipped, exit 0**。

## 十六、第四轮：审查本轮全部改动（2026-11-16）

对第一至第三轮的全部改动做逐文件审查（`git diff` + 新增文件通读 + 探针脚本），发现并修掉以下问题。全部是**真实缺陷**，且多数是我自己前几轮引入的。

### 16.1 性能与死代码

| 位置 | 问题 | 修法 |
| --- | --- | --- |
| `memory_engine.py` `_hybrid_recall` | `cache = self._entity_cache_for()` 是从未使用的死变量 | 删除 |
| `memory_engine.py` `engine_scores` / `_select_permanent_crystals` | 用 `crystals.index(crystal)` / `permanent.index(crystal)` 定位排名。`MemoryCrystal` 是 25 字段 dataclass，`list.index` 逐字段比较，等于每回合 O(n²) 次字段比较 | 改成 `positions = {id(crystal): rank ...}` 映射，按对象身份取排名 |
| `game_session.py` | `_fact_query_tokens` 被定义了两次（内容相同），后一个覆盖前一个 | 只留一份，并改用模块级已导入的 `tokenize_text`（原来在函数里做局部 import） |

### 16.2 规则层：一处真实递归与一处 NameError

- `rules_engine._talk` 在「没人点名但屋里只有一个人」时**递归调用自己**。若那人的位置记录与房间不一致，`_talk` 的 `entity_not_here` 分支会再次落到同一段 fallback，形成自调用。修法：新增 keyword-only 参数 `auto: bool = False`，fallback 传 `auto=True`；`entity_not_here` 分支在 `auto` 下直接答 `talked` 而不是再次派发。
- `_someone_not_here` 是 `staticmethod`，我给它加中文本地化时写了 `self._is_chinese(world)` → `NameError: name 'self' is not defined`。只有走中文分支才炸，英文测试全绿，是审查中读 diff 才发现的。修法：静态方法内直接 `detect_language(world) == "zh"`。

### 16.3 文案层：中文标点拼接

`rejection_message` 把「原因」和「可以试试…」用全角句号连起来，而 `body` 自身以「可以试试」开头是个动词，正确读法是另起一句。现在两者都用 `。` 分隔；并且不再回显玩家原句（原句不是原因）。英文侧同样改用独立句。

### 16.4 Context Pack：预算被静默突破（最重要的一处）

这是审查中发现的**最严重缺陷**，因为它让 P4 的 token budget 在实际叙事路径上不起作用：

1. `narrator_pack.render_narrator_pack` 读的是 `section.items` —— 那是 pack **构建时**的全部条目，而 `render()` 只是跳过装不下的条目、并不从 `items` 里移除它们。于是被预算拒绝的文本原样进了提示词。
2. `render()` 的「整包余量」判定用的是 `remaining - cost` 而不是 `used`，而 `used` 要到本轮循环末尾才累加 —— 一整个多行的记忆块（`[context_pack 把整段记忆拼成一个 item]`）因此可以一次性突破两道天花板。
3. `render()` 只做「整条丢弃」，一个 3000 token 的记忆块要么全进要么全丢，中间没有裁剪。

修法（`context_pack.py`）：

- `ContextItem` 新增 `pinned: bool`；`current_state` 段一律 `pinned=True`（当前权威状态永远不被丢掉，与晶体选择器对 `always_include` 的处理一致）。
- `render()` 新增两个内部工具：`_room()` 算出该条目在各道天花板下**剩多少**；`_clip()` 把超长条目按行裁到能装下的长度（裁到 4 个字符以下就判定为「不是记忆」而丢弃）。
- 每条目的开销现在同时比较「本段份额」与「整包余量」，且整包余量按 `remaining - cost` 算（本段已装条目在循环末尾统一扣，不能在循环内重复扣）。
- `ContextSection` 新增 `kept: List[str]`，`render()` 把真正放行的行写进 `kept`；`narrator_pack` 改读 `kept` 而不是 `items`。
- 裁剪会写 `clipped N oversized item(s) to fit the budget` 的 note —— 裁剪是真实的信息损失，必须和丢弃一样被记录。

新增 `ContextPack.render_report()`，一次返回 `(rendered, dropped, note_count)`。原因：这个 pack 每回合被渲染两次（先量一次、再出提示词），`notes` 是累加的，事后统计会重复计数。

### 16.5 narrator_pack：空块变成了带来源的空行

`build_narrator_pack` 过去无条件传 `[npc_states, inventory]` / `[memory_context, journal]`。入参为 `""` 时 `_as_items` 会产出 0 个条目——但 `render()` 的 `kept` 一并入后，空串也会被当成一条，渲染出 `WORLD MEMORY:` 后跟三行 ` [source: memory]`。修法：构建前过滤掉空块。

### 16.6 权限过滤补到 API 层

`GET /api/memory/facts` 之前没有 observer 参数 —— 也就是说，谁问都能拿到全部事实，NPC 视角隔离只存在于引擎内部。现在加 `observer=` 参数，走同一个 `filter_facts_for_observer`；查询时故意多取 10 倍再裁剪，否则「前 50 条全是被隐藏的事实」会返回空。新增 `test_an_observer_does_not_see_a_secret_it_was_never_told` 守住它。

### 16.7 测试调整（契约澄清，不是为变绿）

- `test_the_dropped_part_is_counted_not_silently_lost` 原先断言 `sum(section.dropped) > 0`。现在超长块会被**裁剪**而不是整条丢弃，`dropped` 为 0 是正确的。改为断言「notes 非空且渲染结果不超过预算」——这才是玩家真正关心的事。
- 新增 `test_the_rendered_text_respects_the_token_budget`，直接对渲染结果算 token 且要求 `<= token_budget`。上面 16.4 的三个 bug 全都逃得过原有断言，只有这一条能抓住。

### 16.8 回归

`tests/services + tests/engines + tests/api` 全量：**2039 passed, 8 skipped, exit 0**（57.71s）。改造前基线为 `2 failed / 1819 passed / 8 skipped`。

一次全量跑里 `tests/api/test_desktop_shell.py` 有 4 例失败（`test_the_window_opens_the_app_from_the_same_port` 等）。单独跑该文件 3 passed，是桌面外壳用例对并发/端口状态的感知问题，与本轮改动无关；重跑全量已全绿。

### 16.9 本轮改动文件

- `backend/app/engines/memory_engine.py`（删死变量、两处 `index()` → 身份映射）
- `backend/app/engines/rules_engine.py`（`_talk(auto=)` 守卫、`_someone_not_here` 去掉 `self`、剩余英文字串中文化）
- `backend/app/services/text_action_service.py`（DO 模式 talk 回读分支去自赋值）
- `backend/app/services/rejection.py`（中英文案分句）
- `backend/app/services/context_pack.py`（`pinned`、`_room`、`_clip`、`kept`、`render_report`、裁剪 note）
- `backend/app/services/narrator_pack.py`（读 `kept`、空块过滤、预算透传）
- `backend/app/services/game_session.py`（去重复定义、用模块级 tokenizer）
- `backend/app/api/routes_memory.py`（`observer=` 过滤）
- `backend/tests/services/test_narrator_pack.py`（+2 例）
- `backend/tests/api/test_routes_memory.py`（+1 例）

## 十二、改动文件清单

新增：

- `backend/app/models/memory.py`
- `backend/app/db/memory_fact_store.py`
- `backend/app/services/memory_ingestion.py`
- `backend/app/services/memory_perspective.py`
- `backend/app/services/context_pack.py`
- `backend/app/services/memory_evaluation.py`
- `backend/app/services/retrieval/entity_index.py`
- `backend/app/services/retrieval/memory_retrieval.py`
- `backend/app/engines/memory_consolidation.py`
- `backend/app/api/routes_memory.py`
- `backend/tests/test_conftest_contract.py`
- `backend/tests/engines/test_memory_evidence_and_scoring.py`
- `backend/tests/services/test_memory_ingestion.py`
- `backend/tests/services/test_memory_retrieval_p2.py`
- `backend/tests/services/test_memory_perspective.py`
- `backend/tests/services/test_context_pack.py`
- `backend/tests/services/test_memory_evaluation.py`
- `backend/tests/services/test_memory_tokenizer_consistency.py`
- `backend/tests/services/test_conversation_progress.py`（第二轮）
- `backend/tests/services/test_say_always_answers.py`（第二轮）
- `backend/tests/services/test_say_listener_not_here.py`（第二轮）
- `backend/tests/services/test_talk_narration.py`（第二轮）
- `backend/tests/services/test_say_end_to_end.py`（第二轮）
- `backend/tests/api/test_advisor_wiring.py`（第二轮）
- `backend/tests/services/test_memory_tokenizer_consistency.py`
- `backend/tests/api/test_routes_memory.py`

修改：

- `backend/app/engines/memory_engine.py`（混合检索接线、`_render_memory_tiers`、perspective 参数）
- `backend/app/services/game_session.py`（tokenizer 统一、`_npc_knowledge_window`）
- `backend/app/services/session_rebuild.py`（`theme_key`/`source_memory_ids` 重建）
- `backend/app/main.py`（挂载 memory 路由）
- `backend/tests/conftest.py`（恢复原有的 `DEFAULT_OPENING`/`make_scenario`，叠加沙箱 shim）
- `backend/tests/engines/test_llm_router_custom_route.py`（数据库隔离）
- `backend/tests/engines/test_memory_engine.py`（测试工厂补 `memory_id`）