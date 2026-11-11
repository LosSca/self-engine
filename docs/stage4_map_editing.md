# 阶段 4：地图编辑（玩家视角 + 坐标持久化）

## 调查结论

`frontend/src/components/WorldMapGraph.jsx` **原本就是可视化编辑器**，不需要重写：

- SVG 手绘地图：区域 blob（`blobPath`）、网格/等高线底纹、缩放平移（`view{x,y,scale}`）、拖拽节点、点选节点与连线、双击编辑地点、删除出口；
- `layoutNodes` 用 BFS 分层算默认位置，地点显式写了 `x/y` 时用 `PAD + x*CELL_X` 定位（PAD=56、CELL_X=220、CELL_Y=150、NODE_W=176、NODE_H=74）。

真正的三个缺口：

1. 拖动后的坐标只写进 `localStorage`（key `lunar.map.positions.<worldId>`），换机器/清缓存就丢；
2. 编辑器地图看不到「玩家现在在哪」；
3. 没有进行中的冒险时，地图无从预览某个存档。

## 改动

### 1. 拖拽即持久化（WorldMapGraph.jsx）

`endInteraction` 里，节点被真正拖动（`drag.moved`）时：

- 仍然写 localStorage（作为即时预览）；
- 同时调用 `onSavePosition(drag.id, {x, y})`，把像素坐标换算成格子坐标写进世界文档。

`WorldStudio.saveLocationPosition` 增加「没变化就不写」的短路：拖动几像素不会去写世界、也不会刷提示。

localStorage 由此降级为「本地预览缓存」，权威位置在世界文档里。

### 2. 玩家标记（WorldMapGraph.jsx + WorldStudio.jsx）

- `WorldStudio` 读取 `fetchCampaigns(scenarioId)`，在地图页顶部提供「游玩进度」下拉：列出绑定过这个场景的冒险（`player_name`），若当前正在进行的那场不在列表里就补一条「当前冒险」；没有冒险时显示「（这个场景还没有冒险）」。
- 选中的冒险 `campaignId` 传给 `<WorldMapGraph campaignId={...} />`。
- `WorldMapGraph` 用 `fetchCampaignEngineState(campaignId)` 读权威状态，取 `location_id` 与 `version`，在地图标题栏显示「● 玩家：村庄 · v3」，并在对应地点节点上叠加青色脉冲环 + `P` 徽标。
- 这个读取是**只读**的：地图不写状态，也不推进回合。

## 边界

- 玩家标记只来自已提交的状态（`GameState.location_id`），不是模型说的、也不是前端攒的。
- 地图仍不允许在图上「边拖边建地点」——建/删地点走现有编辑表单，避免一次改动过大。

## 验证

- 前端构建：`npm run build` → `✓ built in 1.98s`，WorldStudio 91.93 kB / gzip 25.07，GameCanvas 84.03 kB / gzip 22.32。
- 纯前端改动，后端测试不受影响（上一次全量：1278 passed, 8 skipped）。

## 本地验证方式

1. `start.bat` 启动 → 进入某场冒险 → 打开世界工作室 → 地图页；
2. 「游玩进度」下拉选该冒险，地图上应出现青色玩家标记与「玩家：<地点> · vN」；
3. 拖动一个地点节点，松手后提示「已写入坐标」，刷新页面位置仍在（不再是仅 localStorage）。
