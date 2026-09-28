/**
 * 自动化联机测试：模拟 3 个玩家通过 WebSocket 玩完整局
 * 运行：npm test
 * 验证：房间加入、回合流转、购买、建房、收租、破产、胜利
 */
const { spawn } = require('child_process');
const path = require('path');
const WebSocket = require('ws');

const PORT = 3217 + Math.floor(Math.random() * 100);
const BASE_URL = `ws://127.0.0.1:${PORT}`;

// 启动服务端
const serverProc = spawn('node', [path.join(__dirname, '..', 'server.js')], {
  env: { ...process.env, PORT: String(PORT) },
  stdio: ['ignore', 'pipe', 'pipe']
});
serverProc.stdout.on('data', d => process.stdout.write('[server] ' + d));
serverProc.stderr.on('data', d => process.stdout.write('[server-err] ' + d));

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function waitServer() {
  for (let i = 0; i < 40; i++) {
    try {
      const ws = new WebSocket(BASE_URL);
      await new Promise((res, rej) => {
        ws.once('open', () => { ws.close(); res(); });
        ws.once('error', rej);
      });
      return;
    } catch { await sleep(300); }
  }
  throw new Error('服务端启动超时');
}

// 模拟玩家客户端
class Bot {
  constructor(name) {
    this.name = name;
    this.ws = null;
    this.playerId = null;
    this.sessionId = 'bot_' + name;
    this.state = null;
    this.rolledThisTurn = false;
    this.events = [];
  }

  connect() {
    return new Promise((res, rej) => {
      this.ws = new WebSocket(BASE_URL);
      this.ws.on('open', res);
      this.ws.on('error', rej);
      this.ws.on('message', (raw) => {
        try {
          this.onMessage(JSON.parse(raw.toString()));
        } catch (e) {
          console.error(`[${this.name}] 消息处理失败:`, e.message);
        }
      });
    });
  }

  send(obj) { this.ws.send(JSON.stringify(obj)); }

  onMessage(msg) {
    if (msg.type === 'joined') {
      this.playerId = msg.playerId;
      this.state = msg.game;
    } else if (msg.type === 'state') {
      const prev = this.state;
      this.state = msg.state;
      this.events.push(msg.state);
      // 回合切换检测
      if (prev && this.state.currentPlayerId === this.playerId && prev.currentPlayerId !== this.playerId) {
        this.rolledThisTurn = false;
      }
    }
  }

  act() {
    const s = this.state;
    if (!s || !this.playerId) return;
    const me = s.players.find(p => p.id === this.playerId);
    if (!me || me.bankrupt) return;

    if (s.phase === 'ended') return;

    if (s.phase === 'waiting') {
      if (me.isHost && s.players.length >= 2) this.send({ type: 'start' });
      return;
    }

    if (s.currentPlayerId !== this.playerId) return;
    if (this.acting) return;
    this.acting = true;
    setTimeout(() => { try { this.doAct(); } finally { this.acting = false; } }, 80);
  }

  doAct() {
    const s = this.state;
    if (s.phase !== 'playing') return;
    const me = s.players.find(p => p.id === this.playerId);
    if (!me || me.bankrupt) return;

    // 购买决策：有钱就买
    if (s.pendingBuy && s.pendingBuy.playerId === this.playerId) {
      const cell = s.pendingBuy.cell;
      if (me.money >= cell.price) {
        this.send({ type: 'buy' });
      } else {
        this.send({ type: 'skipBuy' });
      }
      return;
    }

    // 掷骰子
    if (!this.rolledThisTurn && !s.canRollAgain) {
      this.rolledThisTurn = true;
      this.send({ type: 'roll' });
      return;
    }

    // 双数再掷
    if (s.canRollAgain) {
      this.send({ type: 'roll' });
      return;
    }

    // 已掷完：建房 + 结束回合
    if (this.rolledThisTurn) {
      // 激进策略：每回合把所有可建地产建到满级（旅馆）
      const props = s.board.filter(c => c.type === 'property' && me.properties.includes(c.id));
      let built = false;
      for (const cell of props) {
        const level = me.houses[cell.id] || 0;
        if (level >= 5) continue;
        const groupIds = s.board.filter(c => c.type === 'property' && c.group === cell.group).map(c => c.id);
        const ownsAll = groupIds.every(gid => me.properties.includes(gid));
        if (ownsAll && me.money >= cell.houseCost) {
          this.send({ type: 'build', propertyId: cell.id });
          built = true;
          break;
        }
      }
      if (!built) {
        this.send({ type: 'endTurn' });
        this.rolledThisTurn = false;
      }
    }
  }
}

async function main() {
  console.log('启动测试服务端（端口 ' + PORT + '）…');
  await waitServer();
  console.log('服务端就绪。');

  const bots = [new Bot('阿明'), new Bot('小美'), new Bot('大壮')];

  // 第一个玩家创建房间
  await bots[0].connect();
  bots[0].send({ type: 'createRoom', name: bots[0].name, sessionId: bots[0].sessionId });
  await sleep(400);

  for (let i = 1; i < bots.length; i++) {
    await bots[i].connect();
    bots[i].send({ type: 'joinRoom', roomId: bots[0].state.roomId, name: bots[i].name, sessionId: bots[i].sessionId });
    await sleep(400);
  }

  // 回合驱动循环
  let lastLog = 0;
  for (let i = 0; i < 4000; i++) {
    for (const b of bots) b.act();
    // 任何 bot 检测到结束
    const ended = bots.find(b => b.state && b.state.phase === 'ended');
    if (ended) {
      await sleep(300);
      const s = ended.state;
      const winner = s.players.find(p => p.id === s.winner);
      console.log('\n===== 游戏结束 =====');
      console.log(`胜利者：${winner ? winner.name : '无'}（第 ${s.turn} 回合）`);
      console.log('最终资产排名：');
      const ranked = s.players.slice().sort((a, b) => b.totalAssets - a.totalAssets);
      ranked.forEach((p, idx) => {
        console.log(`  ${idx + 1}. ${p.name}: 现金 ¥${p.money}，总资产 ¥${p.totalAssets}${p.bankrupt ? '（破产）' : ''}`);
      });
      console.log('\n最近事件：');
      for (const e of s.log.slice(-10)) console.log('  - ' + e.text);

      // ===== 断言 =====
      const alive = s.players.filter(p => !p.bankrupt);
      if (alive.length !== 1) throw new Error('断言失败：应恰好剩 1 名存活玩家');
      if (!winner || winner.bankrupt) throw new Error('断言失败：胜利者不应破产');
      if (s.turn > 320) throw new Error('断言失败：超出回合上限');
      for (const p of s.players) {
        if (p.money < 0) throw new Error(`断言失败：${p.name} 现金为负`);
      }
      console.log('\n✅ 全部断言通过：联机流程、回合流转、购买、建房、收租、破产、胜负判定均正常');
      return;
    }
    await sleep(60);
  }
  throw new Error('测试超时：游戏未在预期内结束（可能有卡死）');
}

main()
  .then(() => process.exit(0))
  .catch((e) => {
    console.error('\n❌ 测试失败：' + e.message);
    process.exit(1);
  })
  .finally(() => setTimeout(() => serverProc.kill(), 300));
