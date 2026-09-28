/* 大富翁前端：渲染 + 交互 */
(function () {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const DICE_GLYPHS = ['', '⚀', '⚁', '⚂', '⚃', '⚄', '⚅'];

  let ws = null;
  let sessionId = localStorage.getItem('mono_session') || '';
  let myRoomId = localStorage.getItem('mono_room') || '';
  let myPlayerId = null;
  let state = null;

  // ==================== 初始化 ====================
  function connect() {
    const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    ws = new WebSocket(proto + location.host);

    ws.onopen = () => {
      hideError();
      if (myRoomId && sessionId) {
        // 尝试重连之前所在房间
        ws.send(JSON.stringify({ type: 'rejoin', roomId: myRoomId, sessionId }));
      }
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        handleMessage(msg);
      } catch (e) { console.error('消息解析失败', e); }
    };
    ws.onclose = () => {
      showError('连接已断开，正在尝试重连…');
      setTimeout(connect, 2000);
    };
    ws.onerror = () => { ws.close(); };
  }

  function send(obj) {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj));
  }

  // ==================== 消息处理 ====================
  function handleMessage(msg) {
    switch (msg.type) {
      case 'joined':
        sessionId = msg.sessionId;
        myPlayerId = msg.playerId;
        myRoomId = msg.roomId;
        localStorage.setItem('mono_session', sessionId);
        localStorage.setItem('mono_room', myRoomId);
        showGame();
        state = msg.game;
        renderAll();
        break;
      case 'state':
        state = msg.state;
        renderAll();
        break;
      case 'chat':
        appendChat(msg);
        break;
      case 'error':
        showError(msg.message);
        break;
      case 'left':
        myRoomId = '';
        localStorage.removeItem('mono_room');
        location.reload();
        break;
    }
  }

  // ==================== 视图切换 ====================
  function showGame() {
    $('#lobby').classList.add('hidden');
    $('#game').classList.remove('hidden');
  }

  function showError(text) {
    $('#lobbyError').textContent = text;
    $('#lobbyError').classList.remove('hidden');
    setTimeout(() => { try { $('#lobbyError').classList.add('hidden'); } catch (e) {} }, 4000);
  }

  function hideError() {
    $('#lobbyError').classList.add('hidden');
  }

  // ==================== 房间逻辑 ====================
  function currentName() {
    return $('#nickname').value.trim() || '玩家';
  }

  function bindLobby() {
    $('#btnCreate').addEventListener('click', () => {
      if (!sessionId) sessionId = 's' + Math.random().toString(36).slice(2, 10);
      send({ type: 'createRoom', name: currentName(), sessionId });
    });
    $('#btnJoin').addEventListener('click', () => {
      const code = $('#roomCode').value.trim().toUpperCase();
      if (!code) { showError('请输入房间码'); return; }
      if (!sessionId) sessionId = 's' + Math.random().toString(36).slice(2, 10);
      send({ type: 'joinRoom', roomId: code, name: currentName(), sessionId });
    });
    $('#roomCode').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') $('#btnJoin').click();
    });
    $('#nickname').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') $('#btnCreate').click();
    });
    // 记住昵称
    const savedName = localStorage.getItem('mono_name');
    if (savedName) $('#nickname').value = savedName;
    $('#nickname').addEventListener('change', () => localStorage.setItem('mono_name', $('#nickname').value));
  }

  // ==================== 棋盘渲染 ====================
  // 格坐标：11x11 grid（行、列从 1 开始）
  function cellPos(id) {
    if (id <= 10) {
      // 底行：格 0..9 (11,11)..(11,2)，格 10 角落 (11,1)
      if (id === 10) return { r: 11, c: 1 };
      return { r: 11, c: 11 - id };
    }
    if (id <= 20) {
      // 左列：格 11..19 (10,1)..(2,1)，格 20 角落 (1,1)
      if (id === 20) return { r: 1, c: 1 };
      return { r: 21 - id, c: 1 };
    }
    if (id <= 30) {
      // 顶行：格 21..29 (1,2)..(1,10)，格 30 角落 (1,11)
      if (id === 30) return { r: 1, c: 11 };
      return { r: 1, c: id - 19 };
    }
    // 右列：格 31..39 (2,11)..(10,11)
    return { r: id - 29, c: 11 };
  }

  function cellClass(id) {
    if (id === 0 || id === 10 || id === 20 || id === 30) return 'corner';
    if (id < 10) return 'dir-b';
    if (id < 20) return 'dir-l';
    if (id < 30) return 'dir-t';
    return 'dir-r';
  }

  const GROUP_COLORS = [
    '#955436', '#7ec8e3', '#e0669c', '#f08c3c', '#e33b3b', '#f5d53b', '#2fa94f', '#3b62d8'
  ];

  const CELL_ICONS = {
    go: 'GO',
    jail: '🔒',
    parking: '🅿️',
    gojail: '🚔',
    station: '🚂',
    utility: '⚡',
    chance: '❓',
    tax: '💸'
  };

  function buildBoardOnce(board) {
    const container = $('#board');
    container.innerHTML = '';
    for (const cell of board) {
      const el = document.createElement('div');
      el.className = 'cell type-' + cell.type + ' ' + cellClass(cell.id);
      el.dataset.id = cell.id;
      const pos = cellPos(cell.id);
      el.style.gridRow = pos.r;
      el.style.gridColumn = pos.c;

      let inner = '';
      if (cell.type === 'property') {
        el.style.setProperty('--gcolor', GROUP_COLORS[cell.group]);
        inner = `<div class="stripe"></div><div class="cell-name">${cell.name}</div><div class="cell-price">¥${cell.price}</div>`;
      } else if (cell.type === 'station') {
        inner = `<div class="corner-icon">🚂</div><div class="cell-name">${cell.name}</div><div class="cell-price">¥${cell.price}</div>`;
      } else if (cell.type === 'utility') {
        inner = `<div class="corner-icon">⚡</div><div class="cell-name">${cell.name}</div><div class="cell-price">¥${cell.price}</div>`;
      } else if (cell.type === 'tax') {
        inner = `<div class="corner-icon">💸</div><div class="cell-name">${cell.name}</div><div class="cell-price">¥${cell.amount}</div>`;
      } else if (cell.type === 'chance') {
        inner = `<div class="corner-icon">❓</div><div class="cell-name">机会卡</div>`;
      } else if (cell.type === 'visit') {
        inner = `<div class="corner-icon">🔒</div><div class="cell-name">监狱</div>`;
      } else {
        inner = `<div class="corner-icon">${CELL_ICONS[cell.type] || ''}</div><div class="cell-name">${cell.name}</div>`;
      }
      el.innerHTML = inner;
      container.appendChild(el);
    }
  }

  // ==================== 状态渲染 ====================
  function renderAll() {
    if (!state) return;
    renderTokens();
    renderOwners();
    renderRoomInfo();
    renderPlayers();
    renderActions();
    renderProperties();
    renderLog();
    renderDice();
  }

  function renderDice() {
    const [a, b] = state.dice;
    $('#die1').textContent = DICE_GLYPHS[a] || '·';
    $('#die2').textContent = DICE_GLYPHS[b] || '·';
  }

  function renderRoomInfo() {
    $('#roomInfo').textContent = '房间：' + state.roomId;
  }

  function renderTokens() {
    const cells = document.querySelectorAll('.cell');
    const byCell = {};
    for (const c of cells) byCell[c.dataset.id] = c;
    // 清理旧棋子
    document.querySelectorAll('.token').forEach(t => t.remove());
    state.players.forEach((p, i) => {
      if (!byCell[p.position]) return;
      const cell = byCell[p.position];
      const t = document.createElement('div');
      t.className = 'token' + (p.bankrupt ? ' bankrupt' : '');
      t.style.background = p.color;
      t.style.left = (16 + (i % 3) * 24) + '%';
      t.style.top = (16 + Math.floor(i / 3) * 24) + '%';
      t.textContent = String(i + 1);
      t.title = p.name;
      cell.appendChild(t);
    });
    // 高亮当前玩家位置
    document.querySelectorAll('.cell.highlight').forEach(c => c.classList.remove('highlight'));
    const cur = state.players.find(p => p.id === state.currentPlayerId);
    if (cur && byCell[cur.position]) byCell[cur.position].classList.add('highlight');
  }

  function renderOwners() {
    const cells = document.querySelectorAll('.cell');
    const byCell = {};
    for (const c of cells) byCell[c.dataset.id] = c;
    // 清理旧标记
    document.querySelectorAll('.cell .owner-dot, .cell .houses, .cell .hotel').forEach(n => n.remove());

    for (const p of state.players) {
      for (const id of p.properties) {
        const cell = byCell[id];
        if (!cell) continue;
        const dot = document.createElement('div');
        dot.className = 'owner-dot';
        dot.style.background = p.color;
        cell.appendChild(dot);
        const houses = p.houses[id] || 0;
        if (houses > 0) {
          if (houses >= 5) {
            const hotel = document.createElement('div');
            hotel.className = 'hotel';
            hotel.textContent = '🏨';
            cell.appendChild(hotel);
          } else {
            const h = document.createElement('div');
            h.className = 'houses';
            h.textContent = '🏠'.repeat(houses);
            cell.appendChild(h);
          }
        }
      }
    }
  }

  function renderPlayers() {
    const panel = $('#playersPanel');
    panel.innerHTML = '<div class="panel-title">玩家（' + state.players.length + '/6）</div>';
    for (const p of state.players) {
      const row = document.createElement('div');
      row.className = 'player-row' +
        (state.currentPlayerId === p.id ? ' current' : '') +
        (p.bankrupt ? ' bankrupt-row' : '');
      const isMe = p.id === myPlayerId;
      const me = state.players.find(x => x.id === myPlayerId);
      let statusText = '';
      if (p.bankrupt) statusText = '💀 已破产';
      else if (p.inJail) statusText = '🔒 在监狱（第 ' + p.jailTurns + ' 次尝试）';
      else if (state.currentPlayerId === p.id) statusText = '🎯 行动中';
      else if (!p.connected && !p.isBot) statusText = '⚠️ 已断线（自动代打）';
      else if (p.isBot) statusText = '🤖 AI 自动行动';
      else statusText = '等待中';
      let tags = '';
      if (isMe) tags += '<span class="tag">我</span>';
      if (p.isHost) tags += '<span class="tag">房主</span>';
      if (p.isBot) tags += '<span class="tag ai-tag">AI</span>';
      let removeBtn = '';
      if (p.isBot && me && me.isHost && state.phase === 'waiting') {
        removeBtn = `<button class="btn-remove-bot" data-bot="${p.id}" title="移除该 AI">✕</button>`;
      }
      row.innerHTML = `
        <div class="player-avatar" style="background:${p.color}">${state.players.indexOf(p) + 1}</div>
        <div class="player-info">
          <div class="player-name">${esc(p.name)}${tags}</div>
          <div class="player-money">💰 ¥${p.money.toLocaleString()}</div>
          <div class="player-status">${statusText} · 总资产 ¥${p.totalAssets.toLocaleString()}</div>
        </div>${removeBtn}`;
      panel.appendChild(row);
    }
    // 绑定移除机器人
    panel.querySelectorAll('.btn-remove-bot').forEach(btn => {
      btn.onclick = () => send({ type: 'removeBot', playerId: btn.dataset.bot });
    });
  }

  function renderActions() {
    const area = $('#actionArea');
    const buyArea = $('#buyArea');
    area.innerHTML = '';
    buyArea.classList.add('hidden');
    buyArea.innerHTML = '';

    if (!state) return;

    // 游戏结束
    if (state.phase === 'ended') {
      const winner = state.players.find(p => p.id === state.winner);
      area.innerHTML = `<div class="buy-area" style="width:90%"><div style="font-size:16px;font-weight:800">🏆 游戏结束</div>
        <div style="margin-top:6px">胜利者：<span style="font-weight:800">${winner ? winner.name : '无'}</span></div></div>`;
      return;
    }

    if (state.phase === 'waiting') {
      const me = state.players.find(p => p.id === myPlayerId);
      const host = state.players.find(p => p.isHost);
      const botCount = state.players.filter(p => p.isBot).length;
      if (me && me.isHost) {
        // 房主：开始游戏 + 添加 AI 玩家
        const btnStart = document.createElement('button');
        btnStart.className = 'btn btn-roll';
        btnStart.textContent = state.players.length >= 2 ? '🚀 开始游戏' : '至少需要 2 人才能开始';
        btnStart.disabled = state.players.length < 2;
        btnStart.onclick = () => send({ type: 'start' });
        area.appendChild(btnStart);

        const btnAddBot = document.createElement('button');
        btnAddBot.className = 'btn btn-ai';
        btnAddBot.textContent = botCount >= 4 || state.players.length >= 6 ? `🤖 已添加 ${botCount}/4 个 AI` : `🤖 添加 AI 玩家（${botCount}/4）`;
        btnAddBot.disabled = botCount >= 4 || state.players.length >= 6;
        btnAddBot.onclick = () => send({ type: 'addBot' });
        area.appendChild(btnAddBot);

        const hint = document.createElement('div');
        hint.className = 'turn-info';
        hint.textContent = `${state.players.length} 人已就绪${botCount > 0 ? `（含 ${botCount} 个 AI）` : ''}，点击「开始游戏」开玩`;
        area.appendChild(hint);
      } else {
        const hint = document.createElement('div');
        hint.className = 'turn-info';
        hint.textContent = '等待房主 ' + (host ? host.name : '') + ' 开始游戏…';
        area.appendChild(hint);
        if (botCount > 0) {
          const sub = document.createElement('div');
          sub.className = 'turn-info';
          sub.textContent = `当前 ${state.players.length} 人（含 ${botCount} 个 AI）`;
          area.appendChild(sub);
        }
      }
      return;
    }

    const me = state.players.find(p => p.id === myPlayerId);
    const myTurn = state.currentPlayerId === myPlayerId && me && !me.bankrupt;

    if (!myTurn) {
      const cur = state.players.find(p => p.id === state.currentPlayerId);
      const wait = document.createElement('div');
      wait.className = 'turn-info';
      wait.textContent = cur ? `等待 ${cur.name} 行动…` : '等待中…';
      area.appendChild(wait);
      return;
    }

    // 我的回合
    // 1) 购买请求
    if (state.pendingBuy && state.pendingBuy.playerId === myPlayerId) {
      const cell = state.pendingBuy.cell;
      buyArea.classList.remove('hidden');
      const remain = Math.max(0, Math.ceil((state.pendingBuy.deadline - Date.now()) / 1000));
      buyArea.innerHTML = `
        <div>来到 <span class="buy-name">${esc(cell.name)}</span></div>
        <div class="buy-price">售价 ¥${cell.price}</div>
        <div class="countdown">${remain} 秒内未决定将自动放弃</div>
        <div class="buy-btns">
          <button id="btnBuy" class="btn btn-small btn-buy" ${me.money < cell.price ? 'disabled' : ''}>购买 ¥${cell.price}</button>
          <button id="btnSkip" class="btn btn-small btn-skip">放弃</button>
        </div>`;
      $('#btnBuy').onclick = () => send({ type: 'buy' });
      $('#btnSkip').onclick = () => send({ type: 'skipBuy' });
      if (me.money < cell.price) {
        buyArea.querySelector('.countdown').textContent = '现金不足（¥' + me.money + '），将自动放弃';
      }
      return;
    }

    // 2) 本回合还没掷骰 → 掷骰子
    if (!state.movedThisTurn) {
      const btn = document.createElement('button');
      btn.className = 'btn btn-roll';
      btn.textContent = '🎲 掷骰子';
      btn.onclick = () => send({ type: 'roll' });
      area.appendChild(btn);
      return;
    }

    // 3) 掷出双数 → 再掷一次
    if (state.canRollAgain) {
      const btn = document.createElement('button');
      btn.className = 'btn btn-roll';
      btn.textContent = '🎲 掷出双数，再掷一次！';
      btn.onclick = () => send({ type: 'roll' });
      area.appendChild(btn);
      return;
    }

    // 4) 本轮已掷完 → 结束回合
    const btn = document.createElement('button');
    btn.className = 'btn btn-roll';
    btn.textContent = '⏹ 结束回合';
    btn.onclick = () => send({ type: 'endTurn' });
    area.appendChild(btn);
  }

  function renderProperties() {
    const panel = $('#myProperties');
    const hint = $('#buildHint');
    const me = state ? state.players.find(p => p.id === myPlayerId) : null;
    if (!me) { panel.innerHTML = ''; hint.textContent = ''; return; }

    const myTurn = state.phase === 'playing' && state.currentPlayerId === myPlayerId && !me.bankrupt;
    hint.textContent = myTurn ? '（点击建房 · 右键出售）' : '';

    if (me.properties.length === 0) {
      panel.innerHTML = '<div class="prop-empty">还没有地产，先去买买买！</div>';
      return;
    }
    panel.innerHTML = '';
    for (const id of me.properties) {
      const cell = state.board[id];
      if (!cell) continue;
      const level = me.houses[id] || 0;
      const chip = document.createElement('div');
      chip.className = 'prop-chip';
      const canBuild = myTurn && !state.pendingBuy;
      if (canBuild && cell.type === 'property' && level < 5) {
        // 检查是否成套
        const groupIds = state.board.filter(c => c.type === 'property' && c.group === cell.group).map(c => c.id);
        const ownsAll = groupIds.every(gid => me.properties.includes(gid));
        if (ownsAll) chip.classList.add('can-build');
      }
      chip.innerHTML = `
        <span class="prop-color" style="background:${cell.type === 'property' ? GROUP_COLORS[cell.group] : (cell.type === 'station' ? '#8e6f4f' : '#f0a832')}"></span>
        <span class="prop-name">${esc(cell.name)}</span>
        <span class="prop-level">${level >= 5 ? '🏨旅馆' : '🏠'.repeat(level) || '空地'}</span>`;
      chip.title = cell.type === 'property'
        ? (level >= 5 ? '已建满旅馆' : `建下一栋房 ¥${cell.houseCost}（右键出售 ¥${Math.floor(cell.houseCost / 2)}）`)
        : '车站/公用事业不可建房';
      if (canBuild && cell.type === 'property' && level < 5) {
        chip.addEventListener('click', () => {
          if (chip.classList.contains('can-build')) send({ type: 'build', propertyId: id });
          else showError('需拥有整套颜色组才能建房');
        });
        chip.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          if (level > 0) send({ type: 'sell', propertyId: id });
        });
      }
      panel.appendChild(chip);
    }
  }

  function renderLog() {
    const list = $('#logList');
    list.innerHTML = '';
    if (!state) return;
    for (const entry of state.log) {
      const div = document.createElement('div');
      div.className = 'log-entry';
      const time = new Date(entry.t);
      const hh = String(time.getHours()).padStart(2, '0');
      const mm = String(time.getMinutes()).padStart(2, '0');
      div.innerHTML = `<span class="lt">${hh}:${mm}</span>${esc(entry.text)}`;
      list.appendChild(div);
    }
    list.scrollTop = list.scrollHeight;
  }

  // ==================== 聊天 ====================
  function appendChat(msg) {
    const list = $('#chatList');
    const div = document.createElement('div');
    div.className = 'chat-msg';
    div.innerHTML = `<span class="chat-name" style="color:${msg.color}">${esc(msg.name)}：</span>${esc(msg.text)}`;
    list.appendChild(div);
    list.scrollTop = list.scrollHeight;
  }

  function bindChat() {
    const input = $('#chatInput');
    const doSend = () => {
      const text = input.value.trim();
      if (!text) return;
      send({ type: 'chat', text });
      input.value = '';
    };
    $('#btnChat').addEventListener('click', doSend);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') doSend(); });
  }

  // ==================== 工具 ====================
  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  // ==================== 启动 ====================
  function init() {
    buildBoardOnce([]); // 占位
    bindLobby();
    bindChat();
    connect();
  }

  // 首次收到 board 时建棋盘
  const originalHandle = handleMessage;
  handleMessage = function (msg) {
    originalHandle(msg);
    const initial = msg.game || msg.state;
    if (initial && $('#board').children.length === 0) {
      buildBoardOnce(initial.board);
      renderTokens();
      renderOwners();
    }
  };

  init();
})();
