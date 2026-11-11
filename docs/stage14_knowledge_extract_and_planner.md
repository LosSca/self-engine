# 阶段 14：从叙事中提炼知识，以及交互预演

本轮落地的是 m09500 需求 ③（对话式补全的落盘半程）④（从文本抽取分类信息与记忆）⑤（基于交互的体验系统）。

## 一、为什么「提炼」只写知识库，不写世界与状态

抽取是启发式的：它把叙事句子切成候选条目，判断它更像「人物」还是「事件」。
启发式会出错，这是设计前提而不是缺陷。因此这条通道被限定为**提案通道**：

- 默认 `apply=false`，只返回候选，不落盘。
- `apply=true` 时只写 `scenario_knowledge` 一张表。
- 没有任何规则读取知识条目：知识只进入叙事上下文。

结论：一条错误提案的代价是「叙事里多了一句不准的背景」，而不是「存档被改坏」。
这是需求里「记忆与知识更新不得静默覆盖权威状态」的直接落地。

## 二、确定性优先的抽取规则（backend/app/services/knowledge_extractor.py）

| 步骤 | 规则 |
| --- | --- |
| 切句 | 句号、感叹号、问号、换行；保留原文，不做改写 |
| 排除 | 短于 2 字的句子、疑问句不算事实 |
| 归类 | 含第二人称（你/您）的句子归「人物」，其余叙事句归「事件」 |
| 去重 | 以 (layer, 句子) 归一化后去重，同一句只出一条 |
| 跳过 | 与场景已有条目内容相同的候选不进提案 |
| 来源 | 每条提案带 `request_id` 与 `state_version`，可回溯到具体回合 |

模型（可选）只负责改写措辞，不负责决定写什么：`refine=true` 且路由可用时才调用；
模型失败时返回原始提案加一条 warning，整批不丢。

## 三、交互预演（backend/app/services/interaction_planner.py）

需求⑤要的是「交互体验」：玩家在房间里能看见能做什么、被拒时能知道缺什么。
这件事做错的方式有两种，都被刻意避开：

1. 预览自己重写一套规则判断 → 会与真实判定漂移。
   现在每个候选都走 `RulesEngine.evaluate`，事件走真实 `event_engine`，
   预览说「能开」与真实回合说「能开」用的是同一段代码。
2. 预览顺手提交 → 违反「只有行动服务能改权威状态」。
   现在状态在开始前深拷贝，任何一步都不写回；测试断言 `version`/`turn` 不变。

三个接口：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/api/v1/worlds/{world_id}/plan?campaign_id=` | 当前地点每个候选行动的判定，分成 allowed / blocked |
| POST | `/api/v1/worlds/{world_id}/preview-actions` | 最多 6 步的连锁预演，逐步列出字段变化与触发事件 |
| POST | `/api/v1/worlds/{world_id}/explain` | 某个行动为什么被拒，以及达成条件需要几步 |

`plan` 不带 `campaign_id` 时用新建的起始状态（编辑器场景：「第一回合这个房间能做什么」），
带 `campaign_id` 时读该战役的状态副本；未知战役 404。

`explain` 的规划是**有界**的：只规划两种形状（`has_item` / `at_location`），
可达距离用 BFS，且每一步都经过规则引擎确认，所以「门后的走廊」不会被算成可达。
规划不出来的条件进 `unplannable`，如实返回，不猜测。

## 四、前端入口

- `frontend/src/api.js`：`planRoom` / `previewActions` / `explainAction`。
- `frontend/src/components/WorldStudio.jsx` 调试页签新增「交互预演」卡片：
  点一次即可看到当前地点全部可行与被拒行动，每个被拒项带「为什么」按钮，
  展开后显示 `code`、规则原话、所需物品/地点、距离步数与最短路径。
- `frontend/src/components/WorldStudio.jsx` 新增「记忆提炼」页签：选择战役、勾选「只预览」，
  显示读了多少回合、多少条提案、写入多少条、跳过了哪些以及 warnings。

## 五、测试

| 文件 | 数量 | 覆盖 |
| --- | --- | --- |
| backend/tests/services/test_knowledge_extractor.py | 12 | 切句、疑问排除、归类、去重、来源、跳过已知、预览不写、apply 幂等、模型坏掉只报警告 |
| backend/tests/api/test_routes_knowledge_extract.py | 4 | 预览不写、apply 幂等、未知场景 404、无模型 503 |
| backend/tests/services/test_interaction_planner.py | 10 | 候选判定、不改状态、逐步变更、事件结算、首步被拒即停、非法链条、需求链与距离 |
| backend/tests/api/test_routes_planner.py | 5 | 三个路由的正常与异常路径、预演后状态未变 |

全量后端：**1482 passed, 8 skipped**（约 132s）。

## 六、未做与已知限制

- 抽取只读 `narrator.response` 事件，不读玩家输入原话。
- 归类是二分的（人物 / 事件），没有「地点」「物品」两个类别。
- 预演不产生叙事，也不写事件表：它是「会发生什么」的答案，不是一次回合。
- `plan` 的候选上限 40，超出后截断，不做分页。
- 前端仍未通过真实 `vite build`（本沙箱的 DSH 预览钩子会让 vite 报 `Cannot find module .dsh-tmp/config-module.mjs`），只做了 esbuild 语法检查。

## 七、本地验证

```bash
cd backend
TEMP='E:/shujia/.tmp' TMP='E:/shujia/.tmp' .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider \
  tests/services/test_knowledge_extractor.py tests/api/test_routes_knowledge_extract.py \
  tests/services/test_interaction_planner.py tests/api/test_routes_planner.py
```

手工走一遍：

1. 启动后创建一个世界（或选一个起步预设），绑定战役。
2. 世界工作台 → 调试 → 交互预演 → 推演当前地点。
3. 点「为什么」查看缺什么；再进游戏拾取钥匙，回来后重新推演，门应从被拒变为可行。
4. 世界工作台 → 记忆提炼 → 只预览 → 运行，确认没有任何条目被写入；取消勾选再运行一次才落盘。
