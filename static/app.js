let ws = null;
let myClientId = "";
let players = [];
let grid = [];
let rows = 10;
let cols = 4;

const PASSWORD = "123456"; // 要跟 server.py 的 SITE_PASSWORD 一樣
let heartbeatTimer = null;
let reconnectTimer = null;
let pongTimer = null;
let connectTimer = null;
let reconnectAttempts = 0;
let reconnectPaused = false;
let hasJoined = false;
let pendingRoomAction = null;
const HEARTBEAT_INTERVAL_MS = 25000;
const PONG_TIMEOUT_MS = 15000;

const STORAGE_KEY = "artale_room_state_v1";
const CLIENT_ID_KEY = "artale_client_id_v1";

function getOrCreateClientId() {
    let cid = localStorage.getItem(CLIENT_ID_KEY);
    if (!cid) {
        cid = Math.random().toString(36).slice(2, 10);
        localStorage.setItem(CLIENT_ID_KEY, cid);
    }
    return cid;
}

function saveRoomState(data) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(data));
}

function loadRoomState() {
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        return raw ? JSON.parse(raw) : null;
    } catch (e) {
        return null;
    }
}

function clearRoomState() {
    localStorage.removeItem(STORAGE_KEY);
}

function clearConnectionTimers() {
    stopHeartbeat();
    clearTimeout(connectTimer);
    clearTimeout(reconnectTimer);
    connectTimer = reconnectTimer = null;
}

function scheduleReconnect() {
    if (reconnectPaused || reconnectTimer) return;
    const delay = Math.min(1000 * 2 ** Math.min(reconnectAttempts++, 4), 15000);
    setText("connText", `連線中斷，${delay / 1000} 秒後重連（進度保留 5 分鐘）`);
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connectWs();
    }, delay);
}

function restartConnection(reason) {
    console.info("[Artale] reconnect", { reason, at: new Date().toISOString() });
    const previous = ws;
    ws = null; // Ignore late events from the connection being replaced.
    hasJoined = false;
    clearConnectionTimers();
    if (previous) previous.close();
    scheduleReconnect();
}

function connectWs() {
    if (reconnectPaused || (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING))) return;
    clearConnectionTimers();
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const clientId = getOrCreateClientId();
    const socket = new WebSocket(`${protocol}://${location.host}/ws?p=${encodeURIComponent(PASSWORD)}&cid=${encodeURIComponent(clientId)}`);
    ws = socket;
    setText("connText", "連線中...");
    connectTimer = setTimeout(() => {
        if (ws === socket) restartConnection("connect_timeout");
    }, 20000);

    socket.onopen = function () {
        if (ws !== socket) return;
        clearTimeout(connectTimer);
        setText("connText", "已連線，正在確認房間...");
        startHeartbeat();
        if (pendingRoomAction) {
            const action = pendingRoomAction;
            pendingRoomAction = null;
            send(action);
        } else if (!tryAutoRejoin()) {
            setText("connText", "已連線，尚未加入房間");
        }
    };

    socket.onclose = function (event) {
        if (ws !== socket) return;
        console.info("[Artale] disconnected", {
            code: event.code, reason: event.reason, clean: event.wasClean,
            visibility: document.visibilityState, at: new Date().toISOString()
        });
        ws = null;
        hasJoined = false;
        clearConnectionTimers();
        if (event.code === 4001 || event.code === 4003) {
            reconnectPaused = true;
            if (event.code === 4003) leaveRoomMemory();
            setText("connText", event.code === 4001
                ? "已在其他分頁連線；可在此重新加入"
                : "已被管理員移出房間");
            return;
        }
        scheduleReconnect();
    };

    socket.onmessage = function (event) {
        if (ws !== socket) return;
        const data = JSON.parse(event.data);

        if (data.type === "pong") {
            clearTimeout(pongTimer);
            pongTimer = null;
            reconnectAttempts = 0;
            return;
        }
        if (data.type === "joined") {
            hasJoined = true;
            reconnectAttempts = 0;
            myClientId = data.client_id;
            setText("connText", "已連線");
            setText("currentRoom", data.room_id);
            document.getElementById("roomId").value = data.room_id;
            saveRoomState({ room_id: data.room_id, name: getName(), color: getColor() });
        }
        if (data.type === "room_update") {
            players = data.players || [];
            grid = data.grid || [];
            rows = data.rows || 10;
            cols = data.cols || 4;
            setText("currentRoom", data.room_id || "-");
            renderPlayers();
            renderGrid();
        }
        if (data.type === "session_expired") {
            leaveRoomMemory();
            setText("connText", data.message);
        }
        if (data.type === "kicked") {
            reconnectPaused = true;
            pendingRoomAction = null;
            leaveRoomMemory();
            setText("connText", data.message);
        }
        if (data.type === "error") {
            setText("connText", hasJoined ? "已連線" : "已連線，尚未加入房間");
            alert(data.message || "發生錯誤");
        }
    };
    socket.onerror = function () {
        if (ws !== socket) return;
        console.info("[Artale] socket error", { at: new Date().toISOString() });
        // The following close event provides the diagnostic code and schedules retry.
        setText("connText", "連線異常，正在重新連線...");
    };
}

