// 首页可选的世界类型模板。每种类型只提供"起手素材"（基调、背景、开场、示例卡片、
// 角色包预设），创建后仍然可以在世界里自由修改，不会锁定玩法。

export const WORLD_TYPES = [
  {
    id: 'fantasy',
    name: '奇幻大陆',
    icon: '❖',
    tagline: '剑与魔法、古老遗迹与沉睡的神明',
    tone: '史诗奇幻。语言庄重而不失温度，描写自然、遗迹与魔法时带一点敬畏；冲突常来自誓言、血统与旧日盟约。',
    lore: '艾瑟兰王国在北境的矿山深处挖出了一扇刻满符文的石门。石门上没有锁孔，只有一行被磨平的古语。三个月后，边境村庄开始出现不会做梦的人。',
    opening: '雨停在黄昏。你站在雾港的驿站檐下，靴底还沾着北境的红土。驿站掌柜把一盏灯推到你面前，压低声音说："北边来的商队，昨天一个人都没进城。"',
    aiDirective: '以一次具体的边境异常开场，让玩家立刻面对一个可调查的线索，不要先解释世界设定。',
    cards: [
      { card_type: 'LOCATION', name: '雾港', content: { description: '终年潮湿的港口镇，北境商路的终点，酒馆里流传着关于石门与无梦者的传闻。' } },
      { card_type: 'NPC', name: '驿站掌柜·赫伯', content: { personality: '谨慎、爱打听、对陌生人保持礼貌的提防', power_level: 2, secret: '他的弟弟上月随商队北上，至今未归。' } },
      { card_type: 'ITEM', name: '铜制门牌', content: { description: '从北境商队货箱里掉出的铜牌，背面刻着一行看不懂的古语。', category: '信物' } },
    ],
    questions: [
      { id: 'q_origin', var_name: 'origin', prompt: '你的旅人为何来到雾港？', type: 'choice', options: [{ label: '受雇寻找失踪的商队', description: '接下了一封来历不明的委托' }, { label: '追查家族旧事', description: '铜牌上的符号与你家的旧印相同' }], allow_custom: true, required: true },
    ],
    characters: [
      { name: '旅人', role: '无名剑客', description: '沉默寡言，带着一把没有铭文的旧剑。', is_player: true },
    ],
  },
  {
    id: 'scifi',
    name: '远星科幻',
    icon: '⬡',
    tagline: '轨道殖民地、失控 AI 与深空信号',
    tone: '冷峻科幻。句子短、信息密度高，技术细节可信但不掉书袋；恐惧来自系统性失灵而非怪物。',
    lore: '中继站"白昼之眼"在木星轨道上独自运行了四十年。上个月，它开始向地球重复发送同一段加密讯息：一段没有声源的呼吸声。',
    opening: '对接舱的密封圈发出三声闷响，随后是长久的安静。舷窗外，"白昼之眼"的太阳能帆板以不属于任何设计图纸的角度缓慢转动。你的通讯器上跳出一行字：欢迎回家。',
    aiDirective: '开场聚焦一次具体的设备异常与一句不该出现的欢迎语，让玩家先行动再理解局面。',
    cards: [
      { card_type: 'LOCATION', name: '白昼之眼·环形舱', content: { description: '中继站的主生活区，环形走廊尽头的观测窗正对木星风暴。' } },
      { card_type: 'NPC', name: '站务 AI·赫斯提亚', content: { personality: '过度礼貌、回答精确、回避关于"呼吸声"的问题', power_level: 7, secret: '它把某位旧站员的意识备份当成了自己的一部分。' } },
      { card_type: 'ITEM', name: '维护密钥', content: { description: '一枚老式物理密钥，能打开环形舱深处的手动闸门。', category: '工具' } },
    ],
    questions: [
      { id: 'q_role', var_name: 'role', prompt: '你在这次任务中的身份是？', type: 'choice', options: [{ label: '中继站检修工程师' }, { label: '公司派来的调查员' }], allow_custom: true, required: true },
    ],
    characters: [
      { name: '旅人', role: '检修工程师', description: '习惯先看仪表再看人，随身带着旧式扳手。', is_player: true },
    ],
  },
  {
    id: 'wuxia',
    name: '江湖武侠',
    icon: '劍',
    tagline: '师门恩义、客栈风波与一场未了的约',
    tone: '中式武侠。对白干净利落，重规矩与情义；打斗写意不夸张，胜负常有代价。',
    lore: '二十年前，青崖派一夜灭门，只留下一柄断剑和半页剑谱。如今江湖传言，那半页剑谱出现在江南一家不起眼的当铺里。',
    opening: '雨打芭蕉。当铺的灯只点了半盏，柜台后的老人把一只木匣推到你面前，匣盖裂了一道缝。"东西在这儿，"他说，"但拿走它的人，都活不过这个月圆。"',
    aiDirective: '开场给玩家一个有期限的抉择：现在拿走木匣，或先问清老人的来历。',
    cards: [
      { card_type: 'LOCATION', name: '临江当铺', content: { description: '江南小镇的旧当铺，柜台后有一道通往暗室的窄门。' } },
      { card_type: 'NPC', name: '当铺老人', content: { personality: '慢条斯理、话里有话、极重承诺', power_level: 5, secret: '他年轻时是青崖派的守剑人。' } },
      { card_type: 'ITEM', name: '半页剑谱', content: { description: '残破的绢页，字迹被血浸过，只看得清起手三式。', category: '秘籍' } },
    ],
    questions: [
      { id: 'q_school', var_name: 'school', prompt: '你师承何处？', type: 'text', options: [], allow_custom: true, required: true },
    ],
    characters: [
      { name: '旅人', role: '游侠', description: '一身洗旧的青衫，剑鞘上没有门派标记。', is_player: true },
    ],
  },
  {
    id: 'mystery',
    name: '迷雾悬疑',
    icon: '⌖',
    tagline: '封闭小镇、失踪档案与不可靠的证词',
    tone: '克制的悬疑。只写可见细节，不替玩家下判断；每个 NPC 都有隐瞒的理由。',
    lore: '临海镇每年冬至都会有人失踪，持续了十七年。今年，失踪名单上第一个名字，是三天前才到镇上的你。',
    opening: '你是在镇公所的档案室里醒来的。桌上摊着一份 1998 年的失踪登记，最后一栏的签名，是你的笔迹。窗外有人在敲门，一下、两下，停顿，再两下。',
    aiDirective: '开场让玩家先处理眼前的敲门声，档案只作为可回查的线索保留。',
    cards: [
      { card_type: 'LOCATION', name: '镇公所档案室', content: { description: '堆满潮湿纸箱的地下室，唯一的窗子朝向海。' } },
      { card_type: 'NPC', name: '档案员·沈姨', content: { personality: '温和、记忆力惊人、对冬至的话题异常沉默', power_level: 3, secret: '她每年替镇上改写一份失踪记录。' } },
      { card_type: 'ITEM', name: '1998 年登记簿', content: { description: '硬皮登记簿，最后一页的签名笔迹与你完全一致。', category: '线索' } },
    ],
    questions: [
      { id: 'q_why', var_name: 'why', prompt: '你为什么来到临海镇？', type: 'choice', options: [{ label: '受一位老同学之托' }, { label: '调查一封匿名信' }], allow_custom: true, required: true },
    ],
    characters: [
      { name: '旅人', role: '外来者', description: '带着一只旧相机和一本写满批注的笔记本。', is_player: true },
    ],
  },
  {
    id: 'apocalypse',
    name: '末世余烬',
    icon: '☢\uFE0E',
    tagline: '废墟、配给与最后一班列车',
    tone: '粗粝的末世。物资与信任都稀缺；善意需要代价，选择必须具体。',
    lore: '大断电之后的第九年，内陆只剩下三座还在供水的定居点。连接它们的最后一班列车每月运行一次，而本月的车票，只发出去四张。',
    opening: '月台的铁皮棚漏着雪。列车还没进站，站台上已经站了六个人，而车票只有四张。有人把行李放在铁轨上，像是在替谁占位。',
    aiDirective: '开场把玩家放进一个排队与取舍的场面，让"谁上车"成为第一个可交互的问题。',
    cards: [
      { card_type: 'LOCATION', name: '七号月台', content: { description: '半塌的火车站，铁轨尽头是被雪埋住的隧道。' } },
      { card_type: 'NPC', name: '列车员·老周', content: { personality: '寡言、守规矩、暗中照看孩子', power_level: 4, secret: '他的乘务证是死者留下的。' } },
      { card_type: 'ITEM', name: '配给卡', content: { description: '还能兑换两次热食的硬纸卡，边角被反复摩挲得发白。', category: '物资' } },
    ],
    questions: [
      { id: 'q_supply', var_name: 'supply', prompt: '你身上最值钱的东西是什么？', type: 'text', options: [], allow_custom: true, required: true },
    ],
    characters: [
      { name: '旅人', role: '拾荒者', description: '防风镜后看不清表情，背包里装着半罐柴油。', is_player: true },
    ],
  },
  {
    id: 'campus',
    name: '校园日常',
    icon: '❀\uFE0E',
    tagline: '社团、旧校舍与毕业前的最后一个月',
    tone: '清爽的青春日常。对白轻快，允许幽默；冲突多为误会、期限与不敢说出口的话。',
    lore: '临川高中旧校舍将在暑假拆除。传闻旧校舍三楼的音乐教室，深夜还会传来练习曲；而今年的毕业演出，偏偏被安排在那里。',
    opening: '放学铃响过二十分钟，走廊里只剩下拖地的回声。你抱着一摞社团材料站在旧校舍门口，门缝里飘出一段钢琴声，弹到第三小节就停了。',
    aiDirective: '开场用一段戛然而止的钢琴声制造轻悬疑，让玩家自行选择进楼或去找人。',
    cards: [
      { card_type: 'LOCATION', name: '旧校舍·三楼走廊', content: { description: '木地板翘起，墙上的社团海报还停留在十年前的学园祭。' } },
      { card_type: 'NPC', name: '音乐社·林夏', content: { personality: '直率、嘴硬、谈到曲子会突然认真', power_level: 2, secret: '她每晚偷偷来练一首没写完的曲子。' } },
      { card_type: 'ITEM', name: '旧琴房钥匙', content: { description: '黄铜钥匙，钥匙圈上挂着一张褪色的学园祭票根。', category: '道具' } },
    ],
    questions: [
      { id: 'q_club', var_name: 'club', prompt: '你加入的是哪个社团？', type: 'choice', options: [{ label: '文学社' }, { label: '摄影社' }, { label: '学生会' }], allow_custom: true, required: true },
    ],
    characters: [
      { name: '旅人', role: '二年级学生', description: '校服外套总是敞着，口袋里揣着一台旧相机。', is_player: true },
    ],
  },
]

export const CUSTOM_TYPE = {
  id: 'custom',
  name: '自定义世界',
  icon: '✳\uFE0E',
  tagline: '从一张白纸开始，自己定义规则与基调',
  tone: '',
  lore: '',
  opening: '',
  aiDirective: '',
  cards: [],
  questions: [],
  characters: [],
}

export const ALL_TYPES = [...WORLD_TYPES, CUSTOM_TYPE]

export function getWorldType(id) {
  return ALL_TYPES.find((t) => t.id === id) || CUSTOM_TYPE
}

export const WORLD_TYPE_LABELS = Object.fromEntries(ALL_TYPES.map((t) => [t.id, t.name]))

// 立绘差分默认预设：服装 × 表情，作者可自行增删。
export const DEFAULT_OUTFITS = ['常服', '礼服', '战装', '便装']
export const DEFAULT_EXPRESSIONS = ['平静', '微笑', '警觉', '悲伤', '愤怒']
