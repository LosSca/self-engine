# 阶段 14：让叙事真的读起来是这个世界

本阶段是需求 ⑤ 的收尾，也是验收第 8 条（LLM 依据已确认结果生成叙事）的落实。

## 1. 发现的问题

用一条纯中文件跑完整流程时，`narrative.text` 是这样的：

```
You arrive at Forest Edge.
```

地点叫「林间小路」，叙事却是英文。而且更糟的是中文世界配模板叙事器时，整句是 `You arrive at ...` 之外的场景描述——因为 `TemplateNarrator` 把 `outcome.message` 与 `describe_effects()` 拼起来，而这两处硬编码英文。

根因有三层：

1. `WorldDefinition` 根本没有语言字段，世界文档不知道自己是用什么语言写的；
2. `describe_effects` / `render_fallback` / `TemplateNarrator` 全部硬编码英文句子；
3. `LLMNarrator` 的语言只来自 `options['language']`（场景预设），世界本身说得上话的地方没有。

这不是本地化细节：验收第 8 条要求叙事描述的是「已经确认的结果」，而玩家读到的句子如果和世界语言不符，就等于在描述另一个世界。

## 2. 改动

### 2.1 世界文档带语言

`backend/app/models/game.py` 的 `WorldDefinition` 新增 `language: str = Field(default="", max_length=16)`。它是**描述语言**，规则引擎从不读它；留空表示「未声明」，一切都维持原来的英文行为。

`backend/app/services/starter_worlds.py` 的 `build_world_document` 在落地时写入 `"zh"` 或 `"en"`，所以起步世界自己就带着语言，不需要调用方再传一次。

### 2.2 确定性句子按语言选择

`backend/app/services/narrative_service.py` 新增 `_localise(world, english, chinese)`：世界语言以 `zh` 开头时返回中文，`world is None` 时返回英文（`render_fallback` 仍会被旧的对局通道调用，那条路径没有世界文档）。

`describe_effects` 的六条句子（拾取/失去/移动/开门/关门/置标记）、`render_fallback` 的失败兜底、`TemplateNarrator` 的任务完成句与中文世界的连接符（中文空格拼接读起来是断的，改成直接相连）都走这个选择。

### 2.3 语言交给模型

`backend/app/services/action_service.py` 新增 `_language_option(world, options)`：`options` 里没有 `language` 时用世界语言补上，已有时保持预设优先。`_narrate_options` 增加 `world` 参数并在三处调用点传入，所以 `LLMNarrator` 的 system prompt 里那句 `Reply in ...` 现在会说对语言。

## 3. 顺带堵上的一个漏洞

`LLMNarrator` 原来只剥 `_CONTROL_TAG`（`ITEM_ADD/USE/LOSE/REMOVE/GIVE` 的通用形态）。旧通道 `backend/app/services/game_session.py` 解析的是 `[ITEM_ADD:name|category|source]` 这种带冒号和竖线的形态，`_CONTROL_TAG` 的 `[^]]*` 其实覆盖得到，但两处正则各自演化，一旦有一处放宽就会漏。

新增 `strip_prose_tags(text)`，用与旧通道一致的 `_PROSE_TAG_NAMES` 剥掉这些标记；`LLMNarrator` 在 `_CONTROL_TAG` 之后再过一遍，`TemplateNarrator` 的最终文本也过一遍。理由：**引擎回合的散文里出现任何控制标记，都等于允许模型间接改状态**，这是 `backend/app/models/game.py` 之外没人守的地方。

## 4. 测试

- `backend/tests/services/test_narrative_service.py` +3 项：
  - `test_template_narrator_speaks_the_world_language`：中文世界 → `你来到林间小路。`，且不含 `You arrive`。
  - `test_engine_hands_the_world_language_to_the_model`：`_language_option` 用世界语言补齐、预设优先、未声明语言时返回 `{}`。
  - `test_engine_prose_never_carries_a_legacy_item_tag`：`strip_prose_tags` 剥标记但保留普通括号旁白；叙事器输出里的 `[ITEM_LOSE:iron_key]` 被清掉。
- `backend/tests/api/test_acceptance_story.py` +1 项：`test_acceptance_chinese_world_reads_in_chinese_even_without_a_model` —— 中文世界经完整 HTTP 行动链路，模板叙事器给出的移动句是中文。

全量后端：**1451 passed, 8 skipped**（130s）。

## 5. 未做

- 只覆盖中英两种；`_localise` 的签名是二选一，要加第三种语言得改成映射。
- 世界语言默认留空（英文），没有做「从未声明的旧存档推断语言」——推断错了比不做更糟。
- 前端编辑器没有暴露 `world.language` 字段，目前由起步世界与剧本生成自动带上。

## 6. 本地验证

```bash
cd backend
TEMP='E:/shujia/.tmp' TMP='E:/shujia/.tmp' .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider \
  tests/services/test_narrative_service.py tests/api/test_acceptance_story.py
```

手工验证：创建一个中文场景（`world_type` 任意），不配置任何供应商密钥，绑定起步世界后执行「移动」，返回的 `narrative.text` 里应出现中文地点名与中文句子。
