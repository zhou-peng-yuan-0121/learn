/**
 * 大富翁核心游戏逻辑（服务端权威执行）
 * 纯逻辑模块，不依赖任何网络库，可独立测试。
 */

// ==================== 棋盘定义（经典大富翁 40 格） ====================

// 颜色组
const GROUPS = [
  { name: '棕色组', color: '#955436' },
  { name: '浅蓝组', color: '#88d8f0' },
  { name: '粉色组', color: '#e0669c' },
  { name: '橙色组', color: '#f08c3c' },
  { name: '红色组', color: '#e33b3b' },
  { name: '黄色组', color: '#f5d53b' },
  { name: '绿色组', color: '#2fa94f' },
  { name: '深蓝组', color: '#3b62d8' }
];

// 每格定义：
// type: go | property | station | utility | chance | tax | jail | visit | parking | gojail
// property: group 颜色组下标, price, rent[6]（空地/1房/2房/3房/4房/旅馆）, houseCost
// station/utility: 按拥有数量计费
const BOARD = [
  { id: 0,  name: '起点', type: 'go', icon: 'GO' },
  { id: 1,  name: '地中海大道', type: 'property', group: 0, price: 60,  rent: [2, 10, 30, 90, 160, 250], houseCost: 50 },
  { id: 2,  name: '机会卡', type: 'chance' },
  { id: 3,  name: '波罗的海大道', type: 'property', group: 0, price: 60,  rent: [4, 20, 60, 180, 320, 450], houseCost: 50 },
  { id: 4,  name: '所得税', type: 'tax', amount: 200 },
  { id: 5,  name: '国王车站', type: 'station', price: 200 },
  { id: 6,  name: '东方大道', type: 'property', group: 1, price: 100, rent: [6, 30, 90, 270, 400, 550], houseCost: 50 },
  { id: 7,  name: '机会卡', type: 'chance' },
  { id: 8,  name: '佛蒙特大道', type: 'property', group: 1, price: 100, rent: [6, 30, 90, 270, 400, 550], houseCost: 50 },
  { id: 9,  name: '康涅狄格大道', type: 'property', group: 1, price: 120, rent: [8, 40, 100, 300, 450, 600], houseCost: 50 },
  { id: 10, name: '监狱（探访）', type: 'visit', icon: '🔒' },
  { id: 11, name: '圣查尔斯大道', type: 'property', group: 2, price: 140, rent: [10, 50, 150, 450, 625, 750], houseCost: 100 },
  { id: 12, name: '电力公司', type: 'utility', price: 150 },
  { id: 13, name: '州大道', type: 'property', group: 2, price: 140, rent: [10, 50, 150, 450, 625, 750], houseCost: 100 },
  { id: 14, name: '弗吉尼亚大道', type: 'property', group: 2, price: 160, rent: [12, 60, 180, 500, 700, 900], houseCost: 100 },
  { id: 15, name: '州立车站', type: 'station', price: 200 },
  { id: 16, name: '圣詹姆斯大道', type: 'property', group: 3, price: 180, rent: [14, 70, 200, 550, 750, 950], houseCost: 100 },
  { id: 17, name: '机会卡', type: 'chance' },
  { id: 18, name: '田纳西大道', type: 'property', group: 3, price: 180, rent: [14, 70, 200, 550, 750, 950], houseCost: 100 },
  { id: 19, name: '纽约大道', type: 'property', group: 3, price: 200, rent: [16, 80, 220, 600, 800, 1000], houseCost: 100 },
  { id: 20, name: '免费停车', type: 'parking', icon: '🅿️' },
  { id: 21, name: '肯塔基大道', type: 'property', group: 4, price: 220, rent: [18, 90, 250, 700, 875, 1050], houseCost: 150 },
  { id: 22, name: '机会卡', type: 'chance' },
  { id: 23, name: '印第安纳大道', type: 'property', group: 4, price: 220, rent: [18, 90, 250, 700, 875, 1050], houseCost: 150 },
  { id: 24, name: '伊利诺伊大道', type: 'property', group: 4, price: 240, rent: [20, 100, 300, 750, 925, 1100], houseCost: 150 },
  { id: 25, name: '波尔车站', type: 'station', price: 200 },
  { id: 26, name: '大西洋大道', type: 'property', group: 5, price: 260, rent: [22, 110, 330, 800, 975, 1150], houseCost: 150 },
  { id: 27, name: '弗农大道', type: 'property', group: 5, price: 260, rent: [22, 110, 330, 800, 975, 1150], houseCost: 150 },
  { id: 28, name: '自来水公司', type: 'utility', price: 150 },
  { id: 29, name: '马文花园大道', type: 'property', group: 5, price: 280, rent: [24, 120, 360, 850, 1025, 1200], houseCost: 150 },
  { id: 30, name: '入狱', type: 'gojail', icon: '🚔' },
  { id: 31, name: '太平洋大道', type: 'property', group: 6, price: 300, rent: [26, 130, 390, 900, 1100, 1275], houseCost: 200 },
  { id: 32, name: '北卡罗来纳大道', type: 'property', group: 6, price: 300, rent: [26, 130, 390, 900, 1100, 1275], houseCost: 200 },
  { id: 33, name: '机会卡', type: 'chance' },
  { id: 34, name: '宾夕法尼亚大道', type: 'property', group: 6, price: 320, rent: [28, 150, 450, 1000, 1200, 1400], houseCost: 200 },
  { id: 35, name: '短线车站', type: 'station', price: 200 },
  { id: 36, name: '机会卡', type: 'chance' },
  { id: 37, name: '公园大道', type: 'property', group: 7, price: 350, rent: [35, 175, 500, 1100, 1300, 1500], houseCost: 200 },
  { id: 38, name: '奢侈税', type: 'tax', amount: 100 },
  { id: 39, name: '五月花大道', type: 'property', group: 7, price: 400, rent: [50, 200, 600, 1400, 1700, 2000], houseCost: 200 }
];

