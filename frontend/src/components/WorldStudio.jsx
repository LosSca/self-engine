import { useCallback, useEffect, useState } from 'react'
import {
  createWorld,
  deleteWorldDoor,
  deleteWorldEntity,
  deleteWorldEvent,
  deleteWorldItem,
  deleteWorldLocation,
  deleteWorldQuest,
  deleteWorldCustomAction,
  saveWorldCustomAction,
  fetchActionTypes,
  fetchCampaignDiagnostics,
  fetchEventExecutions,
  fetchQuestProgress,
  fetchWorld,
  fetchWorldMap,
  fetchWorlds,
  saveWorldDoor,
  saveWorldEntity,
  saveWorldEvent,
  saveWorldItem,
  saveWorldLocation,
  saveWorldQuest,
  simulateWorldEvent,
  validateWorldEvent,
  bindCampaignWorld,
  fetchCampaignEngineState,
  fetchCampaignSnapshots,
  fetchCampaignStateDiff,
  rollbackCampaign,
  fetchActionDetail,
} from '../api'
import WorldMapGraph from './WorldMapGraph'

const CONDITION_KINDS = [
  ['flag', '剧情标记'],
  ['has_item', '持有物品'],
  ['item_count', '物品数量'],
  ['door_open', '门已开启'],
  ['at_location', '位于地点'],
  ['quest_status', '任务状态'],
  ['entity_at', '实体位于地点'],
  ['entity_alive', '实体存活'],
  ['combatant_hp', '实体生命值'],
]

const OPS = [
  ['eq', '等于'],
  ['ne', '不等于'],
  ['gte', '大于等于'],
  ['lte', '小于等于'],
  ['gt', '大于'],
  ['lt', '小于'],
]

const EFFECT_KINDS = [
  ['set_flag', '设置标记'],
  ['give_item', '给予物品'],
  ['remove_item', '移除物品'],
  ['move_player', '移动玩家'],
  ['move_entity', '移动实体'],
  ['open_door', '开门'],
  ['close_door', '关门'],
  ['quest_progress', '推进任务'],
  ['quest_complete', '完成任务'],
  ['combat_hp', '设置实体生命值'],
  ['player_damage', '玩家受伤'],
  ['emit', '发出信号'],
]

const OBJECTIVE_KINDS = [
  ['obtain_item', '获得物品'],
  ['open_door', '打开门'],
  ['reach_location', '抵达地点'],
  ['flag', '剧情标记'],
]

const ENTITY_KINDS = [
  ['npc', 'NPC'],
  ['creature', '生物'],
  ['object', '可交互物件'],
]

const TRIGGERS = [
  ['action', '行动触发'],
  ['state_change', '状态变化触发'],
]

const REPEATS = [
  ['once', '只执行一次'],
  ['always', '每次满足都执行'],
]

const TABS = [
  ['map', '世界地图'],
  ['items', '物品'],
  ['doors', '门'],
  ['entities', '实体'],
  ['events', '事件'],
  ['quests', '任务'],
  ['actions', '行动'],
  ['debug', '调试'],
]

const inputClass =
  'w-full bg-white/5 border border-white/10 rounded-xl px-3 py-2 text-sm text-white placeholder:text-white/20 focus:outline-none focus:border-white/30'
const tinyInput =
  'bg-white/5 border border-white/10 rounded-lg px-2 py-1 text-xs text-white placeholder:text-white/20 focus:outline-none focus:border-white/30'
const primaryButton =
  'px-5 py-2 rounded-xl bg-white text-black text-xs font-bold tracking-[0.15em] hover:bg-gray-200 disabled:opacity-50'
const ghostButton =
  'px-4 py-2 rounded-xl border border-white/10 text-white/60 text-xs hover:border-white/30 hover:text-white'
const dangerButton = 'text-[10px] text-white/30 hover:text-rose-400 tracking-[0.15em] uppercase'

export function splitList(text) {
  return String(text)
    .split(',')
    .map((value) => value.trim())
    .filter(Boolean)
}

export function parseValue(text) {
  const trimmed = String(text).trim()
  if (trimmed === 'true') return true
  if (trimmed === 'false') return false
  if (trimmed !== '' && !Number.isNaN(Number(trimmed))) return Number(trimmed)
  return trimmed
}

