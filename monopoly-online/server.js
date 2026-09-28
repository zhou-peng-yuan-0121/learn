/**
 * 大富翁联机服务端
 * - 静态托管 public/ 前端
 * - WebSocket 实时同步（房间制，服务端权威）
 * 启动：npm start  默认端口 3000
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const { WebSocketServer } = require('ws');
const { Game, BOARD, PLAYER_COLORS } = require('./game');

const PORT = process.env.PORT || 3000;
const PUBLIC_DIR = path.join(__dirname, 'public');

// ==================== HTTP 静态服务 ====================
const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon'
};

const server = http.createServer((req, res) => {
  let urlPath = decodeURIComponent(req.url.split('?')[0]);
  if (urlPath === '/') urlPath = '/index.html';
  const filePath = path.join(PUBLIC_DIR, urlPath);
  // 防目录穿越
  if (!filePath.startsWith(PUBLIC_DIR)) {
    res.writeHead(403); res.end('Forbidden'); return;
  }
  fs.readFile(filePath, (err, data) => {
    if (err) {
      res.writeHead(404); res.end('Not Found'); return;
    }
    const ext = path.extname(filePath).toLowerCase();
    res.writeHead(200, { 'Content-Type': MIME[ext] || 'application/octet-stream' });
    res.end(data);
  });
});

// ==================== WebSocket 服务 ====================
const wss = new WebSocketServer({ server });

// 房间表：roomId -> { game, clients: Map<playerId, ws>, sessionToPlayer: Map<sessionId, playerId> }
const rooms = new Map();

function genRoomId() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  let id;
  do {
    id = Array.from({ length: 4 }, () => chars[Math.floor(Math.random() * chars.length)]).join('');
  } while (rooms.has(id));
  return id;
}

function send(ws, obj) {
  if (ws.readyState === ws.OPEN) {
    ws.send(JSON.stringify(obj));
  }
}

function broadcastRoom(room) {
  const state = room.game.toPublicState();
  for (const ws of room.clients.values()) {
    send(ws, { type: 'state', state });
  }
}

function getPlayerBySession(room, sessionId) {
  const playerId = room.sessionToPlayer.get(sessionId);
  if (!playerId) return null;
  return room.game.players.find(p => p.id === playerId) || null;
}

// 分配一个未使用的玩家颜色
function nextColor(game) {
  const used = new Set(game.players.map(p => p.color));
  return PLAYER_COLORS.find(c => !used.has(c)) || '#7f8c8d';
}

// ==================== AI 机器人行动（服务端自动代打） ====================
// 让机器人像真人一样分步行动：每步间隔由调度器控制，玩家可以看到完整过程
function actBot(room) {
  const g = room.game;
  const cp = g.currentPlayer;
  if (!cp || !cp.isBot || cp.bankrupt) return;
  g.lastActiveAt = Date.now();
  try {
    // 1) 有购买待决 → 买得起就买
    if (g.pendingBuy && g.pendingBuy.playerId === cp.id) {
      const cell = BOARD[g.pendingBuy.propertyId];
      if (cp.money >= cell.price) g.buy(cp.id);
      else g.skipBuy(cp.id);
      return;
    }
    // 2) 未掷骰 → 掷骰子
    if (!g.movedThisTurn && !g.canRollAgain) {
      g.roll(cp.id);
      return;
    }
    // 3) 双数 → 再掷一次
    if (g.canRollAgain) {
      g.roll(cp.id);
      return;
    }
    // 4) 已掷完 → 建房（优先补低等级地皮，建满为止）或结束回合
    if (g.movedThisTurn) {
      let built = false;
      for (const id of cp.properties) {
        const check = g.canBuild(cp, id);
        const level = cp.houses[id] || 0;
        if (check.ok && level < 5 && cp.money >= BOARD[id].houseCost) {
          g.buildHouse(cp.id, id);
          built = true;
          break;
        }
      }
      if (!built) g.endTurn(cp.id);
      return;
    }
    // 5) 保险
    g.endTurn(cp.id);
  } catch (e) {
    // 机器人操作出错时兜底：强制结束当前回合防止卡死
    try {
      if (g.phase === 'playing' && g.currentPlayer && g.currentPlayer.id === cp.id) g._endTurnInner();
    } catch (e2) { /* 忽略 */ }
  }
}

