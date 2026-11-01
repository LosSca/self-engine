/**
 * Authored map/world presets for the world studio.
 *
 * Each entry is a complete WorldDefinition draft: regions and grid
 * coordinates are included so the map editor starts from a real layout, and
 * every reference (exits, keys, quests, events) is valid for validation.
 */
export const WORLD_TEMPLATES = [
  {
    key: 'orbital-station',
    name: '轨道站事故',
    summary: '失控的轨道站：反应堆、医疗舱和一把主管钥匙卡。',
    build: (scenarioId, worldId) => ({
      world_id: worldId,
      scenario_id: scenarioId || '',
      name: '轨道站事故',
      description: '一座失去联络的轨道站，走廊里只有应急灯和冷却液的滴答声。',
      start_location_id: 'dock',
      starting_skills: { engineering: 1 },
      locations: [
        {
          id: 'dock', name: '对接舱', description: '气闸尚未完全闭合，舷窗外是缓慢旋转的星球。',
          region: 'station', x: 0, y: 1, tags: ['入口'],
          exits: [{ target_id: 'corridor', label: '内舱' }],
          door_ids: ['dock_hatch'], item_ids: ['keycard'], npc_ids: [],
        },
        {
          id: 'corridor', name: '环形走廊', description: '应急灯每隔三秒闪一次，墙上贴着烧焦的疏散图。',
          region: 'station', x: 1, y: 1, tags: ['通道'],
          exits: [{ target_id: 'dock', label: '气闸' }, { target_id: 'reactor', label: '反应堆' }, { target_id: 'medbay', label: '医疗舱' }],
          door_ids: [], item_ids: [], npc_ids: ['engineer'],
        },
        {
          id: 'reactor', name: '反应堆', description: '冷却管发出低鸣，控制台上有一枚红色的手动锁。',
          region: 'core', x: 2, y: 0, tags: ['危险'],
          exits: [{ target_id: 'corridor', label: '走廊' }],
          door_ids: ['reactor_door'], item_ids: ['coolant_cell'], npc_ids: [],
        },
        {
          id: 'medbay', name: '医疗舱', description: '药柜被撬开，地上散落着空的注射器。',
          region: 'station', x: 2, y: 2, tags: ['室内'],
          exits: [{ target_id: 'corridor', label: '走廊' }],
          door_ids: [], item_ids: ['medkit'], npc_ids: [],
        },
      ],
      items: [
        { id: 'keycard', name: '主管钥匙卡', description: '边角被烧黑，仍能刷开反应堆门。', tags: ['钥匙'], stackable: false },
        { id: 'coolant_cell', name: '冷却单元', description: '沉重的手提式冷却单元。', tags: ['任务'], stackable: false },
        { id: 'medkit', name: '医疗包', description: '还能用一次。', tags: ['消耗品'], stackable: true, consumable: true, use_effects: [{ kind: 'set_flag', target: 'healed', value: true }] },
        { id: 'ration', name: '压缩口粮', description: '味道像纸板。', tags: ['补给'], stackable: true, price: 4 },
      ],
      doors: [
        { id: 'dock_hatch', name: '气闸', location_id: 'dock', locked: false, opened: true },
        { id: 'reactor_door', name: '反应堆门', location_id: 'reactor', locked: true, opened: false, key_item_id: 'keycard' },
      ],
      entities: [
        { id: 'engineer', name: '工程师艾拉', description: '手一直按在耳后的通讯器上。', location_id: 'corridor', kind: 'npc', faction: 'crew', dialogue: ['别碰反应堆。', '钥匙卡在对接舱，自己拿。'], hostile: false, attributes: {}, items: [] },
        { id: 'med_locker', name: '药品柜', description: '被撬开的柜子。', location_id: 'medbay', kind: 'object', dialogue: [], hostile: false, attributes: {}, items: ['medkit'], shop_items: [] },
      ],
      quests: [
        {
          id: 'stabilize_reactor', title: '稳定反应堆', description: '把冷却单元送进反应堆。',
          objectives: [{ id: 'coolant', kind: 'obtain_item', target: 'coolant_cell', required_count: 1, description: '找到冷却单元' }],
          rewards: [{ kind: 'set_flag', target: 'station_safe', value: true }], enabled: true, auto_accept: true,
        },
      ],
      events: [
        { id: 'reactor_alarm', name: '反应堆警报', trigger: 'state_change', repeat: 'once', conditions: [{ kind: 'has_item', target: 'coolant_cell', op: 'eq', value: true }], effects: [{ kind: 'adjust_alert', target: 'engineer', amount: 1 }], followup_events: [], failure_events: [] },
      ],
      custom_actions: [],
    }),
  },
  {
    key: 'fog-town',
    name: '雾镇档案',
    summary: '封闭小镇、失踪档案与上锁的地窖。',
    build: (scenarioId, worldId) => ({
      world_id: worldId,
      scenario_id: scenarioId || '',
      name: '雾镇档案',
      description: '雾从河面爬进街道，镇上的人都说档案室从来没有钥匙。',
      start_location_id: 'square',
      locations: [
        {
          id: 'square', name: '镇广场', description: '雾里只剩一盏路灯亮着。', region: 'town', x: 1, y: 1,
          exits: [{ target_id: 'inn', label: '旅店' }, { target_id: 'archive', label: '档案室' }],
          item_ids: [], npc_ids: ['innkeeper'], door_ids: [],
        },
        {
          id: 'inn', name: '旅店', description: '壁炉里的火快熄了，柜台上压着一张旧报纸。', region: 'town', x: 0, y: 2,
          exits: [{ target_id: 'square', label: '广场' }],
          item_ids: ['brass_key'], npc_ids: ['innkeeper'], door_ids: [],
        },
        {
          id: 'archive', name: '档案室', description: '书架顶到天花板，最里面有一扇通往地窖的门。', region: 'archive', x: 2, y: 1,
          exits: [{ target_id: 'square', label: '广场' }, { target_id: 'cellar', label: '地窖' }],
          item_ids: [], npc_ids: [], door_ids: ['cellar_door'],
        },
        {
          id: 'cellar', name: '地窖', description: '潮湿的石阶尽头，一只铁箱压在旧档案上。', region: 'archive', x: 3, y: 1,
          exits: [{ target_id: 'archive', label: '档案室' }],
          item_ids: ['missing_file'], npc_ids: [], door_ids: [],
        },
      ],
      items: [
        { id: 'brass_key', name: '黄铜钥匙', description: '钥匙柄上刻着地窖的编号。', tags: ['钥匙'], stackable: false },
        { id: 'missing_file', name: '失踪档案', description: '记录着十年前最后一班渡船。', tags: ['线索'], stackable: false },
        { id: 'lantern', name: '提灯', description: '还能烧一个晚上。', tags: ['工具'], stackable: true, price: 5 },
      ],
      doors: [{ id: 'cellar_door', name: '地窖门', location_id: 'archive', locked: true, opened: false, key_item_id: 'brass_key' }],
      entities: [
        { id: 'innkeeper', name: '旅店老板', description: '擦杯子的手一直没停。', location_id: 'inn', kind: 'npc', faction: 'town', dialogue: ['十年前的渡船？没人记得。', '地窖的钥匙？我从来没拿过。'], hostile: false, attributes: {}, items: ['brass_key'] },
        { id: 'archive_shelf', name: '档案架', description: '落满灰尘的卷宗。', location_id: 'archive', kind: 'object', dialogue: [], hostile: false, attributes: {}, items: [], shop_items: [] },
      ],
      quests: [
        { id: 'find_file', title: '找回失踪档案', description: '打开地窖，把档案带回镇上。', objectives: [{ id: 'file', kind: 'obtain_item', target: 'missing_file', required_count: 1, description: '找到失踪档案' }], rewards: [{ kind: 'set_flag', target: 'truth_found', value: true }], enabled: true, auto_accept: true },
      ],
      events: [],
      custom_actions: [],
    }),
  },
  {
    key: 'wild-camp',
    name: '荒野营地',
    summary: '河谷、遗迹和洞窟：一顶帐篷、一只野兽、一条生路。',
    build: (scenarioId, worldId) => ({
      world_id: worldId,
      scenario_id: scenarioId || '',
      name: '荒野营地',
      description: '补给只够两天，河谷对岸的遗迹在雨里发黑。',
      start_location_id: 'camp',
      starting_skills: { survival: 2 },
      locations: [
        {
          id: 'camp', name: '营地', description: '帐篷被风吹得猎猎作响，篝火剩下一点红。', region: 'river', x: 0, y: 1,
          exits: [{ target_id: 'river', label: '河岸' }], item_ids: ['rope'], npc_ids: [], door_ids: [],
        },
        {
          id: 'river', name: '河岸', description: '水声很大，浅滩上有一串新鲜的爪印。', region: 'river', x: 1, y: 1,
          exits: [{ target_id: 'camp', label: '营地' }, { target_id: 'ruins', label: '遗迹' }, { target_id: 'cave', label: '洞窟' }],
          item_ids: ['herb'], npc_ids: ['wolf'], door_ids: [],
        },
        {
          id: 'ruins', name: '遗迹', description: '断裂的石柱间长满青苔。', region: 'ruins', x: 2, y: 0,
          exits: [{ target_id: 'river', label: '河岸' }], item_ids: ['relic'], npc_ids: [], door_ids: [],
        },
        {
          id: 'cave', name: '洞窟', description: '越往里越冷，地上有拖拽的痕迹。', region: 'ruins', x: 2, y: 2,
          exits: [{ target_id: 'river', label: '河岸' }], item_ids: ['supply_cache'], npc_ids: [], door_ids: [],
        },
      ],
      items: [
        { id: 'rope', name: '绳索', description: '还能承受一个人的重量。', tags: ['工具'], stackable: true, price: 3 },
        { id: 'herb', name: '止血草', description: '嚼碎后敷在伤口上。', tags: ['草药'], stackable: true, consumable: true, use_effects: [{ kind: 'set_flag', target: 'healed', value: true }] },
        { id: 'relic', name: '遗迹碎片', description: '刻着看不懂的符号。', tags: ['宝物'], stackable: false, price: 15 },
        { id: 'supply_cache', name: '补给箱', description: '上一支队伍留下的补给。', tags: ['补给'], stackable: true },
      ],
      doors: [],
      entities: [
        { id: 'wolf', name: '灰狼', description: '瘦得肋骨分明，却没有退。', location_id: 'river', kind: 'creature', faction: 'wild', dialogue: [], hostile: true, attributes: { max_hp: 16, attack: 4, defense: 1 }, items: [] },
        { id: 'cave_cache', name: '补给箱', description: '半埋在土里的箱子。', location_id: 'cave', kind: 'object', dialogue: [], hostile: false, attributes: {}, items: ['supply_cache'], shop_items: [] },
      ],
      quests: [
        { id: 'survive_night', title: '熬过今夜', description: '找到补给，或者找到离开河谷的路。', objectives: [{ id: 'relic', kind: 'obtain_item', target: 'relic', required_count: 1, description: '找到遗迹碎片' }], rewards: [{ kind: 'add_xp', amount: 60 }], enabled: true, auto_accept: true },
      ],
      events: [
        { id: 'wolf_alert', name: '狼的警戒', trigger: 'state_change', repeat: 'once', conditions: [{ kind: 'entity_alert', target: 'wolf', op: 'gte', count: 1 }], effects: [{ kind: 'set_flag', target: 'wolf_angry', value: true }], followup_events: [], failure_events: [] },
      ],
      custom_actions: [],
    }),
  },
]