export function showValue(value) {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

function FieldLabel({ children }) {
  return (
    <span className="block text-[10px] font-mono uppercase tracking-[0.2em] text-white/40 mb-2">
      {children}
    </span>
  )
}

function TabButton({ active, children, onClick }) {
  return (
    <button
      onClick={onClick}
      className={
        'px-4 py-2 rounded-xl text-xs font-bold tracking-[0.15em] transition-all border ' +
        (active
          ? 'bg-white text-black border-white'
          : 'bg-white/5 text-white/60 border-white/5 hover:border-white/20 hover:text-white')
      }
    >
      {children}
    </button>
  )
}

function SectionNote({ children }) {
  return <p className="text-xs text-white/40 leading-relaxed max-w-3xl">{children}</p>
}

function EmptyHint({ children }) {
  return (
    <div className="rounded-2xl border border-dashed border-white/10 py-10 text-center text-white/30 text-xs">
      {children}
    </div>
  )
}

function Badge({ children, tone = 'muted' }) {
  const tones = {
    muted: 'border-white/10 text-white/40',
    accent: 'border-white/30 text-white/70',
    warn: 'border-amber-400/40 text-amber-300',
    bad: 'border-rose-400/40 text-rose-300',
    good: 'border-emerald-400/40 text-emerald-300',
  }
  return (
    <span
      className={
        'text-[10px] font-mono px-2 py-0.5 rounded-full border whitespace-nowrap ' +
        (tones[tone] || tones.muted)
      }
    >
      {children}
    </span>
  )
}

function demoWorldTemplate(scenarioId, worldId) {
  return {
    world_id: worldId,
    scenario_id: scenarioId || '',
    name: '村庄与上锁的仓库',
    description: '一个最小可玩世界：村庄广场、森林边缘、上锁的仓库，以及打开仓库所需的钥匙。',
    start_location_id: 'village_square',
    locations: [
      {
        id: 'village_square',
        name: '村庄广场',
        description: '夯土广场被低矮的石屋围住，东边的仓库门紧锁着。',
        tags: ['村镇'],
        exits: [
          { target_id: 'forest_edge', label: '北', door_id: null, requires_item_id: null },
          { target_id: 'warehouse', label: '东', door_id: 'warehouse_door', requires_item_id: null },
        ],
        door_ids: ['warehouse_door'],
        item_ids: ['iron_key'],
        npc_ids: ['elder_mara'],
      },
      {
        id: 'forest_edge',
        name: '森林边缘',
        description: '松树沿着北面的土路挤成一片，田地到这里就到头了。',
        tags: ['野外'],
        exits: [{ target_id: 'village_square', label: '南', door_id: null, requires_item_id: null }],
        door_ids: [],
        item_ids: [],
        npc_ids: [],
      },
      {
        id: 'warehouse',
        name: '仓库',
        description: '成袋的谷物堆到房梁，灰尘的味道在光柱里浮动。',
        tags: ['室内'],
        exits: [
          { target_id: 'village_square', label: '西', door_id: 'warehouse_door', requires_item_id: null },
        ],
        door_ids: [],
        item_ids: ['silver_charm'],
        npc_ids: [],
      },
    ],
    items: [
      { id: 'iron_key', name: '铁钥匙', description: '一把沉重的铁钥匙，柄上刻着磨损的绳结纹。', tags: ['钥匙'], stackable: false },
      { id: 'silver_charm', name: '银吊坠', description: '一枚小小的银片，摸上去冰凉。', tags: ['宝物'], stackable: false },
      { id: 'blessing_token', name: '祝福木牌', description: '村长付给老实人的报酬。', tags: ['奖励'], stackable: true },
    ],
    doors: [
      {
        id: 'warehouse_door',
        name: '仓库门',
        location_id: 'village_square',
        locked: true,
        opened: false,
        key_item_id: 'iron_key',
        consumes_key: false,
      },
    ],
    entities: [
      {
        id: 'elder_mara',
        name: '村长玛拉',
        description: '把每一袋谷物都数两遍的老妇人。',
        tags: ['村长'],
        location_id: 'village_square',
        kind: 'npc',
        dialogue: ['天黑之前把谷物数清楚，旅人。', '东边那扇门，钥匙在我手里放得太久了。'],
        hostile: false,
        attributes: {},
      },
    ],
    quests: [
      {
        id: 'open_the_warehouse',
        title: '打开仓库',
        description: '村长希望在天黑前清点完谷物。',
        enabled: true,
        objectives: [
          { id: 'find_key', kind: 'obtain_item', target: 'iron_key', required_count: 1, description: '找到铁钥匙' },
          { id: 'open_door', kind: 'open_door', target: 'warehouse_door', required_count: 1, description: '打开仓库门' },
        ],
        rewards: [{ kind: 'give_item', target: 'blessing_token', value: null, amount: 1 }],
      },
    ],
    events: [
      {
        id: 'keeper_alert',
        name: '村长抬起头',
        description: '仓库门终于被推开时，村长转过身来。',
        enabled: true,
        trigger: 'action',
        trigger_action: 'open',
        trigger_target: 'warehouse_door',
        conditions: [{ kind: 'door_open', target: 'warehouse_door', op: 'eq', value: true }],
        effects: [{ kind: 'set_flag', target: 'warehouse_opened', value: true, amount: 1 }],
        priority: 10,
        repeat: 'once',
      },
    ],
  }
}
function RowShell({ children, onRemove }) {
  return (
    <div className="flex flex-wrap items-end gap-2 rounded-xl border border-white/10 bg-white/[0.02] p-2">
      {children}
      {onRemove && (
        <button onClick={onRemove} className={dangerButton + ' pb-2'}>
          移除
        </button>
      )}
    </div>
  )
}

function ExitRows({ world, value, onChange }) {
  const update = (index, patch) =>
    onChange(value.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  return (
    <div className="flex flex-col gap-2">
      {value.map((row, index) => (
        <RowShell key={index} onRemove={() => onChange(value.filter((_, i) => i !== index))}>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] text-white/30">去往地点</span>
            <select className={tinyInput} value={row.target_id || ''} onChange={(e) => update(index, { target_id: e.target.value })}>
              <option value="">选择地点</option>
              {world.locations.map((location) => (
                <option key={location.id} value={location.id}>
                  {location.name}（{location.id}）
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] text-white/30">方位</span>
            <input className={tinyInput + ' w-20'} value={row.label || ''} onChange={(e) => update(index, { label: e.target.value })} placeholder="北" />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] text-white/30">经过的门</span>
            <select className={tinyInput} value={row.door_id || ''} onChange={(e) => update(index, { door_id: e.target.value || null })}>
              <option value="">无</option>
              {world.doors.map((door) => (
                <option key={door.id} value={door.id}>
                  {door.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-[10px] text-white/30">需要物品</span>
            <select className={tinyInput} value={row.requires_item_id || ''} onChange={(e) => update(index, { requires_item_id: e.target.value || null })}>
              <option value="">无</option>
              {world.items.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.name}
                </option>
              ))}
            </select>
          </label>
        </RowShell>
      ))}
      <button
        onClick={() => onChange([...value, { target_id: '', label: '', door_id: null, requires_item_id: null }])}
        className="self-start text-[10px] text-white/40 hover:text-white tracking-[0.15em] uppercase"
      >
        + 添加出口
      </button>
    </div>
  )
}

// Which catalogue a condition's target picks from, and whether it also needs a value.
const CONDITION_TARGETS = {
  flag: { source: 'flags', placeholder: '标记名，如 door_seen' },
  has_item: { source: 'items', placeholder: '物品 ID' },
  item_count: { source: 'items', placeholder: '物品 ID' },
  door_open: { source: 'doors', placeholder: '门 ID' },
  at_location: { source: 'locations', placeholder: '地点 ID' },
  quest_status: { source: 'quests', placeholder: '任务 ID', value: '任务状态 inactive/active/completed/failed' },
  entity_at: { source: 'entities', placeholder: '实体 ID', value: '地点 ID' },
  entity_alive: { source: 'entities', placeholder: '实体 ID' },
  combatant_hp: { source: 'entities', placeholder: '实体 ID', value: '生命值下限' },
}

function ConditionRows({ value, onChange, world }) {
  const update = (index, patch) =>
    onChange(value.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  const options = (source) => {
    if (!world) return []
    if (source === 'items') return world.items.map((entry) => [entry.id, entry.name])
    if (source === 'doors') return world.doors.map((entry) => [entry.id, entry.name])
    if (source === 'locations') return world.locations.map((entry) => [entry.id, entry.name])
    if (source === 'quests') return world.quests.map((entry) => [entry.id, entry.title])
    if (source === 'entities') return world.entities.map((entry) => [entry.id, entry.name])
    return []
  }
  return (
    <div className="flex flex-col gap-2">
      {value.map((row, index) => {
        const meta = CONDITION_TARGETS[row.kind] || {}
        const choices = options(meta.source)
        const needsValue = Boolean(meta.value) || row.kind === 'flag'
        return (
        <RowShell key={index} onRemove={() => onChange(value.filter((_, i) => i !== index))}>
          <select className={tinyInput} value={row.kind} onChange={(e) => update(index, { kind: e.target.value })}>
            {CONDITION_KINDS.map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </select>
          {choices.length > 0 ? (
            <select
              className={tinyInput + ' w-40'}
              value={row.target || ''}
              onChange={(e) => update(index, { target: e.target.value })}
            >
              <option value="">{meta.placeholder || '选择目标'}</option>
              {choices.map(([id, label]) => (
                <option key={id} value={id}>
                  {label} ({id})
                </option>
              ))}
            </select>
          ) : (
            <input
              className={tinyInput + ' w-40'}
              value={row.target || ''}
              onChange={(e) => update(index, { target: e.target.value })}
              placeholder={meta.placeholder || '目标 ID（物品/门/地点/任务/标记）'}
            />
          )}
          <select className={tinyInput} value={row.op || 'eq'} onChange={(e) => update(index, { op: e.target.value })}>
            {OPS.map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </select>
          <input
            className={tinyInput + ' w-28'}
            value={showValue(row.value)}
            onChange={(e) => update(index, { value: parseValue(e.target.value) })}
            placeholder={needsValue ? meta.value || '值' : 'true / 1 / active'}
          />
        </RowShell>
        )
      })}
      <button
        onClick={() => onChange([...value, { kind: 'flag', target: '', op: 'eq', value: true }])}
        className="self-start text-[10px] text-white/40 hover:text-white tracking-[0.15em] uppercase"
      >
        + 添加条件
      </button>
    </div>
  )
}

// What an effect points at, and whether its value is an id, a number or free text.
const EFFECT_TARGETS = {
  set_flag: { source: 'flags', placeholder: '标记名' },
  give_item: { source: 'items', placeholder: '物品 ID' },
  remove_item: { source: 'items', placeholder: '物品 ID' },
  move_player: { source: 'locations', placeholder: '地点 ID' },
  move_entity: { source: 'entities', placeholder: '实体 ID', value: { source: 'locations', hint: '目标地点 ID' } },
  open_door: { source: 'doors', placeholder: '门 ID' },
  close_door: { source: 'doors', placeholder: '门 ID' },
  quest_progress: { source: 'quests', placeholder: '任务 ID', value: { hint: '目标阶段 ID' } },
  quest_complete: { source: 'quests', placeholder: '任务 ID' },
  combat_hp: { source: 'entities', placeholder: '实体 ID', value: { hint: '剩余生命值' } },
  player_damage: { source: 'player', placeholder: 'player' },
  emit: { source: 'none', placeholder: '信号名' },
}

function EffectRows({ value, onChange, world }) {
  const update = (index, patch) =>
    onChange(value.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  const options = (source) => {
    if (!world) return []
    if (source === 'items') return world.items.map((entry) => [entry.id, entry.name])
    if (source === 'doors') return world.doors.map((entry) => [entry.id, entry.name])
    if (source === 'locations') return world.locations.map((entry) => [entry.id, entry.name])
    if (source === 'quests') return world.quests.map((entry) => [entry.id, entry.title])
    if (source === 'entities') return world.entities.map((entry) => [entry.id, entry.name])
    return []
  }
  return (
    <div className="flex flex-col gap-2">
      {value.map((row, index) => {
        const meta = EFFECT_TARGETS[row.kind] || {}
        const choices = options(meta.source)
        const valueOptions = options(meta.value && meta.value.source)
        const valueHint = (meta.value && (meta.value.hint || '值')) || '值（可空）'
        return (
        <RowShell key={index} onRemove={() => onChange(value.filter((_, i) => i !== index))}>
          <select className={tinyInput} value={row.kind} onChange={(e) => update(index, { kind: e.target.value })}>
            {EFFECT_KINDS.map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </select>
          {choices.length > 0 ? (
            <select
              className={tinyInput + ' w-44'}
              value={row.target || ''}
              onChange={(e) => update(index, { target: e.target.value })}
            >
              <option value="">{meta.placeholder || '选择目标'}</option>
              {choices.map(([id, label]) => (
                <option key={id} value={id}>
                  {label} ({id})
                </option>
              ))}
            </select>
          ) : (
            <input
              className={tinyInput + ' w-44'}
              value={row.target || ''}
              onChange={(e) => update(index, { target: e.target.value })}
              placeholder={meta.placeholder || '目标 ID'}
            />
          )}
          {valueOptions.length > 0 ? (
            <select
              className={tinyInput + ' w-28'}
              value={row.value === undefined || row.value === null ? '' : String(row.value)}
              onChange={(e) => update(index, { value: e.target.value || null })}
            >
              <option value="">{valueHint}</option>
              {valueOptions.map(([id, label]) => (
                <option key={id} value={id}>
                  {label} ({id})
                </option>
              ))}
            </select>
          ) : (
            <input
              className={tinyInput + ' w-24'}
              value={showValue(row.value)}
              onChange={(e) => update(index, { value: parseValue(e.target.value) })}
              placeholder={valueHint}
            />
          )}
          <input
            className={tinyInput + ' w-20'}
            type="number"
            value={row.amount === undefined || row.amount === null ? 1 : row.amount}
            onChange={(e) => update(index, { amount: Number(e.target.value) })}
            placeholder="数量"
          />
        </RowShell>
        )
      })}
      <button
        onClick={() => onChange([...value, { kind: 'set_flag', target: '', value: true, amount: 1 }])}
        className="self-start text-[10px] text-white/40 hover:text-white tracking-[0.15em] uppercase"
      >
        + 添加效果
      </button>
    </div>
  )
}

function ObjectiveRows({ value, onChange }) {
  const update = (index, patch) =>
    onChange(value.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  return (
    <div className="flex flex-col gap-2">
      {value.map((row, index) => (
        <RowShell key={index} onRemove={() => onChange(value.filter((_, i) => i !== index))}>
          <input className={tinyInput + ' w-28'} value={row.id} onChange={(e) => update(index, { id: e.target.value })} placeholder="目标 ID" />
          <select className={tinyInput} value={row.kind} onChange={(e) => update(index, { kind: e.target.value })}>
            {OBJECTIVE_KINDS.map(([id, label]) => (
              <option key={id} value={id}>
                {label}
              </option>
            ))}
          </select>
          <input className={tinyInput + ' w-36'} value={row.target || ''} onChange={(e) => update(index, { target: e.target.value })} placeholder="目标物 / 门 / 地点" />
          <input
            className={tinyInput + ' w-20'}
            type="number"
            min="1"
            value={row.required_count}
            onChange={(e) => update(index, { required_count: Math.max(1, Number(e.target.value)) })}
          />
          <input className={tinyInput + ' w-40'} value={row.description || ''} onChange={(e) => update(index, { description: e.target.value })} placeholder="说明" />
        </RowShell>
      ))}
      <button
        onClick={() => onChange([...value, { id: '', kind: 'obtain_item', target: '', required_count: 1, description: '' }])}
        className="self-start text-[10px] text-white/40 hover:text-white tracking-[0.15em] uppercase"
      >
        + 添加目标
      </button>
    </div>
  )
}

function ResourceForm({ editor, world, onChange, onSave, onCancel, saving }) {
  const value = editor.value
  const update = (patch) => onChange({ ...editor, value: { ...value, ...patch } })
  const kind = editor.kind

  return (
    <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-5 flex flex-col gap-4">
      {kind === 'location' && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <label>
              <FieldLabel>地点 ID</FieldLabel>
              <input className={inputClass} value={value.id} onChange={(e) => update({ id: e.target.value })} placeholder="village_square" />
            </label>
            <label>
              <FieldLabel>名称</FieldLabel>
              <input className={inputClass} value={value.name} onChange={(e) => update({ name: e.target.value })} />
            </label>
          </div>
          <label>
            <FieldLabel>描述（叙事只能读到这段文字，不能改写它）</FieldLabel>
            <textarea className={inputClass + ' min-h-[90px] leading-relaxed'} value={value.description || ''} onChange={(e) => update({ description: e.target.value })} />
          </label>
          <label>
            <FieldLabel>标签（逗号分隔）</FieldLabel>
            <input className={inputClass} value={(value.tags || []).join(', ')} onChange={(e) => update({ tags: splitList(e.target.value) })} />
          </label>
          <div>
            <FieldLabel>出口与可达关系</FieldLabel>
            <ExitRows world={world} value={value.exits || []} onChange={(exits) => update({ exits })} />
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <label>
              <FieldLabel>地上的物品</FieldLabel>
              <select
                multiple
                className={inputClass + ' h-28'}
                value={value.item_ids || []}
                onChange={(e) => update({ item_ids: Array.from(e.target.selectedOptions).map((option) => option.value) })}
              >
                {world.items.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <FieldLabel>在场的实体</FieldLabel>
              <select
                multiple
                className={inputClass + ' h-28'}
                value={value.npc_ids || []}
                onChange={(e) => update({ npc_ids: Array.from(e.target.selectedOptions).map((option) => option.value) })}
              >
                {world.entities.map((entity) => (
                  <option key={entity.id} value={entity.id}>
                    {entity.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <FieldLabel>门（用于渲染与校验）</FieldLabel>
              <select
                multiple
                className={inputClass + ' h-28'}
                value={value.door_ids || []}
                onChange={(e) => update({ door_ids: Array.from(e.target.selectedOptions).map((option) => option.value) })}
              >
                {world.doors.map((door) => (
                  <option key={door.id} value={door.id}>
                    {door.name}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </>
      )}

      {kind === 'item' && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <label>
              <FieldLabel>物品 ID</FieldLabel>
              <input className={inputClass} value={value.id} onChange={(e) => update({ id: e.target.value })} placeholder="iron_key" />
            </label>
            <label>
              <FieldLabel>名称</FieldLabel>
              <input className={inputClass} value={value.name} onChange={(e) => update({ name: e.target.value })} />
            </label>
          </div>
          <label>
            <FieldLabel>描述</FieldLabel>
            <textarea className={inputClass + ' min-h-[70px]'} value={value.description || ''} onChange={(e) => update({ description: e.target.value })} />
          </label>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <label>
              <FieldLabel>标签（逗号分隔）</FieldLabel>
              <input className={inputClass} value={(value.tags || []).join(', ')} onChange={(e) => update({ tags: splitList(e.target.value) })} />
            </label>
            <label className="flex items-center gap-2 text-xs text-white/60 mt-6">
              <input type="checkbox" checked={Boolean(value.stackable)} onChange={(e) => update({ stackable: e.target.checked })} />
              可堆叠
            </label>
          </div>
          <div>
            <FieldLabel>拾取条件（全部满足才能拿起）</FieldLabel>
            <ConditionRows value={value.pickup_requires || []} onChange={(pickup_requires) => update({ pickup_requires })} world={world} />
          </div>
          <div>
            <FieldLabel>使用效果（留空则沿用内置行为：作为钥匙开门）</FieldLabel>
            <EffectRows value={value.use_effects || []} onChange={(use_effects) => update({ use_effects })} world={world} />
          </div>
          <label className="flex items-center gap-2 text-xs text-white/60">
            <input type="checkbox" checked={Boolean(value.consumable)} onChange={(e) => update({ consumable: e.target.checked })} />
            使用后消耗一个
          </label>
        </>
      )}

      {kind === 'door' && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <label>
              <FieldLabel>门 ID</FieldLabel>
              <input className={inputClass} value={value.id} onChange={(e) => update({ id: e.target.value })} placeholder="warehouse_door" />
            </label>
            <label>
              <FieldLabel>名称</FieldLabel>
              <input className={inputClass} value={value.name} onChange={(e) => update({ name: e.target.value })} />
            </label>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
            <label>
              <FieldLabel>所在地点</FieldLabel>
              <select className={inputClass} value={value.location_id || ''} onChange={(e) => update({ location_id: e.target.value })}>
                <option value="">选择地点</option>
                {world.locations.map((location) => (
                  <option key={location.id} value={location.id}>
                    {location.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <FieldLabel>钥匙物品</FieldLabel>
              <select className={inputClass} value={value.key_item_id || ''} onChange={(e) => update({ key_item_id: e.target.value || null })}>
                <option value="">无需钥匙</option>
                {world.items.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-2 text-xs text-white/60 mt-6">
              <input type="checkbox" checked={Boolean(value.locked)} onChange={(e) => update({ locked: e.target.checked })} />
              上锁
            </label>
            <label className="flex items-center gap-2 text-xs text-white/60 mt-6">
              <input type="checkbox" checked={Boolean(value.consumes_key)} onChange={(e) => update({ consumes_key: e.target.checked })} />
              消耗钥匙
            </label>
          </div>
        </>
      )}

      {kind === 'entity' && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <label>
              <FieldLabel>实体 ID</FieldLabel>
              <input className={inputClass} value={value.id} onChange={(e) => update({ id: e.target.value })} placeholder="elder_mara" />
            </label>
            <label>
              <FieldLabel>名称</FieldLabel>
              <input className={inputClass} value={value.name} onChange={(e) => update({ name: e.target.value })} />
            </label>
            <label>
              <FieldLabel>类型</FieldLabel>
              <select className={inputClass} value={value.kind || 'npc'} onChange={(e) => update({ kind: e.target.value })}>
                {ENTITY_KINDS.map(([id, label]) => (
                  <option key={id} value={id}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <label>
            <FieldLabel>描述</FieldLabel>
            <textarea className={inputClass + ' min-h-[70px]'} value={value.description || ''} onChange={(e) => update({ description: e.target.value })} />
          </label>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <label>
              <FieldLabel>所在地点</FieldLabel>
              <select className={inputClass} value={value.location_id || ''} onChange={(e) => update({ location_id: e.target.value })}>
                <option value="">选择地点</option>
                {world.locations.map((location) => (
                  <option key={location.id} value={location.id}>
                    {location.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <FieldLabel>标签（逗号分隔）</FieldLabel>
              <input className={inputClass} value={(value.tags || []).join(', ')} onChange={(e) => update({ tags: splitList(e.target.value) })} />
            </label>
          </div>
          <label>
            <FieldLabel>台词（每行一句；只进入叙事上下文，不改变状态）</FieldLabel>
            <textarea
              className={inputClass + ' min-h-[80px]'}
              value={(value.dialogue || []).join('\n')}
              onChange={(e) => update({ dialogue: e.target.value.split('\n').filter((line) => line.trim()) })}
            />
          </label>
          <label className="flex items-center gap-2 text-xs text-white/60">
            <input type="checkbox" checked={Boolean(value.hostile)} onChange={(e) => update({ hostile: e.target.checked })} />
            敌对
          </label>
        </>
      )}

      {kind === 'event' && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <label>
              <FieldLabel>事件 ID</FieldLabel>
              <input className={inputClass} value={value.id} onChange={(e) => update({ id: e.target.value })} placeholder="keeper_alert" />
            </label>
            <label>
              <FieldLabel>名称</FieldLabel>
              <input className={inputClass} value={value.name} onChange={(e) => update({ name: e.target.value })} />
            </label>
          </div>
          <label>
            <FieldLabel>描述</FieldLabel>
            <textarea className={inputClass + ' min-h-[70px]'} value={value.description || ''} onChange={(e) => update({ description: e.target.value })} />
          </label>
          <div className="grid grid-cols-1 sm:grid-cols-4 gap-4">
            <label>
              <FieldLabel>触发方式</FieldLabel>
              <select className={inputClass} value={value.trigger || 'action'} onChange={(e) => update({ trigger: e.target.value })}>
                {TRIGGERS.map(([id, label]) => (
                  <option key={id} value={id}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <FieldLabel>触发行动</FieldLabel>
              <input className={inputClass} value={value.trigger_action || ''} onChange={(e) => update({ trigger_action: e.target.value || null })} placeholder="take / open / move" />
            </label>
            <label>
              <FieldLabel>触发目标</FieldLabel>
              <input className={inputClass} value={value.trigger_target || ''} onChange={(e) => update({ trigger_target: e.target.value || null })} placeholder="iron_key" />
            </label>
            <label>
              <FieldLabel>优先级</FieldLabel>
              <input className={inputClass} type="number" value={value.priority === undefined ? 0 : value.priority} onChange={(e) => update({ priority: Number(e.target.value) })} />
            </label>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <label>
              <FieldLabel>重复策略</FieldLabel>
              <select className={inputClass} value={value.repeat || 'once'} onChange={(e) => update({ repeat: e.target.value })}>
                {REPEATS.map(([id, label]) => (
                  <option key={id} value={id}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-2 text-xs text-white/60 mt-6">
              <input type="checkbox" checked={Boolean(value.enabled)} onChange={(e) => update({ enabled: e.target.checked })} />
              启用
            </label>
          </div>
          <div>
            <FieldLabel>触发条件（全部满足）</FieldLabel>
            <ConditionRows value={value.conditions || []} onChange={(conditions) => update({ conditions })} world={world} />
          </div>
          <div>
            <FieldLabel>状态效果（由规则引擎校验后执行）</FieldLabel>
            <EffectRows value={value.effects || []} onChange={(effects) => update({ effects })} world={world} />
          </div>
        </>
      )}

      {kind === 'quest' && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <label>
              <FieldLabel>任务 ID</FieldLabel>
              <input className={inputClass} value={value.id} onChange={(e) => update({ id: e.target.value })} placeholder="open_the_warehouse" />
            </label>
            <label>
              <FieldLabel>标题</FieldLabel>
              <input className={inputClass} value={value.title} onChange={(e) => update({ title: e.target.value })} />
            </label>
          </div>
          <label>
            <FieldLabel>描述</FieldLabel>
            <textarea className={inputClass + ' min-h-[70px]'} value={value.description || ''} onChange={(e) => update({ description: e.target.value })} />
          </label>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <label className="flex items-center gap-2 text-xs text-white/60">
              <input type="checkbox" checked={Boolean(value.enabled)} onChange={(e) => update({ enabled: e.target.checked })} />
              启用
            </label>
            <label className="flex items-center gap-2 text-xs text-white/60">
              <input type="checkbox" checked={value.auto_accept !== false} onChange={(e) => update({ auto_accept: e.target.checked })} />
              满足条件时自动接受
            </label>
          </div>
          <div>
            <FieldLabel>前置任务（全部完成后才会激活）</FieldLabel>
            <div className="flex flex-wrap gap-2">
              {(world?.quests || [])
                .filter((other) => other.id !== value.id)
                .map((other) => {
                  const picked = (value.prerequisites || []).includes(other.id)
                  return (
                    <button
                      key={other.id}
                      type="button"
                      onClick={() => {
                        const current = value.prerequisites || []
                        update({
                          prerequisites: picked ? current.filter((id) => id !== other.id) : [...current, other.id],
                        })
                      }}
                      className={
                        'text-[10px] font-mono rounded-full px-2 py-1 border transition ' +
                        (picked
                          ? 'border-white/40 text-white bg-white/10'
                          : 'border-white/10 text-white/40 hover:text-white/70')
                      }
                    >
                      {other.title || other.id}
                    </button>
                  )
                })}
              {(world?.quests || []).filter((other) => other.id !== value.id).length === 0 && (
                <span className="text-[10px] text-white/30">（没有其他任务可选）</span>
              )}
            </div>
          </div>
          <div>
            <FieldLabel>接受条件（全部满足才会激活）</FieldLabel>
            <ConditionRows value={value.accept_requires || []} onChange={(accept_requires) => update({ accept_requires })} world={world} />
          </div>
          <div>
            <FieldLabel>失败条件（任一满足即失败）</FieldLabel>
            <ConditionRows value={value.fail_conditions || []} onChange={(fail_conditions) => update({ fail_conditions })} world={world} />
          </div>
          <div>
            <FieldLabel>任务目标</FieldLabel>
            <ObjectiveRows value={value.objectives || []} onChange={(objectives) => update({ objectives })} />
          </div>
          <div>
            <FieldLabel>完成奖励</FieldLabel>
            <EffectRows value={value.rewards || []} onChange={(rewards) => update({ rewards })} world={world} />
          </div>
        </>
      )}

      {kind === 'action' && (
        <>
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <label>
              <FieldLabel>行动 ID</FieldLabel>
              <input className={inputClass} value={value.id} onChange={(e) => update({ id: e.target.value })} placeholder="pray" />
            </label>
            <label>
              <FieldLabel>显示名称</FieldLabel>
              <input className={inputClass} value={value.label || ''} onChange={(e) => update({ label: e.target.value })} placeholder="祈祷" />
            </label>
            <label>
              <FieldLabel>目标要求</FieldLabel>
              <select className={inputClass} value={value.target || 'none'} onChange={(e) => update({ target: e.target.value })}>
                <option value="none">不需要目标</option>
                <option value="optional">目标可选</option>
                <option value="required">必须指定目标</option>
              </select>
            </label>
          </div>
          <label>
            <FieldLabel>描述（规则通过后作为行动结果说明）</FieldLabel>
            <textarea className={inputClass + ' min-h-[70px]'} value={value.description || ''} onChange={(e) => update({ description: e.target.value })} />
          </label>
          <label>
            <FieldLabel>触发词（逗号分隔，不能与内置动词重复）</FieldLabel>
            <input
              className={inputClass}
              value={(value.verbs || []).join(', ')}
              onChange={(e) => update({ verbs: splitList(e.target.value) })}
              placeholder="祈祷, pray"
            />
          </label>
          <div>
            <FieldLabel>前置条件（全部满足才允许执行）</FieldLabel>
            <ConditionRows value={value.requires || []} onChange={(requires) => update({ requires })} world={world} />
          </div>
          <div>
            <FieldLabel>行动效果（通过校验后由规则引擎执行）</FieldLabel>
            <EffectRows value={value.effects || []} onChange={(effects) => update({ effects })} />
          </div>
        </>
      )}

      <div className="flex items-center gap-3">
        <button onClick={onSave} disabled={saving} className={primaryButton}>
          {saving ? '保存中…' : editor.isNew ? '创建' : '保存修改'}
        </button>
        <button onClick={onCancel} className={ghostButton}>
          取消
        </button>
        <span className="text-[10px] text-white/30">
          保存时服务端会做结构与引用校验，校验不通过不会写入。
        </span>
      </div>
    </div>
  )
}

const SAVERS = {
  location: saveWorldLocation,
  action: saveWorldCustomAction,
  item: saveWorldItem,
  door: saveWorldDoor,
  entity: saveWorldEntity,
  event: saveWorldEvent,
  quest: saveWorldQuest,
}

const DELETERS = {
  location: deleteWorldLocation,
  action: deleteWorldCustomAction,
  item: deleteWorldItem,
  door: deleteWorldDoor,
  entity: deleteWorldEntity,
  event: deleteWorldEvent,
  quest: deleteWorldQuest,
}

const EMPTY_FOR = {
  location: { id: '', name: '', description: '', tags: [], exits: [], door_ids: [], item_ids: [], npc_ids: [] },
  item: { id: '', name: '', description: '', tags: [], stackable: true, pickup_requires: [], use_effects: [], consumable: false },
  door: { id: '', name: '', location_id: '', locked: true, opened: false, key_item_id: null, consumes_key: false },
  entity: {
    id: '',
    name: '',
    description: '',
    tags: [],
    location_id: '',
    kind: 'npc',
    dialogue: [],
    hostile: false,
    attributes: {},
  },
  event: {
    id: '',
    name: '',
    description: '',
    enabled: true,
    trigger: 'action',
    trigger_action: null,
    trigger_target: null,
    conditions: [],
    effects: [],
    priority: 0,
    repeat: 'once',
  },
  quest: {
    id: '',
    title: '',
    description: '',
    enabled: true,
    objectives: [],
    rewards: [],
    prerequisites: [],
    accept_requires: [],
    fail_conditions: [],
    auto_accept: true,
  },
  action: {
    id: '',
    label: '',
    description: '',
    verbs: [],
    target: 'none',
    requires: [],
    effects: [],
  },
}

const KIND_LABELS = {
  location: '地点',
  action: '行动',
  item: '物品',
  door: '门',
  entity: '实体',
  event: '事件',
  quest: '任务',
}

const TAB_KIND = {
  items: 'item',
  doors: 'door',
  entities: 'entity',
  events: 'event',
  quests: 'quest',
  actions: 'action',
}

export default function WorldStudio({ open, onClose, scenarioId, campaignId }) {
  const [worlds, setWorlds] = useState([])
  const [worldId, setWorldId] = useState('')
  const [world, setWorld] = useState(null)
  const [mapData, setMapData] = useState(null)
  const [tab, setTab] = useState('map')
  const [errors, setErrors] = useState([])
  const [notice, setNotice] = useState('')
  const [editor, setEditor] = useState(null)
  const [saving, setSaving] = useState(false)
  const [sim, setSim] = useState(null)
  const [debug, setDebug] = useState({ progress: null, executions: [], diagnostics: null, state: null, snapshots: [] })
  const [diff, setDiff] = useState(null)
  const [busyVersion, setBusyVersion] = useState(null)
  const [debugBusy, setDebugBusy] = useState(false)
  const [debugNonce, setDebugNonce] = useState(0)
  const [binding, setBinding] = useState(false)
  const [builtinActions, setBuiltinActions] = useState([])

  const report = useCallback((err) => {
    if (Array.isArray(err && err.detail)) setErrors(err.detail)
    else if (typeof (err && err.detail) === 'string') setErrors([err.detail])
    else setErrors([(err && err.message) || '操作失败'])
    setNotice('')
  }, [])

  const loadWorld = useCallback(async (id) => {
    if (!id) {
      setWorld(null)
      setMapData(null)
      return
    }
    const [definition, map] = await Promise.all([fetchWorld(id), fetchWorldMap(id)])
    setWorld(definition)
    setMapData(map)
  }, [])

  const fetchActionCatalogue = useCallback(async () => {
    try {
      setBuiltinActions((await fetchActionTypes()) || [])
    } catch {
      // The catalogue is a convenience reference; failing to load it must not
      // block editing the world itself.
      setBuiltinActions([])
    }
  }, [])

  useEffect(() => {
    if (!open || tab !== 'actions' || builtinActions.length > 0) return undefined
    fetchActionCatalogue()
    return undefined
  }, [open, tab, builtinActions.length, fetchActionCatalogue])

  useEffect(() => {
    if (!open) return undefined
    let cancelled = false
    setErrors([])
    setNotice('')
    setEditor(null)
    setSim(null)
    setTab('map')
    ;(async () => {
      try {
        const list = await fetchWorlds(scenarioId || undefined)
        if (cancelled) return
        setWorlds(list || [])
        if (list && list.length > 0) {
          const chosen = list.find((item) => item.scenario_id === scenarioId) || list[0]
          setWorldId(chosen.world_id)
          await loadWorld(chosen.world_id)
        } else {
          setWorldId('')
          setWorld(null)
          setMapData(null)
        }
      } catch (err) {
        if (!cancelled) report(err)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [open, scenarioId, loadWorld, report])

  useEffect(() => {
    if (!open || tab !== 'debug' || !campaignId) return undefined
    let cancelled = false
    ;(async () => {
      setDebugBusy(true)
      try {
        const [progress, executions, diagnostics, state, snapshots] = await Promise.all([
          fetchQuestProgress(campaignId).catch(() => null),
          fetchEventExecutions(campaignId, { limit: 50 }).catch(() => []),
          fetchCampaignDiagnostics(campaignId).catch(() => null),
          fetchCampaignEngineState(campaignId).catch(() => null),
          fetchCampaignSnapshots(campaignId, 30).catch(() => []),
        ])
        if (!cancelled)
          setDebug({ progress, executions: executions || [], diagnostics, state, snapshots: snapshots || [] })
      } finally {
        if (!cancelled) setDebugBusy(false)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [open, tab, campaignId, debugNonce])

  const selectWorld = async (id) => {
    setErrors([])
    setNotice('')
    setEditor(null)
    setSim(null)
    setWorldId(id)
    try {
      await loadWorld(id)
    } catch (err) {
      report(err)
    }
  }

  const createTemplateWorld = async () => {
    setErrors([])
    setNotice('')
    const stem = (scenarioId || 'world').replace(/[^a-zA-Z0-9]/g, '').slice(0, 12) || 'world'
    let id = 'world_' + stem
    if (worlds.some((item) => item.world_id === id)) {
      id = id + '_' + Date.now().toString(36).slice(-4)
    }
    try {
      await createWorld(demoWorldTemplate(scenarioId || '', id))
      const list = await fetchWorlds(scenarioId || undefined)
      setWorlds(list || [])
      setWorldId(id)
      await loadWorld(id)
      setNotice('已创建示范世界：' + id)
    } catch (err) {
      report(err)
    }
  }

  const removeExit = async (sourceId, targetId) => {
    if (!worldId || !world) return
    const source = (world.locations || []).find((item) => item.id === sourceId)
    if (!source) return
    const exits = (source.exits || []).filter((exit) => exit.target_id !== targetId)
    setErrors([])
    setNotice('')
    try {
      const updated = await saveWorldLocation(worldId, sourceId, { ...source, exits })
      setWorld(updated)
      setMapData(await fetchWorldMap(worldId))
      setNotice('已删除出口 ' + sourceId + ' → ' + targetId)
    } catch (err) {
      report(err)
    }
  }

  const openEditor = (kind, value) => {
    setErrors([])
    setNotice('')
    setEditor({
      kind,
      value: value ? JSON.parse(JSON.stringify(value)) : { ...EMPTY_FOR[kind] },
      isNew: !value,
    })
  }

  const editLocationById = (locationId) => {
    const location = ((world && world.locations) || []).find((item) => item.id === locationId)
    if (location) openEditor('location', location)
  }

  const save = async () => {
    if (!editor || !worldId) return
    const { kind, value } = editor
    if (!value.id) {
      setErrors(['ID 不能为空'])
      return
    }
    setSaving(true)
    setErrors([])
    setNotice('')
    try {
      const updated = await SAVERS[kind](worldId, value.id, value)
      setWorld(updated)
      setEditor(null)
      setNotice('已保存 ' + (value.name || value.title || value.label || value.id))
      setMapData(await fetchWorldMap(worldId))
      setWorlds((prev) =>
        prev.map((item) =>
          item.world_id === updated.world_id
            ? { ...item, name: updated.name, scenario_id: updated.scenario_id }
            : item
        )
      )
    } catch (err) {
      report(err)
    } finally {
      setSaving(false)
    }
  }

  const remove = async (kind, value) => {
    if (!worldId) return
    const label = value.name || value.title || value.label || value.id
    if (!window.confirm('删除「' + label + '」？引用它的出口与列表项也会被清理。')) return
    setErrors([])
    setNotice('')
    try {
      const updated = await DELETERS[kind](worldId, value.id)
      setWorld(updated)
      setEditor(null)
      setNotice('已删除「' + label + '」')
      setMapData(await fetchWorldMap(worldId))
    } catch (err) {
      report(err)
    }
  }

  const runValidate = async (eventId) => {
    setErrors([])
    setNotice('')
    try {
      const result = await validateWorldEvent(worldId, eventId)
      if (result.valid) setNotice('事件「' + eventId + '」校验通过')
      else setErrors(result.errors || [])
    } catch (err) {
      report(err)
    }
  }

  const runSimulate = async (eventId) => {
    setErrors([])
    setNotice('')
    try {
      const result = await simulateWorldEvent(worldId, eventId, campaignId || undefined)
      setSim({ eventId, result })
      setNotice('事件「' + eventId + '」模拟完成；模拟不会写入真实状态')
    } catch (err) {
      report(err)
    }
  }

  const compareWithPrevious = async (version) => {
    if (!campaignId) return
    setErrors([])
    setBusyVersion(version)
    try {
      const result = await fetchCampaignStateDiff(campaignId, version - 1, version)
      setDiff({ from: version - 1, to: version, changed: result.changed || {} })
    } catch (err) {
      report(err)
      setDiff(null)
    } finally {
      setBusyVersion(null)
    }
  }

  const rollbackTo = async (version) => {
    if (!campaignId) return
    if (!window.confirm('把当前冒险回滚到版本 ' + version + '？回滚会生成一个新的版本，历史记录不会删除。')) return
    setErrors([])
    setBusyVersion(version)
    try {
      const result = await rollbackCampaign(campaignId, version, 'via world studio debug')
      setNotice('已回滚到版本 ' + version + '，当前版本 ' + (result.state?.version ?? result.restored_version ?? ''))
      setDebugNonce((n) => n + 1)
    } catch (err) {
      report(err)
    } finally {
      setBusyVersion(null)
    }
  }

  const inspectAction = async (requestId) => {
    if (!requestId) return
    setErrors([])
    try {
      const result = await fetchActionDetail(requestId)
      setDiff({ action: requestId, detail: result })
    } catch (err) {
      report(err)
      setDiff(null)
    }
  }

  const bindWorld = async () => {
    if (!campaignId || !worldId) return
    setErrors([])
    setNotice('')
    setBinding(true)
    try {
      await bindCampaignWorld(campaignId, worldId)
      setNotice('已把世界「' + (world?.name || worldId) + '」绑定到当前冒险')
      setDebugNonce((n) => n + 1)
    } catch (err) {
      report(err)
    } finally {
      setBinding(false)
    }
  }

  if (!open) return null

  const activeKind = TAB_KIND[tab] || null

  const renderList = (kind, items) => (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <SectionNote>
          {kind === 'item' &&
            '物品在这里定义名称、拾取条件与使用效果；数量与持有关系属于权威状态，由规则引擎改变。'}
          {kind === 'door' && '门是独立配置：上锁、钥匙和是否消耗钥匙都在这里决定，开门仍要经过规则判定。'}
          {kind === 'entity' && '实体只保存固定配置；它当前在哪个地点、携带什么属于运行时状态。'}
          {kind === 'event' && '事件由条件与效果组成，保存时服务端会做结构校验，执行前再校验一次；未知条件或效果会安全失败。'}
          {kind === 'quest' &&
            '任务的目标、前置、接受与失败条件都是配置；进度由规则与事件驱动，模型只负责描述任务，不负责判定完成。'}
          {kind === 'action' &&
            '自定义行动只是配置：条件与效果都由规则引擎判定，绝不执行任意代码。条件不满足时会安全拒绝。'}
        </SectionNote>
        <button onClick={() => openEditor(kind, null)} className={primaryButton}>
          新建{KIND_LABELS[kind]}
        </button>
      </div>

      {editor && editor.kind === kind && (
        <ResourceForm
          editor={editor}
          world={world}
          onChange={setEditor}
          onSave={save}
          onCancel={() => setEditor(null)}
          saving={saving}
        />
      )}

      {kind === 'action' && builtinActions.length > 0 && (
        <div className="rounded-2xl border border-white/10 bg-white/[0.02] p-4">
          <div className="text-[10px] font-mono uppercase tracking-[0.2em] text-white/40 mb-2">
            内置行动（由引擎提供，无需配置）
          </div>
          <div className="flex flex-wrap gap-2">
            {builtinActions.map((spec) => (
              <span key={spec.name} className="text-[10px] font-mono text-white/40 border border-white/10 rounded-full px-2 py-0.5">
                {spec.label} · {spec.name} · 目标 {spec.target}
              </span>
            ))}
          </div>
        </div>
      )}

      {items.length === 0 && <EmptyHint>还没有配置{KIND_LABELS[kind]}。</EmptyHint>}

      {items.map((item) => (
        <div
          key={item.id}
          className="rounded-2xl border border-white/10 bg-white/[0.03] p-4 flex flex-wrap items-start gap-3"
        >
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-bold text-white">
                {item.name || item.title || item.label || item.id}
              </span>
              <Badge>{item.id}</Badge>
              {item.enabled === false && <Badge tone="warn">已停用</Badge>}
              {kind === 'door' && item.locked && <Badge tone="warn">上锁</Badge>}
              {kind === 'quest' && <Badge>{(item.objectives || []).length} 个目标</Badge>}
              {kind === 'event' && <Badge>{item.repeat === 'once' ? '只执行一次' : '可重复'}</Badge>}
            </div>
            <p className="text-xs text-white/40 mt-2 leading-relaxed">{item.description || '（无描述）'}</p>
            {kind === 'action' && (
              <div className="text-[10px] font-mono text-white/30 mt-2">
                触发词 {(item.verbs || []).join(' / ') || '（无）'} · 目标 {item.target} · 条件{' '}
                {(item.requires || []).length} · 效果 {(item.effects || []).length}
              </div>
            )}
            {kind === 'item' && (
              <div className="text-[10px] font-mono text-white/30 mt-2">
                拾取条件 {(item.pickup_requires || []).length} · 使用效果 {(item.use_effects || []).length}
                {item.consumable ? ' · 使用后消耗' : ''}
              </div>
            )}
            {kind === 'quest' && (
              <div className="text-[10px] font-mono text-white/30 mt-2">
                前置 {(item.prerequisites || []).length} · 接受条件 {(item.accept_requires || []).length} ·
                失败条件 {(item.fail_conditions || []).length} · 奖励 {(item.rewards || []).length}
                {item.auto_accept === false ? ' · 需手动接受' : ''}
              </div>
            )}
            {kind === 'event' && (
              <div className="text-[10px] font-mono text-white/30 mt-2">
                触发 {item.trigger} / {item.trigger_action || '*'} / {item.trigger_target || '*'} · 条件{' '}
                {(item.conditions || []).length} · 效果 {(item.effects || []).length} · 优先级 {item.priority}
              </div>
            )}
          </div>
          <div className="flex items-center gap-3">
            {kind === 'event' && (
              <>
                <button
                  onClick={() => runValidate(item.id)}
                  className="text-[10px] text-white/50 hover:text-white tracking-[0.15em] uppercase"
                >
                  校验
                </button>
                <button
                  onClick={() => runSimulate(item.id)}
                  className="text-[10px] text-white/50 hover:text-white tracking-[0.15em] uppercase"
                >
                  模拟
                </button>
              </>
            )}
            <button
              onClick={() => openEditor(kind, item)}
              className="text-[10px] text-white/50 hover:text-white tracking-[0.15em] uppercase"
            >
              编辑
            </button>
            <button onClick={() => remove(kind, item)} className={dangerButton}>
              删除
            </button>
          </div>
        </div>
      ))}
    </div>
  )

  const catalog = {
    item: (world && world.items) || [],
    door: (world && world.doors) || [],
    entity: (world && world.entities) || [],
    event: (world && world.events) || [],
    quest: (world && world.quests) || [],
    action: (world && world.custom_actions) || [],
  }

  return (
    <div className="fixed inset-0 z-50 bg-black/80 flex items-start justify-center overflow-y-auto p-4 sm:p-8">
      <div className="w-full max-w-5xl bg-[#0b0b0b] border border-white/10 rounded-[2rem] shadow-2xl">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 p-6 border-b border-white/10">
          <div className="flex items-start gap-4">
            <img
              src="/self-reference-mark.png"
              alt=""
              aria-hidden="true"
              width="44"
              height="44"
              className="mt-1 opacity-70 shrink-0"
              draggable="false"
            />
            <div>
              <div className="text-[10px] font-mono uppercase tracking-[0.3em] text-white/30 mb-2">
                世界 · 实体 · 事件 · 任务
              </div>
              <h2 className="text-2xl font-bold text-white">{(world && world.name) || '世界工坊'}</h2>
              <p className="text-xs text-white/40 mt-2">
                这里配置的世界是规则的事实来源；叙事只能描述已经提交的状态变化。
              </p>
            </div>
          </div>
          <button onClick={onClose} className={ghostButton + ' self-start'}>
            关闭
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-3 px-6 pt-5">
          <select
            className={tinyInput + ' max-w-[16rem]'}
            value={worldId}
            onChange={(e) => selectWorld(e.target.value)}
          >
            {worlds.length === 0 && <option value="">还没有世界</option>}
            {worlds.map((item) => (
              <option key={item.world_id} value={item.world_id}>
                {item.name}（{item.world_id}）
              </option>
            ))}
          </select>
          <button onClick={createTemplateWorld} className={ghostButton}>
            从模板创建工作世界
          </button>
          <div className="flex-1" />
          <button
            onClick={async () => {
              setErrors([])
              try {
                setWorlds((await fetchWorlds(scenarioId || undefined)) || [])
                if (worldId) await loadWorld(worldId)
                setNotice('已刷新')
              } catch (err) {
                report(err)
              }
            }}
            className="text-[10px] text-white/40 hover:text-white tracking-[0.15em] uppercase"
          >
            刷新
          </button>
        </div>

        <div className="flex flex-wrap items-center gap-3 px-6 pt-4">
          {TABS.map(([id, label]) => (
            <TabButton key={id} active={tab === id} onClick={() => setTab(id)}>
              {label}
            </TabButton>
          ))}
        </div>

        {(notice || errors.length > 0) && (
          <div className="px-6 pt-4">
            {notice && (
              <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 text-emerald-300 text-xs px-4 py-2">
                {notice}
              </div>
            )}
            {errors.length > 0 && (
              <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 text-rose-300 text-xs px-4 py-3 mt-2">
                <div className="font-bold mb-1">配置未通过校验</div>
                <ul className="list-disc pl-5 space-y-1">
                  {errors.map((item, index) => (
                    <li key={index}>{typeof item === 'string' ? item : JSON.stringify(item)}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}

        <div className="p-6">
          {!world && (
            <div className="flex flex-col items-start gap-4">
              <EmptyHint>
                这个剧本还没有绑定世界。可以从模板创建一个最小可玩世界（村庄、森林、上锁的仓库和一把钥匙），再改成你自己的设定。
              </EmptyHint>
              <button onClick={createTemplateWorld} className={primaryButton}>
                从模板创建工作世界
              </button>
            </div>
          )}

          {world && tab === 'map' && (
            <div className="flex flex-col gap-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <SectionNote>
                  起点：{(mapData && mapData.start_location_id) || world.start_location_id}。出口、门与物品都已经由世界定义解析出来；点击编辑可以增删地点与连线。
                </SectionNote>
                <button onClick={() => openEditor('location', null)} className={primaryButton}>
                  新建地点
                </button>
              </div>

              {editor && editor.kind === 'location' && (
                <ResourceForm
                  editor={editor}
                  world={world}
                  onChange={setEditor}
                  onSave={save}
                  onCancel={() => setEditor(null)}
                  saving={saving}
                />
              )}

              {world.locations.length > 0 && (
                <WorldMapGraph
                  worldId={worldId}
                  mapData={mapData}
                  onEditLocation={editLocationById}
                  onDeleteExit={removeExit}
                />
              )}

              {world.locations.length === 0 && <EmptyHint>还没有地点。</EmptyHint>}

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                {((mapData && mapData.locations) || []).map((location) => (
                  <div
                    key={location.id}
                    className="rounded-2xl border border-white/10 bg-white/[0.03] p-4 flex flex-col gap-3"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-bold text-white">{location.name}</span>
                      <Badge>{location.id}</Badge>
                      {location.is_start && <Badge tone="accent">起点</Badge>}
                    </div>
                    <p className="text-xs text-white/40 leading-relaxed">{location.description || '（无描述）'}</p>
                    <div className="text-[10px] font-mono text-white/40 flex flex-col gap-1">
                      <div>
                        出口：
                        {location.exits.length === 0
                          ? '无'
                          : location.exits
                              .map(
                                (exitDef) =>
                                  (exitDef.label || '?') +
                                  '→' +
                                  exitDef.target_id +
                                  (exitDef.door
                                    ? '（门：' + exitDef.door.name + (exitDef.door.locked ? '·上锁' : '') + '）'
                                    : '')
                              )
                              .join('，')}
                      </div>
                      <div>
                        物品：
                        {location.items.length === 0
                          ? '无'
                          : location.items.map((item) => item.name + (item.known ? '' : '（悬空）')).join('，')}
                      </div>
                      <div>
                        实体：
                        {location.npcs.length === 0
                          ? '无'
                          : location.npcs.map((npc) => npc.name + (npc.known ? '' : '（悬空）')).join('，')}
                      </div>
                    </div>
                    <div className="flex items-center gap-3">
                      <button
                        onClick={() =>
                          openEditor(
                            'location',
                            (world.locations || []).find((item) => item.id === location.id) || null
                          )
                        }
                        className="text-[10px] text-white/50 hover:text-white tracking-[0.15em] uppercase"
                      >
                        编辑
                      </button>
                      <button
                        onClick={() =>
                          remove(
                            'location',
                            (world.locations || []).find((item) => item.id === location.id) || {
                              id: location.id,
                              name: location.name,
                            }
                          )
                        }
                        className={dangerButton}
                      >
                        删除
                      </button>
                    </div>
                  </div>
                ))}
              </div>

              {sim && (
                <div className="rounded-2xl border border-white/10 bg-white/[0.02] p-4">
                  <div className="text-[10px] font-mono uppercase tracking-[0.2em] text-white/40 mb-2">
                    最近一次事件模拟：{sim.eventId}
                  </div>
                  <pre className="text-[11px] text-white/60 whitespace-pre-wrap break-all max-h-64 overflow-auto">
                    {JSON.stringify(sim.result, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          )}

          {world && activeKind && renderList(activeKind, catalog[activeKind])}

          {world && tab === 'debug' && (
            <div className="flex flex-col gap-5">
              <SectionNote>
                调试面板读取的是已经提交的权威状态与事件记录；这里不能直接改状态。
                {!campaignId && ' 当前没有进行中的冒险，任务进度与事件历史为空。'}
              </SectionNote>

              {campaignId && (
                <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
                  <div className="flex flex-wrap items-center gap-3 mb-2">
                    <span className="text-sm font-bold text-white">冒险绑定</span>
                    {debug.state ? (
                      debug.state.world_id === worldId ? (
                        <Badge tone="good">已绑定此世界</Badge>
                      ) : (
                        <Badge tone="warn">绑定了 {debug.state.world_id}</Badge>
                      )
                    ) : (
                      <Badge tone="muted">未绑定世界</Badge>
                    )}
                    <Badge>版本 {debug.state ? debug.state.version : '—'}</Badge>
                    {debug.state && <Badge>回合 {debug.state.turn}</Badge>}
                  </div>
                  <p className="text-xs text-white/40 mb-3">
                    绑定后游戏内的行动会走确定性链路：规则判定 → 事务提交 → 事件与任务 → 叙事。
                  </p>
                  <button
                    onClick={bindWorld}
                    disabled={binding || !worldId || (debug.state && debug.state.world_id === worldId)}
                    className={primaryButton}
                  >
                    {binding ? '绑定中…' : '把这个世界绑定到当前冒险'}
                  </button>
                </div>
              )}

              {campaignId && (
                <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
                  <div className="flex flex-wrap items-center gap-3 mb-2">
                    <span className="text-sm font-bold text-white">状态版本与回滚</span>
                    <Badge>{(debug.snapshots || []).length} 个快照</Badge>
                    <Badge>当前 {debug.state ? debug.state.version : '—'}</Badge>
                  </div>
                  {(debug.snapshots || []).length === 0 ? (
                    <p className="text-xs text-white/40">还没有提交过任何版本。</p>
                  ) : (
                    <div className="flex flex-col gap-2">
                      {(debug.snapshots || []).map((snapshot) => (
                        <div
                          key={snapshot.version}
                          className="flex flex-wrap items-center gap-2 rounded-xl border border-white/10 px-3 py-2"
                        >
                          <Badge tone={debug.state && snapshot.version === debug.state.version ? 'good' : 'muted'}>
                            v{snapshot.version}
                          </Badge>
                          {snapshot.request_id && (
                            <button
                              onClick={() => inspectAction(snapshot.request_id)}
                              className="text-[11px] font-mono text-cyan-300/80 hover:text-cyan-200 underline underline-offset-4"
                              title="查看这次行动的判定与结果"
                            >
                              {snapshot.request_id}
                            </button>
                          )}
                          <span className="text-[10px] text-white/30 font-mono">
                            {snapshot.created_at}
                          </span>
                          <div className="flex items-center gap-2 ml-auto">
                            <button
                              onClick={() => compareWithPrevious(snapshot.version)}
                              disabled={snapshot.version === 0 || busyVersion === snapshot.version}
                              className={ghostButton}
                            >
                              对比上一版
                            </button>
                            <button
                              onClick={() => rollbackTo(snapshot.version)}
                              disabled={busyVersion === snapshot.version || (debug.state && snapshot.version === debug.state.version)}
                              className={ghostButton}
                            >
                              回滚到此版本
                            </button>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {diff && (
                <div className="rounded-2xl border border-cyan-400/20 bg-cyan-400/[0.04] p-4">
                  <div className="flex flex-wrap items-center gap-3 mb-2">
                    <span className="text-sm font-bold text-white">
                      {diff.action ? '行动详情 ' + diff.action : '差异 v' + diff.from + ' → v' + diff.to}
                    </span>
                    <button onClick={() => setDiff(null)} className={ghostButton}>
                      关闭
                    </button>
                  </div>
                  <pre className="text-[11px] leading-relaxed text-white/60 whitespace-pre-wrap break-words max-h-64 overflow-auto">
                    {JSON.stringify(diff.action ? diff.detail : diff.changed, null, 2)}
                  </pre>
                </div>
              )}

              {debug.diagnostics && (
                <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
                  <div className="flex flex-wrap items-center gap-3 mb-2">
                    <span className="text-sm font-bold text-white">状态诊断</span>
                    <Badge tone={debug.diagnostics.ok ? 'good' : 'bad'}>
                      {debug.diagnostics.ok ? '一致' : '存在问题'}
                    </Badge>
                    <Badge tone={debug.diagnostics.errors ? 'bad' : 'muted'}>
                      错误 {debug.diagnostics.errors}
                    </Badge>
                    <Badge tone={debug.diagnostics.warnings ? 'warn' : 'muted'}>
                      警告 {debug.diagnostics.warnings}
                    </Badge>
                    <Badge>版本 {debug.diagnostics.version}</Badge>
                  </div>
                  {(debug.diagnostics.issues || []).length === 0 ? (
                    <p className="text-xs text-white/40">没有发现问题。</p>
                  ) : (
                    <ul className="text-xs text-white/60 list-disc pl-5 space-y-1">
                      {(debug.diagnostics.issues || []).map((issue, index) => (
                        <li key={index}>
                          [{issue.severity}] {issue.code}：{issue.message}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {debug.progress && (
                <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
                  <div className="flex flex-wrap items-center gap-3 mb-3">
                    <span className="text-sm font-bold text-white">任务进度</span>
                    <Badge>版本 {debug.progress.version}</Badge>
                    {debugBusy && <Badge tone="warn">读取中…</Badge>}
                  </div>
                  {(debug.progress.quests || []).length === 0 && (
                    <p className="text-xs text-white/40">这个世界还没有配置任务。</p>
                  )}
                  <div className="flex flex-col gap-3">
                    {(debug.progress.quests || []).map((quest) => (
                      <div key={quest.quest_id} className="rounded-xl border border-white/10 p-3">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-xs font-bold text-white">{quest.title}</span>
                          <Badge
                            tone={
                              quest.status === 'completed'
                                ? 'good'
                                : quest.status === 'failed'
                                ? 'bad'
                                : quest.status === 'active'
                                ? 'accent'
                                : 'muted'
                            }
                          >
                            {quest.status}
                          </Badge>
                          <span className="text-[10px] font-mono text-white/30">
                            {quest.completed_objectives}/{quest.total_objectives}
                          </span>
                        </div>
                        <ul className="mt-2 text-[11px] text-white/50 space-y-1">
                          {quest.objectives.map((objective) => (
                            <li key={objective.id}>
                              {objective.done ? '✓' : '·'} {objective.description || objective.id}（
                              {objective.count}/{objective.required_count}，{objective.kind} → {objective.target}）
                            </li>
                          ))}
                        </ul>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              <div className="rounded-2xl border border-white/10 bg-white/[0.03] p-4">
                <div className="flex flex-wrap items-center gap-3 mb-3">
                  <span className="text-sm font-bold text-white">事件执行历史</span>
                  <Badge>{(debug.executions || []).length} 条</Badge>
                </div>
                {(debug.executions || []).length === 0 ? (
                  <p className="text-xs text-white/40">还没有事件被执行过。</p>
                ) : (
                  <div className="flex flex-col gap-2 max-h-80 overflow-auto">
                    {debug.executions.map((execution, index) => (
                      <div
                        key={index}
                        className="rounded-xl border border-white/10 px-3 py-2 text-[11px] text-white/60"
                      >
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge
                            tone={
                              execution.status === 'fired'
                                ? 'good'
                                : execution.status === 'failed'
                                ? 'bad'
                                : 'warn'
                            }
                          >
                            {execution.status}
                          </Badge>
                          <span className="font-bold text-white/80">{execution.name || execution.event_id}</span>
                          <span className="font-mono text-white/30">v{execution.state_version}</span>
                        </div>
                        {execution.reason && <div className="text-rose-300/80 mt-1">{execution.reason}</div>}
                        {(execution.effects || []).length > 0 && (
                          <div className="font-mono text-white/30 mt-1">
                            {execution.effects
                              .map(
                                (effect) =>
                                  effect.kind + ':' + (effect.target || '') + (effect.amount ? '=' + effect.amount : '')
                              )
                              .join('，')}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>

              {sim && (
                <div className="rounded-2xl border border-white/10 bg-white/[0.02] p-4">
                  <div className="text-[10px] font-mono uppercase tracking-[0.2em] text-white/40 mb-2">
                    最近一次事件模拟：{sim.eventId}
                  </div>
                  <pre className="text-[11px] text-white/60 whitespace-pre-wrap break-all max-h-64 overflow-auto">
                    {JSON.stringify(sim.result, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