function pingServer() {
    if (!ws || ws.readyState !== WebSocket.OPEN || pongTimer) return;
    const socket = ws;
    pongTimer = setTimeout(() => {
        if (ws === socket) restartConnection("heartbeat_timeout");
    }, PONG_TIMEOUT_MS);
    send({ action: "ping" });
}

function startHeartbeat() {
    stopHeartbeat();
    pingServer();
    heartbeatTimer = setInterval(pingServer, HEARTBEAT_INTERVAL_MS);
}

function stopHeartbeat() {
    clearInterval(heartbeatTimer);
    clearTimeout(pongTimer);
    heartbeatTimer = pongTimer = null;
}

function resumeConnection() {
    if (reconnectPaused) return;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        if (ws && ws.readyState === WebSocket.CONNECTING) return;
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
        connectWs();
        return;
    }
    // Timers may have been suspended while the page was in the background.
    clearTimeout(pongTimer);
    pongTimer = null;
    pingServer();
}

function tryAutoRejoin() {
    const saved = loadRoomState();
    if (!saved || !saved.room_id || !saved.name || !saved.color) return false;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;

    const nameInput = document.getElementById("name");
    const roomInput = document.getElementById("roomId");
    const colorInput = document.getElementById("color");

    if (nameInput) nameInput.value = saved.name;
    if (roomInput) roomInput.value = saved.room_id;
    if (colorInput) colorInput.value = saved.color;

    updateMyColorPreview();

    ws.send(JSON.stringify({
        action: "join_room",
        name: saved.name,
        room_id: saved.room_id,
        color: saved.color
    }));
    return true;
}

function setText(id, text) {
    const el = document.getElementById(id);
    if (el) {
        el.textContent = text;
    }
}

function getName() {
    return document.getElementById("name").value.trim();
}

function getRoomId() {
    return document.getElementById("roomId").value.trim();
}

function getColor() {
    return document.getElementById("color").value;
}

function updateMyColorPreview() {
    const color = getColor();
    const preview = document.getElementById("myColorPreview");
    if (preview) {
        preview.style.background = color;
    }
    setText("myColorText", color);
}

function send(obj) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
        setText("connText", "正在重新連線，連線成功後會自動返回房間");
        resumeConnection();
        return false;
    }
    try {
        ws.send(JSON.stringify(obj));
        return true;
    } catch (error) {
        restartConnection("send_failed");
        return false;
    }
}

function requestRoom(action) {
    reconnectPaused = false;
    if (ws && ws.readyState === WebSocket.OPEN) {
        send(action);
    } else {
        pendingRoomAction = action;
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
        connectWs();
    }
}

