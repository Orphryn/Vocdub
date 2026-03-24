import { app, BrowserWindow, Tray, Menu, nativeImage, Notification, ipcMain } from "electron";
import path from "path";
import {
  AppState, getState, setState, getStateLabel, getStateColor, canTransitionTo, forceState
} from "./state-machine";
import { transcriptLines, saveTranscript } from "./transcript-store";
import {
  startPythonWorker, stopPythonWorker, sendCommand, resetRestartCounter, WorkerEvent
} from "./python-worker";

// ━━ App State ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

let tray: Tray | null = null;
let mainWindow: BrowserWindow | null = null;
let overlayWindow: BrowserWindow | null = null;
let isQuitting = false;
let workerReady = false;
let pendingCommands: object[] = [];
let autoResume = true;

// Session data
let liveSessionLines: string[] = [];
let lastTranscription = "";
let lastTranslation = "";
let lastLanguage = "";
let sessionCount = 0;

// ━━ Utilities ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function htmlToDataUrl(html: string): string {
  return `data:text/html;charset=UTF-8,${encodeURIComponent(html)}`;
}

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function addLine(line: string): void {
  const ts = new Date().toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  liveSessionLines.push(`[${ts}] ${line}`);
  if (liveSessionLines.length > 100) {
    liveSessionLines = liveSessionLines.slice(-100);
  }
}

// ━━ Command Queue ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function safeSend(command: object): void {
  if (workerReady) {
    sendCommand(command);
  } else {
    pendingCommands.push(command);
  }
}

function flushPending(): void {
  for (const cmd of pendingCommands) sendCommand(cmd);
  pendingCommands = [];
}