// ==================== 机会卡池 ====================
const CHANCE_CARDS = [
  { text: '银行分红：获得 200', effect: { money: 200 } },
  { text: '获得 100 奖金', effect: { money: 100 } },
  { text: '获得 50 零花钱', effect: { money: 50 } },
  { text: '支付意外账单 100', effect: { money: -100 } },
  { text: '支付医疗费 150', effect: { money: -150 } },
  { text: '前进到起点', effect: { goTo: 0 } },
  { text: '前进 3 格', effect: { move: 3 } },
  { text: '后退 3 格', effect: { move: -3 } },
  { text: '前往最近的火车站，若已属他人需付双倍租金', effect: { nearestStation: true } },
  { text: '前往最近的公用事业，若已属他人需付 10 倍租金', effect: { nearestUtility: true } },
  { text: '前往监狱（不领取经过起点的钱）', effect: { jail: true } }
];

// 每局初始资金
const START_MONEY = 1500;
// 过起点奖励
const GO_PASS_BONUS = 200;
// 购买决策倒计时（秒）
const BUY_TIMEOUT = 15;
// 回合数上限（防止无限局）
const MAX_TURNS = 300;
// 监狱规则
const JAIL_BAIL = 50;
const JAIL_MAX_TURNS = 3;

// ==================== 工具函数 ====================
function shuffle(arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function rollDice() {
  return [1 + Math.floor(Math.random() * 6), 1 + Math.floor(Math.random() * 6)];
}

// ==================== 玩家 ====================
function createPlayer(id, name, color) {
  return {
    id,
    name,
    color,
    money: START_MONEY,
    position: 0,
    properties: [],       // 拥有的地产/车站/公用 id
    houses: {},           // { propertyId: 房子数(0-5) }
    inJail: false,
    jailTurns: 0,         // 已蹲的回合数
    doublesStreak: 0,     // 连续掷出双数的次数
    bankrupt: false,
    connected: true,
    isHost: false,
    isBot: false          // AI 机器人
  };
}

// ==================== 游戏主类 ====================
class Game {
  constructor(roomId) {
    this.roomId = roomId;
    this.players = [];
    this.currentIndex = 0;
    this.phase = 'waiting';       // waiting | playing | ended
    this.turn = 0;                // 已执行回合数
    this.dice = [1, 1];
    this.canRollAgain = false;    // 掷出双数后可再掷
    this.movedThisTurn = false;   // 本回合是否已掷过骰子
    this.pendingBuy = null;       // { playerId, propertyId, deadline }
    this.chancePile = shuffle(CHANCE_CARDS);
    this.winner = null;
    this.logs = [];
    this.startedAt = null;
    this.lastActiveAt = Date.now();
  }

  // ---------- 基础 ----------
  log(msg) {
    const entry = { t: Date.now(), text: msg };
    this.logs.push(entry);
    if (this.logs.length > 200) this.logs.shift();
    return entry;
  }

  addPlayer(name, color, isBot = false) {
    if (this.phase !== 'waiting') throw new Error('游戏已开始，无法加入');
    if (this.players.length >= 6) throw new Error('房间已满（最多 6 人）');
    const id = 'p' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    const p = createPlayer(id, name, color);
    p.isBot = isBot;
    p.isHost = this.players.length === 0 && !isBot;
    this.players.push(p);
    this.log(`${name} ${isBot ? '（AI）' : ''}加入了游戏`);
    return p;
  }

  removePlayer(playerId) {
    const i = this.players.findIndex(p => p.id === playerId);
    if (i < 0) return;
    const [removed] = this.players.splice(i, 1);
    this.log(`${removed.name} 离开了房间`);
    // 房主移交给第一个真人玩家
    const nextHost = this.players.find(p => !p.isBot);
    if (nextHost) nextHost.isHost = true;
    if (this.phase === 'playing') this._checkGameOver();
  }

  canStart() {
    return this.phase === 'waiting' && this.players.length >= 2;
  }

  start() {
    if (this.phase !== 'waiting') throw new Error('游戏已开始');
    if (this.players.length < 2) throw new Error('至少需要 2 名玩家才能开始');
    this.phase = 'playing';
    this.startedAt = Date.now();
    this.currentIndex = 0;
    this.turn = 1;
    this.log('游戏开始！由 ' + this.players[0].name + ' 先行');
    this.log(`${this.players[0].name} 掷出骰子开始回合`);
  }

  get currentPlayer() {
    return this.players[this.currentIndex];
  }

  activePlayers() {
    return this.players.filter(p => !p.bankrupt);
  }

  // ---------- 掷骰子 ----------
  roll(playerId) {
    if (this.phase !== 'playing') throw new Error('游戏尚未开始');
    const p = this.players.find(x => x.id === playerId);
    if (!p || p.bankrupt) throw new Error('无效的玩家');
    if (this.players[this.currentIndex].id !== playerId) throw new Error('还没轮到你');
    if (this.pendingBuy) throw new Error('请先处理购买决定');
    // 本回合已掷过且非双数（不允许重复掷）
    if (this.movedThisTurn && !this.canRollAgain) throw new Error('本回合已掷过骰子，请结束回合');

    this.dice = rollDice();
    const sum = this.dice[0] + this.dice[1];
    const isDouble = this.dice[0] === this.dice[1];
    this.movedThisTurn = true;
    this.log(`${p.name} 掷出 ${this.dice[0]} + ${this.dice[1]} = ${sum}`);

    // 监狱处理
    if (p.inJail) {
      if (isDouble) {
        p.inJail = false;
        p.jailTurns = 0;
        p.doublesStreak = 0;
        this.log(`${p.name} 掷出双数，成功出狱！`);
      } else {
        p.jailTurns++;
        this.log(`${p.name} 未能掷出双数，继续留在监狱（第 ${p.jailTurns} 次尝试）`);
        if (p.jailTurns >= JAIL_MAX_TURNS) {
          p.inJail = false;
          p.jailTurns = 0;
          p.doublesStreak = 0;
          this._pay(p, JAIL_BAIL, null, '强制保释金');
          this.log(`${p.name} 三次未掷出双数，支付 ${JAIL_BAIL} 强制出狱`);
        } else {
          // 监狱中未出狱：回合结束
          this._endTurnInner();
          return;
        }
      }
    }

    // 非监狱移动
    if (!p.inJail) {
      p.position = (p.position + sum) % 40;
      // 经过起点（跨过或恰好落在 0）
      const crossedGo = this._crossedGo(p, sum);
      if (crossedGo && p.position !== 0) {
        p.money += GO_PASS_BONUS;
        this.log(`${p.name} 经过起点，获得 ${GO_PASS_BONUS}`);
      }
      if (p.position === 0) {
        p.money += GO_PASS_BONUS;
        this.log(`${p.name} 到达起点，获得 ${GO_PASS_BONUS}`);
      }
    }

    // 连续双数：三次入狱
    if (isDouble && !p.inJail) {
      p.doublesStreak++;
      if (p.doublesStreak >= 3) {
        this.log(`${p.name} 连续三次掷出双数，直接入狱！`);
        this.goToJail(p);
        this.canRollAgain = false;
        this._endTurnInner();
        return;
      }
    } else {
      p.doublesStreak = 0;
    }

    // 落地结算
    const settled = this._settleLanding(p);
    if (settled) return; // 已入狱/破产等，回合已处理

    // 双数可以再掷
    this.canRollAgain = isDouble && !p.inJail;
    if (this.canRollAgain) {
      this.log(`${p.name} 掷出双数，可以再掷一次！`);
    } else {
      this.log(`${p.name} 可以结束回合`);
    }
    this._checkGameOver();
  }

  // 是否跨过起点（0 号格）
  _crossedGo(p, steps) {
    // 若移动前位置 + steps 超过 40，或恰好落在 0 由落点处理
    return p.position + steps >= 40;
  }

  // ---------- 落地结算 ----------
  _settleLanding(p) {
    const cell = BOARD[p.position];
    switch (cell.type) {
      case 'go':
        // 已由经过处理
        break;
      case 'property':
        return this._handleProperty(p, cell);
      case 'station':
        return this._handleStation(p, cell);
      case 'utility':
        return this._handleUtility(p, cell);
      case 'tax':
        this._pay(p, cell.amount, null, `缴纳${cell.name}`);
        break;
      case 'chance':
        this._drawChance(p);
        break;
      case 'gojail':
        this.log(`${p.name} 被警察带走，入狱！`);
        this.goToJail(p);
        return true;
      case 'jail':
      case 'visit':
      case 'parking':
        this.log(`${p.name} 停留在 ${cell.name}`);
        break;
    }
    return false;
  }

  _handleProperty(p, cell) {
    const owner = this.players.find(x => x.properties.includes(cell.id) && !x.bankrupt);
    if (!owner) {
      // 无主 → 发起购买
      this.pendingBuy = { playerId: p.id, propertyId: cell.id, deadline: Date.now() + BUY_TIMEOUT * 1000 };
      this.log(`${p.name} 可以购买 ${cell.name}（价格 ${cell.price}）`);
      return false;
    }
    if (owner.id === p.id) {
      this.log(`${p.name} 回到自己的地产 ${cell.name}`);
      return false;
    }
    const rent = this.getRent(cell.id, owner);
    this.log(`${p.name} 支付给 ${owner.name} ${rent} 租金（${cell.name}）`);
    this._pay(p, rent, owner);
    return false;
  }

  _handleStation(p, cell) {
    const owner = this.players.find(x => x.properties.includes(cell.id) && !x.bankrupt);
    if (!owner) {
      this.pendingBuy = { playerId: p.id, propertyId: cell.id, deadline: Date.now() + BUY_TIMEOUT * 1000 };
      this.log(`${p.name} 可以购买 ${cell.name}（价格 ${cell.price}）`);
      return false;
    }
    if (owner.id === p.id) return false;
    const count = owner.properties.filter(id => BOARD[id].type === 'station').length;
    const rent = [0, 25, 50, 100, 200][count] || 200;
    this.log(`${p.name} 支付给 ${owner.name} ${rent} 车站租金（${owner.name} 拥有 ${count} 个车站）`);
    this._pay(p, rent, owner);
    return false;
  }

  _handleUtility(p, cell) {
    const owner = this.players.find(x => x.properties.includes(cell.id) && !x.bankrupt);
    if (!owner) {
      this.pendingBuy = { playerId: p.id, propertyId: cell.id, deadline: Date.now() + BUY_TIMEOUT * 1000 };
      this.log(`${p.name} 可以购买 ${cell.name}（价格 ${cell.price}）`);
      return false;
    }
    if (owner.id === p.id) return false;
    const count = owner.properties.filter(id => BOARD[id].type === 'utility').length;
    const diceSum = this.dice[0] + this.dice[1];
    const rent = count >= 2 ? diceSum * 10 : diceSum * 4;
    this.log(`${p.name} 支付给 ${owner.name} ${rent} 公用事业费（骰子 ${diceSum} × ${count >= 2 ? 10 : 4}）`);
    this._pay(p, rent, owner);
    return false;
  }

  // ---------- 购买 ----------
  buy(playerId) {
    if (!this.pendingBuy) throw new Error('当前没有可购买的地产');
    if (this.pendingBuy.playerId !== playerId) throw new Error('购买权属于当前玩家');
    const p = this.players.find(x => x.id === playerId);
    if (!p || p.bankrupt) throw new Error('无效玩家');
    const cell = BOARD[this.pendingBuy.propertyId];
    if (p.money < cell.price) {
      this.log(`${p.name} 现金不足（${p.money} < ${cell.price}），无法购买 ${cell.name}`);
      this.pendingBuy = null;
      this._checkGameOver();
      return;
    }
    p.money -= cell.price;
    p.properties.push(cell.id);
    p.houses[cell.id] = 0;
    this.log(`${p.name} 购买了 ${cell.name}（花费 ${cell.price}）`);
    this.pendingBuy = null;
    this._checkGameOver();
  }

  skipBuy(playerId) {
    if (!this.pendingBuy) throw new Error('当前没有可购买的地产');
    if (this.pendingBuy.playerId !== playerId) throw new Error('购买权属于当前玩家');
    const cell = BOARD[this.pendingBuy.propertyId];
    const p = this.players.find(x => x.id === playerId);
    this.log(`${p ? p.name : '玩家'} 放弃了购买 ${cell.name}`);
    this.pendingBuy = null;
    this._checkGameOver();
  }

  // ---------- 建房 / 卖房 ----------
  canBuild(player, propertyId) {
    const cell = BOARD[propertyId];
    if (!cell || cell.type !== 'property') return { ok: false, reason: '该格不可建房' };
    if (!player.properties.includes(propertyId)) return { ok: false, reason: '你不拥有该地产' };
    // 同色组必须全部拥有
    const groupIds = BOARD.filter(c => c.type === 'property' && c.group === cell.group).map(c => c.id);
    const ownsAll = groupIds.every(id => player.properties.includes(id));
    if (!ownsAll) return { ok: false, reason: '需拥有整套颜色组才能建房' };
    return { ok: true, groupIds };
  }

  buildHouse(playerId, propertyId) {
    if (this.phase !== 'playing') throw new Error('游戏尚未开始');
    const p = this.players.find(x => x.id === playerId);
    if (!p || p.bankrupt) throw new Error('无效玩家');
    if (this.players[this.currentIndex].id !== playerId) throw new Error('还没轮到你');
    if (this.pendingBuy) throw new Error('请先处理购买决定');
    const check = this.canBuild(p, propertyId);
    if (!check.ok) throw new Error(check.reason);
    const cell = BOARD[propertyId];
    const level = p.houses[propertyId] || 0;
    if (level >= 5) throw new Error('该地产已建满旅馆');
    if (p.money < cell.houseCost) throw new Error('现金不足，无法建房');
    p.money -= cell.houseCost;
    p.houses[propertyId] = level + 1;
    this.log(`${p.name} 在 ${cell.name} 上建造了${level + 1 >= 5 ? '旅馆' : `第 ${level + 1} 栋房子`}（花费 ${cell.houseCost}）`);
    this._checkGameOver();
  }

  sellHouse(playerId, propertyId) {
    const p = this.players.find(x => x.id === playerId);
    if (!p || p.bankrupt) throw new Error('无效玩家');
    if (this.players[this.currentIndex].id !== playerId) throw new Error('还没轮到你');
    if (this.pendingBuy) throw new Error('请先处理购买决定');
    const level = p.houses[propertyId] || 0;
    if (level <= 0) throw new Error('该地产没有房子');
    const cell = BOARD[propertyId];
    p.houses[propertyId] = level - 1;
    p.money += Math.floor(cell.houseCost / 2);
    this.log(`${p.name} 卖掉了 ${cell.name} 上的一栋房子（收回 ${Math.floor(cell.houseCost / 2)}）`);
  }

  // ---------- 租金 ----------
  getRent(propertyId, owner) {
    const cell = BOARD[propertyId];
    const level = owner.houses[propertyId] || 0;
    if (level > 0) return cell.rent[level];
    // 空地：完整色组翻倍
    const groupIds = BOARD.filter(c => c.type === 'property' && c.group === cell.group).map(c => c.id);
    const ownsAll = groupIds.every(id => owner.properties.includes(id));
    return ownsAll ? cell.rent[0] * 2 : cell.rent[0];
  }

  // ---------- 付款与破产 ----------
  _pay(payer, amount, to, reason) {
    if (amount <= 0) return;
    if (payer.money >= amount) {
      payer.money -= amount;
      if (to) to.money += amount;
      return;
    }
    // 破产：不足支付
    payer.money = 0;
    payer.bankrupt = true;
    const remaining = amount - payer.money; // 即 amount
    if (to) {
      // 抵债：地产移交给债主
      for (const id of payer.properties.slice()) {
        to.properties.push(id);
        to.houses[id] = to.houses[id] || 0;
        payer.properties.splice(payer.properties.indexOf(id), 1);
        delete payer.houses[id];
      }
    }
    // 清空地皮（归银行，无主）
    for (const id of payer.properties.slice()) {
      payer.properties.splice(payer.properties.indexOf(id), 1);
      delete payer.houses[id];
    }
    payer.inJail = false;
    this.log(`${payer.name} 破产出局${to ? `，地产移交给了 ${to.name}` : ''}！${reason ? `（${reason}）` : ''}`);
    // 清理未决购买
    if (this.pendingBuy && this.pendingBuy.playerId === payer.id) this.pendingBuy = null;
  }

  goToJail(p) {
    p.inJail = true;
    p.jailTurns = 0;
    p.position = 10;
    p.doublesStreak = 0;
  }

  // ---------- 机会卡 ----------
  _drawChance(p) {
    if (this.chancePile.length === 0) this.chancePile = shuffle(CHANCE_CARDS);
    const card = this.chancePile.shift();
    this.log(`${p.name} 抽到机会卡：「${card.text}」`);
    const e = card.effect;
    if (e.money) {
      if (e.money > 0) {
        p.money += e.money;
        this.log(`${p.name} 获得 ${e.money}`);
      } else {
        this._pay(p, -e.money, null, '机会卡费用');
      }
    }
    if (e.goTo !== undefined) {
      p.position = e.goTo;
      if (e.goTo === 0) p.money += GO_PASS_BONUS;
    }
    if (e.move) {
      p.position = (p.position + e.move + 40) % 40;
    }
    if (e.jail) {
      this.goToJail(p);
      return;
    }
    if (e.nearestStation) {
      const stationIds = BOARD.filter(c => c.type === 'station').map(c => c.id);
      const next = this._nearestOf(p.position, stationIds);
      p.position = next;
      const owner = this.players.find(x => x.properties.includes(next) && !x.bankrupt);
      if (owner && owner.id !== p.id) {
        const count = owner.properties.filter(id => BOARD[id].type === 'station').length;
        const rent = [0, 50, 100, 200, 400][count] || 400; // 双倍
        this.log(`${p.name} 支付给 ${owner.name} 双倍车站租金 ${rent}`);
        this._pay(p, rent, owner);
      }
    }
    if (e.nearestUtility) {
      const utilIds = BOARD.filter(c => c.type === 'utility').map(c => c.id);
      const next = this._nearestOf(p.position, utilIds);
      p.position = next;
      const owner = this.players.find(x => x.properties.includes(next) && !x.bankrupt);
      if (owner && owner.id !== p.id) {
        const count = owner.properties.filter(id => BOARD[id].type === 'utility').length;
        const rent = (this.dice[0] + this.dice[1]) * (count >= 2 ? 20 : 10); // 10倍
        this.log(`${p.name} 支付给 ${owner.name} ${rent} 公用事业费`);
        this._pay(p, rent, owner);
      }
    }
    // 抽卡后可能再次落地需要结算（简化：机会卡不触发二次购买/租金结算，只执行移动效果）
  }

  _nearestOf(from, ids) {
    let best = ids[0], bestDist = 999;
    for (const id of ids) {
      let d = (id - from + 40) % 40;
      if (d <= 0) d += 40;
      if (d < bestDist) { bestDist = d; best = id; }
    }
    return best;
  }

  // ---------- 回合控制 ----------
  endTurn(playerId) {
    if (this.phase !== 'playing') throw new Error('游戏尚未开始');
    const p = this.players.find(x => x.id === playerId);
    if (!p || p.bankrupt) throw new Error('无效玩家');
    if (this.players[this.currentIndex].id !== playerId) throw new Error('还没轮到你');
    if (this.pendingBuy) throw new Error('请先处理购买决定');
    if (this.canRollAgain) throw new Error('掷出双数，必须先再掷一次');
    if (!this.movedThisTurn) throw new Error('请先掷骰子');
    this._endTurnInner();
  }

  _endTurnInner() {
    if (this.phase !== 'playing') return;
    // 推进到下一个未破产玩家
    let guard = 0;
    do {
      this.currentIndex = (this.currentIndex + 1) % this.players.length;
      this.canRollAgain = false;
      this.movedThisTurn = false;
      this.pendingBuy = null;
      guard++;
    } while (this.players[this.currentIndex].bankrupt && guard < this.players.length * 2);

    // 回合计数：回到第一个玩家 = 完成一整轮
    if (this.currentIndex === 0) {
      this.turn++;
      if (this.turn > MAX_TURNS) {
        this._finishByTurns();
        return;
      }
    }
    const cp = this.currentPlayer;
    this.log(`轮到 ${cp.name}（第 ${this.turn} 回合）`);
    this.lastActiveAt = Date.now();

    // 断线玩家自动回合
    if (!cp.connected) {
      this._autoPlayDisconnected(cp);
    }
  }

  _autoPlayDisconnected(p) {
    // 自动掷骰子
    this.canRollAgain = false;
    this.pendingBuy = null;
    // 简单处理：掷骰并走完整流程，自动跳过购买
    const oldPending = this.pendingBuy;
    try {
      this.roll(p.id);
    } catch (e) { /* 忽略 */ }
    if (this.pendingBuy) {
      this.skipBuy(p.id);
    }
    if (this.canRollAgain) {
      // 自动再次掷骰（最多处理一次再掷）
      this.canRollAgain = false;
      try { this.roll(p.id); } catch (e) {}
      if (this.pendingBuy) this.skipBuy(p.id);
    }
    this.canRollAgain = false;
    this._endTurnInner();
  }

  _finishByTurns() {
    const alive = this.activePlayers();
    if (alive.length === 0) return;
    alive.sort((a, b) => this.totalAssets(b) - this.totalAssets(a));
    this.winner = alive[0];
    this.phase = 'ended';
    this.log(`达到回合上限，按总资产排名：${alive.map((p, i) => `第${i + 1}名 ${p.name}`).join('，')}`);
    this.log(`最终胜利者：${this.winner.name}！`);
  }

  _checkGameOver() {
    if (this.phase !== 'playing') return;
    const alive = this.activePlayers();
    if (alive.length <= 1) {
      this.phase = 'ended';
      this.winner = alive[0] || null;
      this.log(alive[0] ? `游戏结束！${alive[0].name} 获胜！` : '游戏结束，没有获胜者');
      return true;
    }
    return false;
  }

  totalAssets(p) {
    let assets = p.money;
    for (const id of p.properties) {
      const cell = BOARD[id];
      if (cell.type === 'property') {
        assets += cell.price + (p.houses[id] || 0) * cell.houseCost;
      } else {
        assets += cell.price;
      }
    }
    return assets;
  }

  // 检查过期购买请求（服务端定时调用）
  checkTimeouts() {
    if (this.pendingBuy && Date.now() > this.pendingBuy.deadline) {
      const cell = BOARD[this.pendingBuy.propertyId];
      const p = this.players.find(x => x.id === this.pendingBuy.playerId);
      this.log(`${p ? p.name : '玩家'} 未在限定时间内决定，放弃购买 ${cell.name}`);
      this.pendingBuy = null;
      this._checkGameOver();
    }
    // 长时间无操作的在线玩家：自动继续（防止卡死）
    if (this.phase === 'playing' && !this.pendingBuy && !this.canRollAgain) {
      const cp = this.currentPlayer;
      if (cp && cp.connected && Date.now() - this.lastActiveAt > 60000) {
        this.log(`${cp.name} 长时间未操作，自动结束回合`);
        this._endTurnInner();
      }
    }
  }

  // ---------- 对外状态 ----------
  toPublicState() {
    return {
      roomId: this.roomId,
      phase: this.phase,
      turn: this.turn,
      dice: this.dice,
      canRollAgain: this.canRollAgain,
      movedThisTurn: this.movedThisTurn,
      currentPlayerId: this.phase === 'playing' ? this.currentPlayer.id : null,
      pendingBuy: this.pendingBuy ? { ...this.pendingBuy, cell: BOARD[this.pendingBuy.propertyId] } : null,
      winner: this.winner ? this.winner.id : null,
      players: this.players.map(p => ({
        id: p.id,
        name: p.name,
        color: p.color,
        money: p.money,
        position: p.position,
        properties: p.properties.slice(),
        houses: { ...p.houses },
        inJail: p.inJail,
        jailTurns: p.jailTurns,
        bankrupt: p.bankrupt,
        connected: p.connected,
        isHost: p.isHost,
        isBot: p.isBot,
        totalAssets: this.totalAssets(p)
      })),
      log: this.logs.slice(-30),
      board: BOARD
    };
  }
}

// ==================== 导出 ====================
module.exports = {
  Game,
  BOARD,
  GROUPS,
  START_MONEY,
  GO_PASS_BONUS,
  BUY_TIMEOUT,
  MAX_TURNS,
  JAIL_BAIL,
  PLAYER_COLORS: ['#e74c3c', '#3498db', '#2ecc71', '#f1c40f', '#9b59b6', '#e67e22']
};
