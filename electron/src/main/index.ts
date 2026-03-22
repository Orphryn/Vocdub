import { app, BrowserWindow, Tray, Menu, nativeImage, Notification, ipcMain } from "electron";
import path from "path";
import {
  AppState,
  getState,
  setState,
  getStateLabel,
  getStateColor,
  canTransitionTo,
  forceState
} from "./state-machine";
import { transcriptLines, saveTranscript } from "./transcript-store";
import {
  startPythonWorker,
  stopPythonWorker,
  sendCommand,
  WorkerEvent
} from "./python-worker";

let tray: Tray | null = null;
let mainWindow: BrowserWindow | null = null;
let overlayWindow: BrowserWindow | null = null;
let isQuitting = false;
let liveSessionLines: string[] = [];

function htmlToDataUrl(html: string): string {
  return `data:text/html;charset=UTF-8,${encodeURIComponent(html)}`;
}

function addLiveSessionLine(line: string): void {
  liveSessionLines.push(line);

  if (liveSessionLines.length > 30) {
    liveSessionLines = liveSessionLines.slice(-30);
  }
}

function getTranscriptHtml(state: AppState): string {
  const baseLines = transcriptLines[state].map(
    (line) =>
      `<div style="padding:8px 0;border-bottom:1px solid #eee;font-family:Consolas,monospace;font-size:14px;">${line}</div>`
  );

  const liveLines = liveSessionLines.map(
    (line) =>
      `<div style="padding:8px 0;border-bottom:1px solid #f0f0f0;font-family:Consolas,monospace;font-size:14px;color:#222;">${line}</div>`
  );

  return [...baseLines, ...liveLines].join("");
}

function isDisabled(targetState: AppState): boolean {
  const current = getState();
  return !canTransitionTo(targetState) && current !== targetState;
}

function getButtonStyle(disabled: boolean): string {
  return `
    padding:10px 16px;
    font-size:14px;
    cursor:${disabled ? "not-allowed" : "pointer"};
    opacity:${disabled ? "0.5" : "1"};
  `;
}

function getMainWindowHtml(state: AppState): string {
  const stateLabel = getStateLabel(state);
  const stateColor = getStateColor(state);

  const idleDisabled = isDisabled("idle");
  const monitoringDisabled = isDisabled("monitoring");
  const detectedDisabled = isDisabled("detected");
  const dubbingDisabled = isDisabled("dubbing");

  return `
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="UTF-8" />
        <title>VoxDub Control Center</title>
      </head>
      <body style="margin:0;font-family:Arial,sans-serif;background:#f4f4f4;color:#111;">
        <div style="padding:24px;">
          <h1>VoxDub Control Center</h1>
          <p>Status: <strong style="color:${stateColor};">${stateLabel}</strong></p>
          <p>This build adds real microphone energy detection and local Whisper transcription.</p>

          <div style="margin-top:24px;display:flex;gap:12px;flex-wrap:wrap;">
            <button ${idleDisabled ? "disabled" : ""} onclick="window.voxdub.setState('idle')" style="${getButtonStyle(idleDisabled)}">Stop / Set Idle</button>
            <button ${monitoringDisabled ? "disabled" : ""} onclick="window.voxdub.setState('monitoring')" style="${getButtonStyle(monitoringDisabled)}">Start Monitoring</button>
            <button ${detectedDisabled ? "disabled" : ""} onclick="window.voxdub.setState('detected')" style="${getButtonStyle(detectedDisabled)}">Manual Detect</button>
            <button ${dubbingDisabled ? "disabled" : ""} onclick="window.voxdub.setState('dubbing')" style="${getButtonStyle(dubbingDisabled)}">Start Dubbing</button>
            <button onclick="window.voxdub.listAudioDevices()" style="padding:10px 16px;font-size:14px;cursor:pointer;">List Audio Devices</button>
            <button onclick="window.voxdub.setDefaultInputDevice()" style="padding:10px 16px;font-size:14px;cursor:pointer;">Use Default Input</button>
            <button onclick="window.voxdub.testNotification()" style="padding:10px 16px;font-size:14px;cursor:pointer;">Test Notification</button>
            <button onclick="window.voxdub.toggleOverlay()" style="padding:10px 16px;font-size:14px;cursor:pointer;">Toggle Overlay</button>
            <button onclick="window.voxdub.saveTranscript()" style="padding:10px 16px;font-size:14px;cursor:pointer;">Save Transcript</button>
          </div>

          <div style="margin-top:28px;padding:16px;border-radius:12px;background:white;border:1px solid #ddd;max-width:780px;">
            <h3 style="margin-top:0;">Current Phase</h3>
            <ul>
              <li>Real microphone energy detection works</li>
              <li>Audio device discovery works</li>
              <li>Default input selection works</li>
              <li>Whisper transcription runs after voice detection</li>
              <li>Live session events appear below</li>
            </ul>
          </div>

          <div style="margin-top:24px;padding:16px;border-radius:12px;background:white;border:1px solid #ddd;max-width:780px;">
            <h3 style="margin-top:0;">Transcript Preview</h3>
            <div style="margin-top:12px;max-height:320px;overflow:auto;border:1px solid #eee;border-radius:8px;padding:0 12px;background:#fafafa;">
              ${getTranscriptHtml(state)}
            </div>
          </div>
        </div>
      </body>
    </html>
  `;
}

