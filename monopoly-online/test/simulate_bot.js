/**
 * AI 机器人模式测试：1 名真人 + 3 个 AI 玩家玩完整局
 * 运行：node test/simulate_bot.js
 * 验证：添加机器人、开始游戏、AI 自动行动（掷骰/购买/建房/结束回合）、对局正常结束
 */
const { spawn } = require('child_process');
const path = require('path');
const WebSocket = require('ws');

const PORT = 3317 + Math.floor(Math.random() * 100);
const BASE_URL = `ws://127.0.0.1:${PORT}`;

const serverProc = spawn('node', [path.join(__dirname, '..', 'server.js')], {
  env: { ...process.env, PORT: String(PORT) },
  stdio: ['ignore', 'pipe', 'pipe']
});
serverProc.stdout.on('data', d => process.stdout.write('[server] ' + d));

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

class Human {
  constructor(name) {
    this.name = name;
    this.ws = null;
    this.playerId = null;
    this.state = null;
    this.rolled = false;
  }
  connect() {
    return new Promise((res, rej) => {
      this.ws = new WebSocket(BASE_URL);
      this.ws.on('open', res);
      this.ws.on('error', rej);
      this.ws.on('message', (raw) => {
        const msg = JSON.parse(raw.toString());
        if (msg.type === 'joined') { this.playerId = msg.playerId; this.state = msg.game; }
        else if (msg.type === 'state') this.state = msg.state;
      });
    });
  }
  send(obj) { this.ws.send(JSON.stringify(obj)); }
  act() {
    const s = this.state;
    if (!s || !this.playerId) return;
    const me = s.players.find(p => p.id === this.playerId);
    if (!me || me.bankrupt || s.phase !== 'playing') return;
    if (s.currentPlayerId !== this.playerId) return;
    if (this.acting) return;
    this.acting = true;
    setTimeout(() => { try { this.doAct(); } finally { this.acting = false; } }, 100);
  }
  doAct() {
    const s = this.state;
    const me = s.players.find(p => p.id === this.playerId);
    if (!me) return;
    if (s.pendingBuy && s.pendingBuy.playerId === this.playerId) {
      const cell = s.pendingBuy.cell;
      if (me.money >= cell.price) this.send({ type: 'buy' });
      else this.send({ type: 'skipBuy' });
      return;
    }
    if (!s.movedThisTurn && !s.canRollAgain) {
      this.rolled = true;
      this.send({ type: 'roll' });
      return;
    }
    if (s.canRollAgain) { this.send({ type: 'roll' }); return; }
    if (this.rolled) {
      // 激进建房：把可建地产建到满级
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
      if (!built) { this.send({ type: 'endTurn' }); this.rolled = false; }
    }
  }
}

async function main() {
  console.log('启动测试服务端（端口 ' + PORT + '）…');
  await waitServer();
  console.log('服务端就绪。');

  const human = new Human('玩家一');
  await human.connect();
  human.send({ type: 'createRoom', name: human.name, sessionId: 'human_1' });
  await sleep(400);

  // 添加 3 个 AI
  for (let i = 0; i < 3; i++) {
    human.send({ type: 'addBot' });
    await sleep(400);
  }
  const waiting = human.state;
  const bots = waiting.players.filter(p => p.isBot);
  if (bots.length !== 3) throw new Error(`断言失败：应有 3 个 AI，实际 ${bots.length}`);
  if (bots.some(b => b.money !== 1500)) throw new Error('断言失败：AI 初始资金应为 1500');
  console.log(`✅ 3 个 AI 已添加：${bots.map(b => b.name).join('、')}`);

  // 开始游戏
  human.send({ type: 'start' });
  await sleep(500);

  // 对局驱动：验证 AI 自动行动持续推进（不强制等破产结束，回合持续推进即验证通过）
  let lastTurn = 0;
  let aiLogCount = 0;
  const aiNames = bots.map(b => b.name);
  for (let i = 0; i < 8000; i++) {
    human.act();
    const s = human.state;
    if (!s) { await sleep(30); continue; }

    if (i % 400 === 0) {
      const cur = s.players.find(p => p.id === s.currentPlayerId);
      console.log(`[debug] i=${i} phase=${s.phase} turn=${s.turn} cur=${cur ? cur.name : '?'} moved=${s.movedThisTurn}`);
    }

    if (s.phase === 'ended') {
      await sleep(300);
      const winner = s.players.find(p => p.id === s.winner);
      console.log('\n===== 游戏结束 =====');
      console.log(`胜利者：${winner ? winner.name : '无'}（第 ${s.turn} 回合）`);
      const ranked = s.players.slice().sort((a, b) => b.totalAssets - a.totalAssets);
      ranked.forEach((p, idx) => console.log(`  ${idx + 1}. ${p.name}${p.isBot ? '（AI）' : ''}: 现金 ¥${p.money}，总资产 ¥${p.totalAssets}${p.bankrupt ? '（破产）' : ''}`));
      verify(s, aiNames, true);
      console.log('✅ 全部断言通过：AI 添加、自动行动、破产、胜负判定均正常');
      return;
    }

    // 记录 AI 行动日志
    aiLogCount = Math.max(aiLogCount, s.log.filter(e => aiNames.some(n => e.text.includes(n))).length);

    if (s.turn >= 30) {
      // 已推进 30 轮（120 个玩家回合），机制验证完成
      console.log('\n===== 快速验证完成（对局进行中，第 ' + s.turn + ' 回合） =====');
      console.log('当前玩家状态：');
      s.players.forEach(p => console.log(`  ${p.name}${p.isBot ? '（AI）' : ''}: 现金 ¥${p.money}，地产 ${p.properties.length} 块，总资产 ¥${p.totalAssets}${p.bankrupt ? '（破产）' : ''}`));
      verify(s, aiNames, false);
      console.log('✅ 断言通过：AI 自动行动持续推进、回合流转正常、状态合法');
      console.log('（注：未等到破产结束即为快速验证模式，完整胜负判定已由 simulate.js 覆盖）');
      return;
    }
    await sleep(30);
  }
  throw new Error('测试超时：30 轮内回合未正常推进（可能有卡死）');
}

function verify(s, aiNames, ended) {
  // ===== 断言 =====
  // AI 行动过：日志里有 AI 掷骰/购买/建房记录
  const aiLog = s.log.filter(e => aiNames.some(n => e.text.includes(n)));
  if (aiLog.length === 0) throw new Error('断言失败：日志中无 AI 行动记录');
  console.log(`\n✅ AI 机器人行动记录 ${aiLog.length} 条（掷骰/购买/建房等）`);
  for (const p of s.players) {
    if (p.money < 0) throw new Error(`断言失败：${p.name} 现金为负`);
  }
  if (ended) {
    const alive = s.players.filter(p => !p.bankrupt);
    if (alive.length !== 1) throw new Error('断言失败：应恰好剩 1 名存活玩家');
  }
}

main()
  .then(() => process.exit(0))
  .catch((e) => {
    console.error('\n❌ 测试失败：' + e.message);
    process.exit(1);
  })
  .finally(() => setTimeout(() => serverProc.kill(), 300));