function createRoom() {
    const name = getName();
    if (!name) {
        alert("請輸入名稱");
        return;
    }
    updateMyColorPreview();
    requestRoom({ action: "create_room", name, color: getColor() });
}

function joinRoom() {
    const name = getName();
    const roomId = getRoomId();
    if (!name || !roomId) {
        alert("請輸入名稱與房號");
        return;
    }
    updateMyColorPreview();
    requestRoom({ action: "join_room", name, room_id: roomId, color: getColor() });
}

function changeColor() {
    const color = getColor();
    updateMyColorPreview();

    const saved = loadRoomState();
    if (saved) {
        saved.color = color;
        saveRoomState(saved);
    }

    if (!hasJoined) return;

    send({
        action: "change_color",
        color: color
    });
}

function resetMe() {
    if (!hasJoined) { resumeConnection(); return; }
    send({ action: "reset_me" });
}

function resetAll() {
    if (!hasJoined) { resumeConnection(); return; }
    send({ action: "reset_all" });
}

function clickCell(row, col) {
    if (!hasJoined) { resumeConnection(); return; }
    send({
        action: "click_cell",
        row: row,
        col: col
    });
}

function leaveRoomMemory() {
    clearRoomState();
    hasJoined = false;
    myClientId = "";
    players = [];
    grid = [];
    renderPlayers();
    renderGrid();
    setText("currentRoom", "-");
    const roomInput = document.getElementById("roomId");
    if (roomInput) {
        roomInput.value = "";
    }
}

function renderPlayers() {
    const wrap = document.getElementById("players");
    if (!wrap) return;

    wrap.innerHTML = "";

    players.forEach(p => {
        const div = document.createElement("div");
        div.className = "player-tag";
        div.style.background = p.color;
        div.textContent =
            p.name +
            (p.client_id === myClientId ? "（你）" : "") +
            (p.is_host ? "［房主］" : "") +
            (p.connected === false ? "（斷線保留中）" : "");
        wrap.appendChild(div);
    });
}

function copyRoomCode() {
    const roomCode = document.getElementById("currentRoom").textContent.trim();

    if (!roomCode || roomCode === "-") {
        alert("目前沒有房號");
        return;
    }

    navigator.clipboard.writeText(roomCode).then(() => {
        const msg = document.getElementById("copyMsg");
        if (msg) {
            msg.textContent = "已複製";
            setTimeout(() => {
                msg.textContent = "";
            }, 1500);
        }
    }).catch(() => {
        alert("複製失敗");
    });
}

function renderGrid() {
    const table = document.getElementById("gridTable");
    if (!table) return;

    table.innerHTML = "";

    for (let r = rows - 1; r >= 0; r--) {
        const tr = document.createElement("tr");

        const label = document.createElement("td");
        label.className = "floor-label";
        label.textContent = `${r + 1}F`;
        tr.appendChild(label);

        for (let c = 0; c < cols; c++) {
            const td = document.createElement("td");
            const btn = document.createElement("button");
            btn.className = "cell-btn";

            const cell = grid[r] ? grid[r][c] : null;

            if (cell) {
                btn.style.background = cell.color;
                btn.style.borderColor = cell.color;
                btn.style.color = "#fff";
                btn.textContent = cell.name;
            } else {
                btn.style.background = "#fff";
                btn.style.borderColor = "#d1d5db";
                btn.style.color = "#374151";
                btn.textContent = "";
            }

            btn.onclick = function () {
                clickCell(r, c);
            };

            td.appendChild(btn);
            tr.appendChild(td);
        }

        table.appendChild(tr);
    }
}

window.addEventListener("online", resumeConnection);
window.addEventListener("pageshow", resumeConnection);
document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") resumeConnection();
});

document.getElementById("color").addEventListener("input", changeColor);
updateMyColorPreview();
connectWs();
