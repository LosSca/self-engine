# 阶段 9：世界穿越器与作品角色库（需求 ② ⑦）

## 结论

`POST /api/v1/works/enter` 用**策展档案优先、模型兜底**的方式，把一部外部作品变成一张连通的初始世界，
并把选中的角色写进战役的角色位。作品档案（地点、角色、基调、开场提示）是确定性数据，
所以整条链路可以脱机测试，不依赖模型可用性。

## 为什么不是「联网搜索」

原始需求写的是「联网搜索作品生成世界和对应的故事线」。本轮没有接入真实网页搜索，原因有二：

1. 运行环境出网受限（`github.com` / `raw.githubusercontent.com` 连接被重置，只有部分 CDN 主机可达），
   搜索链路无法在本机稳定验证，写进去就是不可测试的代码。
2. 模型对具体作品的记忆不稳定，同一部作品两次生成的设定会打架。

因此第一版用**策展档案**（`backend/app/services/work_catalogue.py` 里 6 部作品的固定档案）保证确定性，
`allow_model=true` 时才回落到模型生成。接口与数据形状不依赖搜索，将来加搜索只是多一个档案来源。

版权边界：只收录文字档案（名称、角色、地点、基调），不抓取、不分发受版权保护的图片或正文。

## 数据模型（backend/app/models/work.py）

| 模型 | 字段 |
| --- | --- |
| `CharacterProfile` | `name` / `role` / `description` / `traits` / `source_work` |
| `WorkProfile` | `title` / `medium` / `summary` / `tone` / `characters` / `locations` / `location_notes` / `opening_hint` / `source` |

两者都是 `extra="forbid"` 的严格模型。`source` 区分 `curated`（库内档案）与 `generated`（模型产出），
这样前端和调试面板能一眼看出这份设定是确定的还是模型编的。

## 服务层

- `backend/app/services/work_catalogue.py`
  - `catalogue_keys()` / `find_work(title)`：先精确 key，再关键词包含匹配。
  - `key_for(title)`：稳定 ASCII key。库内命中返回条目 key（`福尔摩斯` → `sherlock_holmes`），
    未命中才退到 `slug_of()`。**纯中文且不在库里的标题会退化成 `entry`，调用方必须自己去重**，
    这是有意的：世界 id 不该随语言漂移。
  - `profile_from_payload(payload, title, source)`：校验模型输出，补全 `locations`/`location_notes` 默认值；
    非 dict、标题为空、`characters` 非列表、没有可用角色都抛 `ValueError`（中文可读信息）。
  - `seed_world_definition(profile, character, world_id)`：生成**链式连通**骨架——每个地点连前后邻居、双向出口、
    label 为「返回 X / 前往 Y」，起点放一个 `kind="npc"` 的实体。六部作品全部通过 `world_service.validate_world`。
- `backend/app/services/work_generator.py`
  - `generate_work(router, title, language)` 走 `ai_generator._ask`，提示词要求纯 JSON、至少 3 角色 3 地点、不写数值与机制。
  - `resolve_work(router, title, language)`：策展优先，未命中才调模型。

## API（backend/app/api/routes_works.py，挂在 `/api/v1`）

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/api/v1/works` | 列出全部策展作品 |
| GET | `/api/v1/works/{title}` | 只查策展，未命中 404（不花模型调用） |
| POST | `/api/v1/works/resolve` | 策展优先；`allow_model=false` 且未命中时 404 |
| POST | `/api/v1/works/enter` | 校验角色名 → 生成并保存世界 → 可选写入战役角色位 |

`enter` 的细节：

- 角色名不在档案里返回 400，`detail` 里列出「可选：…」，玩家不用猜。
- `world_id` 默认 `crossover_{key_for(title)}`，可用 `world_id` 覆盖（重复进入同一作品会覆盖同一张世界，而不是堆一堆副本）。
- 给了 `campaign_id` 时通过 `scenario_store.update_campaign_character` 写入 `character_pick`；
  读回走既有的 `GET /api/campaigns/{campaign_id}/character`（不在本模块另开一套）。

路由总数 121 → 125。

## 前端（WorldStudio「作品穿越」标签页）

- `frontend/src/api.js` 新增 `fetchWorks` / `resolveWork` / `enterWork`。
- `frontend/src/components/WorldStudio.jsx` 新增 `works` 标签：作品下拉、角色下拉、作品卡片（梗概 / 基调 / 地点 / 角色）、
  「生成世界并进入」按钮。进入后自动刷新世界列表并跳到新世界，notice 里写出世界名、地点数与角色位。

## 测试

- `backend/tests/api/test_routes_works.py` 9 项：列表、策展命中、未知作品 404、`allow_model=false` 不花模型调用、
  resolve 命中策展、enter 保存的图连通、enter 拒绝未知角色、enter 写入战役角色位。
- `backend/tests/services/test_work_catalogue.py` 6 项：六部作品都能过 `validate_world`、种子世界全连通、
  `key_for` 优先库内 key、`slug_of` 非 ASCII 退化、模型输出校验、模型输出补全默认地点。
- `backend/tests/api/test_routes_contracts.py` 增加 4 条 typed 路径。

全量后端：**1406 passed, 8 skipped**。

前端：`npx vite build` 在本沙箱不可用（DSH 预览钩子让 `vite.config.js` 找不到 `.dsh-tmp/config-module.mjs`），
本轮用 esbuild 逐个 transform 了改动文件（`App.jsx` / `api.js` / `WorldStudio.jsx`）确认语法与 JSX 正确。
**前端仍未真正过构建，这是已知缺口。**

## 未完成 / 后续

- 没有真实网页搜索；下一步可以把 `resolve_work` 的 "未命中" 分支换成一个 search adapter。
- 作品库只有 6 部，扩充是纯数据工作。
- 「世界穿越器」目前是「生成一张新世界」，还不能把角色带进**已有**战役；跨世界携带物品/记忆留待后续。