function getOverlayHtml(state: AppState): string {
  const stateLabel = getStateLabel(state);
  const stateColor = getStateColor(state);

  return `
    <!DOCTYPE html>
    <html>
      <head>
        <meta charset="UTF-8" />
        <title>VoxDub Overlay</title>
      </head>
      <body style="margin:0;background:#111;color:#fff;font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;">
        <div style="padding:16px 20px;border-radius:12px;background:#1e1e1e;box-shadow:0 4px 12px rgba(0,0,0,0.35);min-width:260px;text-align:center;">
          <div style="font-weight:bold;font-size:16px;">VoxDub Overlay</div>
          <div style="margin-top:8px;font-size:13px;color:#bbb;">Current State</div>
          <div style="margin-top:6px;font-size:15px;font-weight:bold;color:${stateColor};">${stateLabel}</div>
        </div>
      </body>
    </html>
  `;
}

function updateWindowsForState(): void {
  const state = getState();

  if (mainWindow) {
    mainWindow.loadURL(htmlToDataUrl(getMainWindowHtml(state)));
  }

  if (overlayWindow) {
    overlayWindow.loadURL(htmlToDataUrl(getOverlayHtml(state)));
  }

  if (tray) {
    tray.setToolTip(`VoxDub - ${getStateLabel(state)}`);
  }
}

function changeState(newState: AppState): void {
  const oldState = getState();

  if (oldState === newState) {
    console.log(`Ignoring duplicate state change: ${oldState} -> ${newState}`);
    return;
  }

  const changed = setState(newState);

  if (!changed) {
    console.warn(`Blocked invalid transition: ${oldState} -> ${newState}`);
    return;
  }

  console.log(`State changed: ${oldState} -> ${newState}`);
  addLiveSessionLine(`[State] ${oldState} -> ${newState}`);
  updateWindowsForState();

  if (newState === "detected") {
    showDetectionNotification();
  }
}

function sendStateCommand(targetState: AppState): void {
  const current = getState();

  if (current === targetState) {
    console.log(`Ignoring duplicate command for current state: ${targetState}`);
    return;
  }

  if (!canTransitionTo(targetState)) {
    console.warn(`Blocked invalid command transition: ${current} -> ${targetState}`);
    return;
  }

  if (targetState === "monitoring") {
    sendCommand({ action: "start_monitoring" });
  } else if (targetState === "detected") {
    sendCommand({ action: "simulate_detection" });
  } else if (targetState === "dubbing") {
    sendCommand({ action: "start_dubbing" });
  } else if (targetState === "idle") {
    sendCommand({ action: "stop" });
  }
}

function handleWorkerEvent(event: WorkerEvent): void {
  console.log("Worker event received:", event);

  if (event.type === "state_change") {
    changeState(event.state);
    return;
  }

  if (event.type === "audio_devices") {
    console.log("Audio devices:", event.data);
    addLiveSessionLine(`[AudioDevices] ${event.message}`);
    updateWindowsForState();
    return;
  }

  if (event.type === "device_selected") {
    console.log("Device selected:", event.data);
    addLiveSessionLine(`[Device] ${event.message}`);
    updateWindowsForState();
    new Notification({
      title: "VoxDub",
      body: event.message
    }).show();
    return;
  }

  if (event.type === "voice_activity") {
    console.log("Voice activity:", event.message);
    addLiveSessionLine(`[VoiceActivity] ${event.message}`);
    updateWindowsForState();
    return;
  }

  if (event.type === "transcription") {
    const payload = event.data as { text?: string; language?: string; language_probability?: number } | undefined;
    const text = payload?.text ?? event.message;
    const language = payload?.language ?? "unknown";
    const probability = payload?.language_probability ?? 0;

    console.log("Transcription:", payload);
    addLiveSessionLine(`[Transcription][${language} ${probability.toFixed(2)}] ${text}`);
    updateWindowsForState();
    return;
  }

  if (event.type === "status") {
    console.log("Worker status:", event.message);
    addLiveSessionLine(`[Status] ${event.message}`);
    updateWindowsForState();
  }
}