// ━━ UI Rendering ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function getMainHtml(state: AppState): string {
  const label = getStateLabel(state);
  const color = getStateColor(state);
  const logHtml = liveSessionLines
    .slice(-40)
    .map(l => `<div class="log-line">${escapeHtml(l)}</div>`)
    .join("");

  const resultHtml = lastTranscription
    ? `<div class="result-box">
         <div class="result-label">Transcription <span class="lang-badge">${escapeHtml(lastLanguage.toUpperCase())}</span></div>
         <div class="result-text">${escapeHtml(lastTranscription)}</div>
         ${lastTranslation && lastTranslation !== lastTranscription
           ? `<div class="result-label" style="margin-top:12px;">Translation</div>
              <div class="result-text translation">${escapeHtml(lastTranslation)}</div>`
           : ""}
       </div>`
    : "";

  return `<!DOCTYPE html>
<html><head><meta charset="UTF-8"/><title>VoxDub</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600&family=DM+Sans:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    font-family: 'DM Sans', sans-serif;
    background: #0a0a0f;
    color: #e4e4e7;
    height: 100vh;
    overflow: hidden;
  }
  .container { display:flex; height:100vh; }
  .sidebar {
    width: 260px;
    background: #111118;
    border-right: 1px solid #1e1e2a;
    padding: 24px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .logo {
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.5px;
    margin-bottom: 4px;
    background: linear-gradient(135deg, #3b82f6, #8b5cf6);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  .version { font-size: 11px; color: #52525b; margin-bottom: 20px; }
  .status-pill {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 8px 14px;
    border-radius: 99px;
    font-size: 13px;
    font-weight: 500;
    margin-bottom: 16px;
    background: ${color}18;
    border: 1px solid ${color}40;
    color: ${color};
  }
  .status-dot {
    width: 8px; height: 8px;
    border-radius: 50%;
    background: ${color};
    ${state === "monitoring" ? "animation: pulse 1.5s ease-in-out infinite;" : ""}
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.4; transform: scale(0.8); }
  }
  .btn {
    padding: 10px 14px;
    font-size: 13px;
    font-family: 'DM Sans', sans-serif;
    font-weight: 500;
    border: 1px solid #27272a;
    border-radius: 8px;
    background: #18181b;
    color: #e4e4e7;
    cursor: pointer;
    transition: all 0.15s;
    text-align: left;
  }
  .btn:hover:not(:disabled) { background: #27272a; border-color: #3f3f46; }
  .btn:disabled { opacity: 0.3; cursor: not-allowed; }
  .btn.primary {
    background: #3b82f6;
    border-color: #3b82f6;
    color: white;
  }
  .btn.primary:hover:not(:disabled) { background: #2563eb; }
  .btn.danger {
    background: #18181b;
    border-color: #27272a;
    color: #ef4444;
  }
  .btn.danger:hover:not(:disabled) { background: #1c1012; border-color: #ef4444; }
  .spacer { flex:1; }
  .main-content {
    flex: 1;
    padding: 24px 32px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  .section-title {
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1.5px;
    color: #52525b;
    margin-bottom: 8px;
  }
  .result-box {
    background: #111118;
    border: 1px solid #1e1e2a;
    border-radius: 12px;
    padding: 20px;
  }
  .result-label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #71717a;
    margin-bottom: 6px;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .lang-badge {
    font-size: 10px;
    padding: 2px 8px;
    border-radius: 99px;
    background: #3b82f620;
    color: #60a5fa;
    border: 1px solid #3b82f640;
  }
  .result-text {
    font-size: 18px;
    font-weight: 500;
    line-height: 1.5;
    color: #fafafa;
  }
  .result-text.translation {
    color: #22c55e;
  }
  .log-container {
    flex: 1;
    background: #111118;
    border: 1px solid #1e1e2a;
    border-radius: 12px;
    padding: 16px;
    overflow-y: auto;
    min-height: 200px;
  }
  .log-line {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11.5px;
    line-height: 1.8;
    color: #71717a;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .log-line:last-child { color: #a1a1aa; }
  .stats {
    display: flex;
    gap: 16px;
  }
  .stat-card {
    flex: 1;
    background: #111118;
    border: 1px solid #1e1e2a;
    border-radius: 10px;
    padding: 16px;
  }
  .stat-value {
    font-size: 28px;
    font-weight: 700;
    color: #fafafa;
    font-variant-numeric: tabular-nums;
  }
  .stat-label {
    font-size: 11px;
    color: #52525b;
    text-transform: uppercase;
    letter-spacing: 1px;
    margin-top: 4px;
  }
</style>
</head>
<body>
  <div class="container">
    <div class="sidebar">
      <div class="logo">VoxDub</div>
      <div class="version">v0.2.0 · Whisper Small · MarianMT</div>
      <div class="status-pill"><div class="status-dot"></div>${label}</div>

      <button class="btn primary"
        ${state === "monitoring" || !canTransitionTo("monitoring") ? "disabled" : ""}
        onclick="window.voxdub.setState('monitoring')">
        Start Monitoring
      </button>
      <button class="btn danger"
        ${state === "idle" ? "disabled" : ""}
        onclick="window.voxdub.setState('idle')">
        Stop
      </button>
      <button class="btn"
        ${!canTransitionTo("dubbing") ? "disabled" : ""}
        onclick="window.voxdub.setState('dubbing')">
        Start Dubbing
      </button>

      <div class="spacer"></div>

      <button class="btn" onclick="window.voxdub.toggleOverlay()">Toggle Overlay</button>
      <button class="btn" onclick="window.voxdub.saveTranscript()">Save Transcript</button>
    </div>

    <div class="main-content">
      <div class="stats">
        <div class="stat-card">
          <div class="stat-value">${sessionCount}</div>
          <div class="stat-label">Detections</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">${lastLanguage ? lastLanguage.toUpperCase() : "—"}</div>
          <div class="stat-label">Last Language</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">${autoResume ? "ON" : "OFF"}</div>
          <div class="stat-label">Auto Resume</div>
        </div>
      </div>

      ${resultHtml}

      <div class="section-title">Event Log</div>
      <div class="log-container">
        ${logHtml}
      </div>
    </div>
  </div>
</body></html>`;
}