// 每 500ms 检查超时、驱动机器人并广播
setInterval(() => {
  for (const room of rooms.values()) {
    if (room.game.phase === 'playing') {
      room.game.checkTimeouts();
      const cp = room.game.currentPlayer;
      if (cp && cp.isBot && !cp.bankrupt) {
        const now = Date.now();
        if (!room.lastBotAct || now - room.lastBotAct > 400) {
          room.lastBotAct = now;
          actBot(room);
        }
      }
      broadcastRoom(room);
    }
  }
}, 500);

wss.on('connection', (ws, req) => {
  const ip = req.socket.remoteAddress;
  let currentRoom = null;
  let currentPlayerId = null;

  ws.on('message', (raw) => {
    let msg;
    try {
      msg = JSON.parse(raw.toString());
    } catch {
      send(ws, { type: 'error', message: '消息格式错误' });
      return;
    }

    try {
      handleMessage(msg);
    } catch (e) {
      send(ws, { type: 'error', message: e.message || '操作失败' });
    }
  });

  ws.on('close', () => {
    // 标记离线（保留玩家身份，可重连）
    if (currentRoom && currentPlayerId) {
      const room = rooms.get(currentRoom);
      if (!room) return;
      const p = room.game.players.find(x => x.id === currentPlayerId);
      if (p) {
        p.connected = false;
        room.log(`【系统】${p.name} 断线了，将自动代打`);
        broadcastRoom(room);
      }
    }
  });

  function handleMessage(msg) {
    switch (msg.type) {
      // ---------- 房间 ----------
      case 'createRoom': {
        if (currentRoom) throw new Error('你已在房间中');
        const name = String(msg.name || '').trim().slice(0, 12) || '玩家';
        const roomId = genRoomId();
        const game = new Game(roomId);
        const player = game.addPlayer(name, PLAYER_COLORS[0]);
        const room = {
          game,
          clients: new Map([[player.id, ws]]),
          sessionToPlayer: new Map(),
          log: (m) => game.log(m)
        };
        rooms.set(roomId, room);
        currentRoom = roomId;
        currentPlayerId = player.id;
        if (msg.sessionId) room.sessionToPlayer.set(String(msg.sessionId), player.id);
        send(ws, { type: 'joined', roomId, playerId: player.id, sessionId: msg.sessionId || player.id, isHost: true, game: game.toPublicState() });
        break;
      }

      case 'joinRoom': {
        if (currentRoom) throw new Error('你已在房间中');
        const roomId = String(msg.roomId || '').trim().toUpperCase();
        const room = rooms.get(roomId);
        if (!room) throw new Error('房间不存在');
        if (room.game.phase !== 'waiting') throw new Error('游戏已开始，无法加入');
        if (room.clients.size >= 6) throw new Error('房间已满');
        const name = String(msg.name || '').trim().slice(0, 12) || '玩家';
        // 重连：同一 sessionId 恢复身份
        if (msg.sessionId) {
          const existing = getPlayerBySession(room, msg.sessionId);
          if (existing && existing.connected === false) {
            existing.connected = true;
            existing.name = name;
            room.clients.set(existing.id, ws);
            currentRoom = roomId;
            currentPlayerId = existing.id;
            send(ws, { type: 'joined', roomId, playerId: existing.id, sessionId: msg.sessionId, isHost: existing.isHost, game: room.game.toPublicState() });
            room.log(`【系统】${name} 重新连接`);
            broadcastRoom(room);
            return;
          }
        }
        const player = room.game.addPlayer(name, nextColor(room.game));
        room.clients.set(player.id, ws);
        room.sessionToPlayer.set(msg.sessionId || player.id, player.id);
        currentRoom = roomId;
        currentPlayerId = player.id;
        send(ws, { type: 'joined', roomId, playerId: player.id, sessionId: msg.sessionId || player.id, isHost: player.isHost, game: room.game.toPublicState() });
        broadcastRoom(room);
        break;
      }

      case 'rejoin': {
        const roomId = String(msg.roomId || '').trim().toUpperCase();
        const room = rooms.get(roomId);
        if (!room) throw new Error('房间不存在');
        const existing = getPlayerBySession(room, msg.sessionId);
        if (!existing) throw new Error('会话不存在或已失效');
        existing.connected = true;
        room.clients.set(existing.id, ws);
        currentRoom = roomId;
        currentPlayerId = existing.id;
        send(ws, { type: 'joined', roomId, playerId: existing.id, sessionId: msg.sessionId, isHost: existing.isHost, game: room.game.toPublicState() });
        room.log(`【系统】${existing.name} 重新连接`);
        broadcastRoom(room);
        break;
      }

      // ---------- AI 机器人 ----------
      case 'addBot': {
        const room = requireRoom();
        if (room.game.phase !== 'waiting') throw new Error('游戏已开始，无法添加机器人');
        if (room.clients.get(room.game.players.find(p => p.isHost)?.id) !== ws) throw new Error('只有房主可以添加机器人');
        if (room.game.players.length >= 6) throw new Error('房间已满（最多 6 人）');
        const botCount = room.game.players.filter(p => p.isBot).length;
        if (botCount >= 4) throw new Error('最多添加 4 个 AI 玩家');
        const bot = room.game.addPlayer('AI-' + (botCount + 1), nextColor(room.game), true);
        room.log(`🤖 AI-${botCount + 1} 已加入（可随时移除）`);
        broadcastRoom(room);
        break;
      }

      case 'removeBot': {
        const room = requireRoom();
        if (room.game.phase !== 'waiting') throw new Error('游戏已开始，无法移除机器人');
        if (room.clients.get(room.game.players.find(p => p.isHost)?.id) !== ws) throw new Error('只有房主可以移除机器人');
        const bot = room.game.players.find(p => p.isBot && p.id === msg.playerId);
        if (!bot) throw new Error('机器人不存在');
        room.game.removePlayer(bot.id);
        broadcastRoom(room);
        break;
      }

      // ---------- 游戏操作 ----------
      case 'start': {
        const room = requireRoom();
        if (room.clients.get(room.game.players[0].id) !== ws) throw new Error('只有房主可以开始游戏');
        room.game.start();
        broadcastRoom(room);
        break;
      }

      case 'roll': {
        const room = requireRoom();
        room.game.roll(currentPlayerId);
        broadcastRoom(room);
        break;
      }

      case 'buy': {
        const room = requireRoom();
        room.game.buy(currentPlayerId);
        broadcastRoom(room);
        break;
      }

      case 'skipBuy': {
        const room = requireRoom();
        room.game.skipBuy(currentPlayerId);
        broadcastRoom(room);
        break;
      }

      case 'build': {
        const room = requireRoom();
        room.game.buildHouse(currentPlayerId, msg.propertyId);
        broadcastRoom(room);
        break;
      }

      case 'sell': {
        const room = requireRoom();
        room.game.sellHouse(currentPlayerId, msg.propertyId);
        broadcastRoom(room);
        break;
      }

      case 'endTurn': {
        const room = requireRoom();
        room.game.endTurn(currentPlayerId);
        broadcastRoom(room);
        break;
      }

      case 'chat': {
        const room = requireRoom();
        const p = room.game.players.find(x => x.id === currentPlayerId);
        const text = String(msg.text || '').slice(0, 200);
        if (!text) return;
        const chatMsg = { type: 'chat', playerId: currentPlayerId, name: p ? p.name : '??', color: p ? p.color : '#888', text };
        for (const ws2 of room.clients.values()) send(ws2, chatMsg);
        break;
      }

      case 'leaveRoom': {
        // 离开房间（非断线，直接移除）
        const room = rooms.get(currentRoom);
        if (room) {
          room.game.removePlayer(currentPlayerId);
          room.clients.delete(currentPlayerId);
          // 清理 session 映射
          for (const [sid, pid] of room.sessionToPlayer.entries()) {
            if (pid === currentPlayerId) room.sessionToPlayer.delete(sid);
          }
          broadcastRoom(room);
          if (room.clients.size === 0) rooms.delete(currentRoom);
        }
        currentRoom = null;
        currentPlayerId = null;
        send(ws, { type: 'left' });
        break;
      }

      default:
        throw new Error('未知指令');
    }
  }

  function requireRoom() {
    const room = rooms.get(currentRoom);
    if (!room) throw new Error('你不在房间中');
    return room;
  }
});

// ==================== 启动 ====================
server.listen(PORT, () => {
  console.log('==========================================');
  console.log('  🎲 大富翁联机服务已启动');
  console.log(`  本机访问：  http://localhost:${PORT}`);
  console.log('  局域网访问：http://<本机IP>:' + PORT);
  console.log('  （同一 Wi-Fi 下的朋友可用本机 IP 加入）');
  console.log('==========================================');
});