function showDetectionNotification(): void {
  const notification = new Notification({
    title: "VoxDub",
    body: "Foreign language detected. Start dubbing?"
  });

  notification.show();
}

function toggleOverlay(): void {
  if (!overlayWindow) return;

  if (overlayWindow.isVisible()) {
    overlayWindow.hide();
  } else {
    overlayWindow.show();
    overlayWindow.focus();
  }
}

function createMainWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1000,
    height: 820,
    show: true,
    webPreferences: {
      preload: path.join(__dirname, "../preload/preload.js"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  mainWindow.loadURL(htmlToDataUrl(getMainWindowHtml(getState())));

  mainWindow.on("close", (event) => {
    if (!isQuitting) {
      event.preventDefault();
      mainWindow?.hide();
    }
  });
}

function createOverlayWindow(): void {
  overlayWindow = new BrowserWindow({
    width: 340,
    height: 120,
    show: false,
    frame: false,
    transparent: false,
    alwaysOnTop: true,
    resizable: false,
    movable: true,
    skipTaskbar: true,
    webPreferences: {
      preload: path.join(__dirname, "../preload/preload.js"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  overlayWindow.loadURL(htmlToDataUrl(getOverlayHtml(getState())));
}

function createTray(): void {
  const icon = nativeImage.createFromDataURL(
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wn2SfcAAAAASUVORK5CYII="
  );

  const createdTray = new Tray(icon);
  tray = createdTray;

  const contextMenu = Menu.buildFromTemplate([
    {
      label: "Open VoxDub",
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
        }
      }
    },
    {
      label: "Stop / Set Idle",
      click: () => sendStateCommand("idle")
    },
    {
      label: "Start Monitoring",
      click: () => sendStateCommand("monitoring")
    },
    {
      label: "Manual Detect",
      click: () => sendStateCommand("detected")
    },
    {
      label: "Start Dubbing",
      click: () => sendStateCommand("dubbing")
    },
    {
      label: "List Audio Devices",
      click: () => {
        sendCommand({ action: "list_audio_devices" });
      }
    },
    {
      label: "Use Default Input",
      click: () => {
        sendCommand({ action: "set_default_input_device" });
      }
    },
    {
      label: "Toggle Overlay",
      click: () => {
        toggleOverlay();
      }
    },
    {
      label: "Test Detection Notification",
      click: () => {
        showDetectionNotification();
      }
    },
    {
      label: "Save Transcript",
      click: () => {
        const savedPath = saveTranscript(getState(), liveSessionLines);
        new Notification({
          title: "VoxDub",
          body: `Transcript saved to ${savedPath}`
        }).show();
      }
    },
    {
      type: "separator"
    },
    {
      label: "Quit VoxDub",
      click: () => {
        isQuitting = true;
        app.quit();
      }
    }
  ]);

  createdTray.setToolTip(`VoxDub - ${getStateLabel(getState())}`);
  createdTray.setContextMenu(contextMenu);

  createdTray.on("click", () => {
    toggleOverlay();
  });
}

app.whenReady().then(() => {
  forceState("idle");
  liveSessionLines = [];

  createMainWindow();
  createOverlayWindow();
  createTray();

  startPythonWorker(handleWorkerEvent);

  ipcMain.handle("set-state", async (_event, state: AppState) => {
    sendStateCommand(state);
  });

  ipcMain.handle("test-notification", async () => {
    showDetectionNotification();
  });

  ipcMain.handle("toggle-overlay", async () => {
    toggleOverlay();
  });

  ipcMain.handle("save-transcript", async () => {
    return saveTranscript(getState(), liveSessionLines);
  });

  ipcMain.handle("list-audio-devices", async () => {
    sendCommand({ action: "list_audio_devices" });
  });

  ipcMain.handle("set-default-input-device", async () => {
    sendCommand({ action: "set_default_input_device" });
  });
});

app.on("before-quit", () => {
  isQuitting = true;
  stopPythonWorker();
});

app.on("window-all-closed", () => {
  // Keep app alive in tray on Windows.
});