function getOverlayHtml(state: AppState): string {
  const label = getStateLabel(state);
  const color = getStateColor(state);
  const hasResult = lastTranslation && lastTranslation !== lastTranscription;

  return `<!DOCTYPE html>
<html><head><meta charset="UTF-8"/><title>VoxDub Overlay</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    background: #0a0a0fdd;
    color: #fff;
    font-family: system-ui, sans-serif;
    height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    -webkit-app-region: drag;
  }
  .card {
    padding: 14px 20px;
    border-radius: 12px;
    background: #18181b;
    border: 1px solid #27272a;
    min-width: 280px;
    text-align: center;
  }
  .state { font-size: 12px; font-weight: 600; color: ${color}; letter-spacing: 1px; text-transform: uppercase; }
  .text { font-size: 15px; margin-top: 8px; color: #fafafa; }
  .translation { font-size: 14px; margin-top: 6px; color: #22c55e; }
</style>
</head>
<body>
  <div class="card">
    <div class="state">${label}</div>
    ${hasResult
      ? `<div class="text">${escapeHtml(lastTranscription)}</div>
         <div class="translation">${escapeHtml(lastTranslation)}</div>`
      : lastTranscription
        ? `<div class="text">${escapeHtml(lastTranscription)}</div>`
        : `<div class="text" style="color:#52525b;">Listening...</div>`
    }
  </div>
</body></html>`;
}

// ━━ Window Management ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function updateUI(): void {
  const state = getState();
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.loadURL(htmlToDataUrl(getMainHtml(state)));
  }
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    overlayWindow.loadURL(htmlToDataUrl(getOverlayHtml(state)));
  }
  if (tray) {
    tray.setToolTip(`VoxDub — ${getStateLabel(state)}`);
  }
}

function changeState(newState: AppState): void {
  const old = getState();
  if (old === newState) return;
  if (!setState(newState)) {
    console.warn(`Blocked: ${old} → ${newState}`);
    return;
  }
  console.log(`State: ${old} → ${newState}`);
  addLine(`State: ${old} → ${newState}`);
  updateUI();
  if (newState === "detected") {
    sessionCount++;
    new Notification({ title: "VoxDub", body: "Speech detected — transcribing..." }).show();
  }
}

function sendStateCommand(target: AppState): void {
  const current = getState();
  if (current === target) return;
  if (!canTransitionTo(target)) return;

  switch (target) {
    case "monitoring":
      safeSend({ action: "set_default_input_device" });
      safeSend({ action: "start_monitoring" });
      break;
    case "detected":
      safeSend({ action: "simulate_detection" });
      break;
    case "dubbing":
      safeSend({ action: "start_dubbing" });
      break;
    case "idle":
      safeSend({ action: "stop" });
      break;
  }
}

// ━━ Worker Event Handler ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function handleWorkerEvent(event: WorkerEvent): void {
  // Ready gate
  if (event.type === "status" && event.message === "Worker ready") {
    workerReady = true;
    resetRestartCounter();
    addLine("Worker ready");
    flushPending();
    updateUI();
    return;
  }

  if (event.type === "state_change") {
    changeState(event.state);
    return;
  }

  if (event.type === "device_selected") {
    const d = event.data as { name?: string } | undefined;
    addLine(`Device: ${d?.name ?? event.message}`);
    updateUI();
    return;
  }

  if (event.type === "voice_activity") {
    addLine(event.message);
    updateUI();
    return;
  }

  if (event.type === "transcription") {
    const p = event.data as {
      text?: string; language?: string;
      language_probability?: number; low_confidence?: boolean;
    } | undefined;

    lastTranscription = p?.text ?? event.message;
    lastLanguage = p?.language ?? "?";
    const prob = p?.language_probability ?? 0;
    const low = p?.low_confidence ?? false;

    addLine(`[STT] ${lastLanguage.toUpperCase()} (${low ? "LOW" : prob.toFixed(2)}): ${lastTranscription}`);
    if (low) lastTranslation = "";
    updateUI();
    return;
  }

  if (event.type === "translation") {
    const p = event.data as {
      translated_text?: string; source_language?: string; target_language?: string;
    } | undefined;

    lastTranslation = p?.translated_text ?? event.message;
    const src = p?.source_language ?? "?";
    const tgt = p?.target_language ?? "en";

    if (src !== tgt) {
      addLine(`[TL] ${src}→${tgt}: ${lastTranslation}`);
    }
    updateUI();
    return;
  }

  if (event.type === "status") {
    const m = event.message;
    // Only log interesting statuses
    if (m.includes("Load") || m.includes("loaded") || m.includes("confidence") ||
        m.includes("skip") || m.includes("fail") || m.includes("error") ||
        m.includes("Auto-resumed") || m.includes("hint")) {
      addLine(m);
      updateUI();
    }
  }
}

