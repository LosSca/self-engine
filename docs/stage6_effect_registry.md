# 效果注册表：apply_effect 从分支链变成查表

## 问题

`backend/app/engines/event_engine.py` 的 `apply_effect()` 曾经是一个 431 行的分支链：每加一种效果就在中间插一段 `if kind == "..."`，改一行要看完整段，且没有任何机制保证"模型里声明的效果"和"引擎能应用的效果"一致——少写一条只会安静地落到 `unsupported effect kind`。

## 现在

`apply_effect()` 只剩 13 行，是一次查表：

```python
kind = effect.kind
handler = EFFECT_HANDLERS.get(kind)
if handler is None:
    return AppliedEffect(effect, False, f"unsupported effect kind '{kind}'")
return handler(kind, state, world, effect)
```

每种效果的实现是一个独立函数，用装饰器登记：

```python
@register_effect("give_item")
def _effect_give_item(kind, state, world, effect) -> AppliedEffect:
    ...
```

- `EFFECT_HANDLERS: Dict[str, _EffectHandler]` —— 唯一的派发表。
- `register_effect(*kinds)` —— 一个函数可以登记多个种类（`add_currency`/`remove_currency` 共用一个实现）；重复登记直接抛 `ValueError`，不会静默覆盖。
- `effect_kinds()` —— 这一版能应用的全部效果种类，供调试与自省使用。

28 个种类 → 25 个处理函数（货币、关系三兄弟各自合并）。函数体是从原分支逐行搬过来的，**只是去掉了一层缩进**，行为没有任何改动。

## 为什么这是安全性改进，不只是整洁

- **对齐可测**：`Effect.kind` 的 Literal 声明与 `EFFECT_HANDLERS` 现在有测试断言完全相等 —— 少一个、多一个都会红。
- **失败仍然关闭**：未知种类依旧返回 `ok=False` 的可读原因，绝不猜测。
- **不会再长出分支链**：新增效果只需写函数 + 装饰器，`apply_effect` 的函数体被测试锁死（`assert 'if kind ==' not in source`）。
- **事件执行器无需改动**：`run_triggered_events` 仍在临时副本上逐个应用，任一失败即放弃整次触发。

## 测试

`backend/tests/engines/test_effect_registry.py`（7 条）：

1. 每个声明的效果种类都有处理函数（集合相等）。
2. `effect_kinds()` 有序且完整。
3. `apply_effect` 是纯派发，不含任何 `if kind` 分支。
4. 注册表被清空时，未知种类失败关闭并给出 `unsupported effect kind 'set_flag'`。
5. `register_effect` 拒绝重复登记，并可在测试中清理。
6. 合并实现仍能靠 `kind` 区分兄弟种类。
7. 关键种类（give_item / quest_complete / emit）在新进程里依然存在。

全量后端：**1304 passed, 8 skipped**（120s）。

## 后续

- 其它巨型函数可以照此拆分：`models/game.py` 的 `validate_references`（306 行）、`services/game_session.py` 的 `_process_action_single_call`（459 行）。
- `conditions` 一侧同样是分支链（`condition_holds`），可以按同一模式做成 `CONDITION_HANDLERS`。