// ━━ Window Creation ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function createMainWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1080, height: 720,
    minWidth: 800, minHeight: 500,
    show: true,
    backgroundColor: "#0a0a0f",
    webPreferences: {
      preload: path.join(__dirname, "../preload/preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.loadURL(htmlToDataUrl(getMainHtml(getState())));
  mainWindow.on("close", (e) => {
    if (!isQuitting) { e.preventDefault(); mainWindow?.hide(); }
  });
}

function createOverlayWindow(): void {
  overlayWindow = new BrowserWindow({
    width: 380, height: 110,
    show: false, frame: false, transparent: true,
    alwaysOnTop: true, resizable: false, movable: true, skipTaskbar: true,
    webPreferences: {
      preload: path.join(__dirname, "../preload/preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  overlayWindow.loadURL(htmlToDataUrl(getOverlayHtml(getState())));
}

function toggleOverlay(): void {
  if (!overlayWindow) return;
  overlayWindow.isVisible() ? overlayWindow.hide() : (overlayWindow.show(), overlayWindow.focus());
}

function createTray(): void {
  const icon = nativeImage.createFromDataURL(
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wn2SfcAAAAASUVORK5CYII="
  );
  tray = new Tray(icon);
  tray.setToolTip(`VoxDub — ${getStateLabel(getState())}`);
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Open VoxDub", click: () => { mainWindow?.show(); mainWindow?.focus(); } },
    { type: "separator" },
    { label: "Start Monitoring", click: () => sendStateCommand("monitoring") },
    { label: "Stop", click: () => sendStateCommand("idle") },
    { type: "separator" },
    { label: "Toggle Overlay", click: () => toggleOverlay() },
    { label: "Save Transcript", click: () => {
      const p = saveTranscript(getState(), liveSessionLines);
      new Notification({ title: "VoxDub", body: `Saved: ${p}` }).show();
    }},
    { type: "separator" },
    { label: "Quit", click: () => { isQuitting = true; app.quit(); } },
  ]));
  tray.on("click", () => { mainWindow?.show(); mainWindow?.focus(); });
}

// ━━ App Lifecycle ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

app.whenReady().then(() => {
  forceState("idle");
  liveSessionLines = [];
  workerReady = false;
  pendingCommands = [];
  sessionCount = 0;
  lastTranscription = "";
  lastTranslation = "";
  lastLanguage = "";

  createMainWindow();
  createOverlayWindow();
  createTray();
  startPythonWorker(handleWorkerEvent);

  ipcMain.handle("set-state", async (_, s: AppState) => sendStateCommand(s));
  ipcMain.handle("test-notification", async () =>
    new Notification({ title: "VoxDub", body: "Test notification" }).show());
  ipcMain.handle("toggle-overlay", async () => toggleOverlay());
  ipcMain.handle("save-transcript", async () => saveTranscript(getState(), liveSessionLines));
  ipcMain.handle("list-audio-devices", async () => safeSend({ action: "list_audio_devices" }));
  ipcMain.handle("set-default-input-device", async () => safeSend({ action: "set_default_input_device" }));
  ipcMain.handle("set-auto-resume", async (_, enabled: boolean) => {
    autoResume = enabled;
    safeSend({ action: "set_auto_resume", enabled });
  });
});

app.on("before-quit", () => { isQuitting = true; stopPythonWorker(); });
app.on("window-all-closed", () => { /* keep alive in tray */ });