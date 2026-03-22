import { app, BrowserWindow, Tray, Menu, nativeImage, Notification, ipcMain } from "electron";
import path from "path";
import fs from "fs";
import os from "os";

let tray: Tray | null = null;
let mainWindow: BrowserWindow | null = null;
let overlayWindow: BrowserWindow | null = null;
let isQuitting = false;

type AppState = "idle" | "monitoring" | "detected" | "dubbing";

let currentState: AppState = "idle";

const transcriptLines: Record<AppState, string[]> = {
  idle: [
    "[System] VoxDub is idle.",
    "[System] Waiting for media monitoring to begin."
  ],
  monitoring: [
    "[System] Monitoring browser and local video audio...",
    "[Detector] Speech activity check active.",
    "[Detector] Language identification pending."
  ],
  detected: [
    "[Detector] Foreign language detected: Spanish",
    "[Detector] Confidence: 0.91",
    "[Prompt] Ready to start dubbing."
  ],
  dubbing: [
    "[STT] Hola, bienvenidos a nuestro programa.",
    "[Translate] Hello, welcome to our program.",
    "[TTS] Dubbed voice playback started.",
    "[System] Overlay active."
  ]
};

function htmlToDataUrl(html: string): string {
  return `data:text/html;charset=UTF-8,${encodeURIComponent(html)}`;
}

function getStateLabel(state: AppState): string {
  switch (state) {
    case "idle":
      return "Idle";
    case "monitoring":
      return "Monitoring";
    case "detected":
      return "Foreign Language Detected";
    case "dubbing":
      return "Dubbing Active";
    default:
      return "Unknown";
  }
}

function getStateColor(state: AppState): string {
  switch (state) {
    case "idle":
      return "#111";
    case "monitoring":
      return "#0b6bcb";
    case "detected":
      return "#d97706";
    case "dubbing":
      return "#15803d";
    default:
      return "#111";
  }
}

function getTranscriptHtml(state: AppState): string {
  return transcriptLines[state]
    .map(
      (line) =>
        `<div style="padding:8px 0;border-bottom:1px solid #eee;font-family:Consolas,monospace;font-size:14px;">${line}</div>`
    )
    .join("");
}

function getMainWindowHtml(state: AppState): string {
  const stateLabel = getStateLabel(state);
  const stateColor = getStateColor(state);

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
          <p>This is the Phase 3 VoxDub shell with simulated app states and transcript export.</p>

          <div style="margin-top:24px;display:flex;gap:12px;flex-wrap:wrap;">
            <button onclick="window.voxdub.setState('idle')" style="padding:10px 16px;font-size:14px;cursor:pointer;">Set Idle</button>
            <button onclick="window.voxdub.setState('monitoring')" style="padding:10px 16px;font-size:14px;cursor:pointer;">Start Monitoring</button>
            <button onclick="window.voxdub.setState('detected')" style="padding:10px 16px;font-size:14px;cursor:pointer;">Simulate Detection</button>
            <button onclick="window.voxdub.setState('dubbing')" style="padding:10px 16px;font-size:14px;cursor:pointer;">Start Dubbing</button>
            <button onclick="window.voxdub.testNotification()" style="padding:10px 16px;font-size:14px;cursor:pointer;">Test Notification</button>
            <button onclick="window.voxdub.toggleOverlay()" style="padding:10px 16px;font-size:14px;cursor:pointer;">Toggle Overlay</button>
            <button onclick="window.voxdub.saveTranscript()" style="padding:10px 16px;font-size:14px;cursor:pointer;">Save Transcript</button>
          </div>

          <div style="margin-top:28px;padding:16px;border-radius:12px;background:white;border:1px solid #ddd;max-width:760px;">
            <h3 style="margin-top:0;">Current Phase</h3>
            <ul>
              <li>Main window works</li>
              <li>Tray menu works</li>
              <li>Overlay window works</li>
              <li>Notification test works</li>
              <li>App state simulation works</li>
              <li>Transcript export works</li>
            </ul>
          </div>

          <div style="margin-top:24px;padding:16px;border-radius:12px;background:white;border:1px solid #ddd;max-width:760px;">
            <h3 style="margin-top:0;">Transcript Preview</h3>
            <div style="margin-top:12px;max-height:220px;overflow:auto;border:1px solid #eee;border-radius:8px;padding:0 12px;background:#fafafa;">
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
  if (mainWindow) {
    mainWindow.loadURL(htmlToDataUrl(getMainWindowHtml(currentState)));
  }

  if (overlayWindow) {
    overlayWindow.loadURL(htmlToDataUrl(getOverlayHtml(currentState)));
  }

  if (tray) {
    tray.setToolTip(`VoxDub - ${getStateLabel(currentState)}`);
  }
}

function setState(newState: AppState): void {
  currentState = newState;
  updateWindowsForState();

  if (newState === "detected") {
    showDetectionNotification();
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

function saveTranscriptToFile(): string {
  const documentsPath = path.join(os.homedir(), "Documents");
  const voxDubFolder = path.join(documentsPath, "VoxDub");

  if (!fs.existsSync(voxDubFolder)) {
    fs.mkdirSync(voxDubFolder, { recursive: true });
  }

  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const filePath = path.join(voxDubFolder, `transcript-${currentState}-${timestamp}.txt`);

  const contents = [
    `VoxDub Transcript Export`,
    `State: ${getStateLabel(currentState)}`,
    `Created: ${new Date().toString()}`,
    ``,
    ...transcriptLines[currentState]
  ].join("\n");

  fs.writeFileSync(filePath, contents, "utf-8");

  return filePath;
}

function createMainWindow(): void {
  mainWindow = new BrowserWindow({
    width: 950,
    height: 760,
    show: true,
    webPreferences: {
      preload: path.join(__dirname, "../preload/preload.js"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  mainWindow.loadURL(htmlToDataUrl(getMainWindowHtml(currentState)));

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

  overlayWindow.loadURL(htmlToDataUrl(getOverlayHtml(currentState)));
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
      label: "Set Idle",
      click: () => setState("idle")
    },
    {
      label: "Start Monitoring",
      click: () => setState("monitoring")
    },
    {
      label: "Simulate Detection",
      click: () => setState("detected")
    },
    {
      label: "Start Dubbing",
      click: () => setState("dubbing")
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
        const savedPath = saveTranscriptToFile();
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

  createdTray.setToolTip(`VoxDub - ${getStateLabel(currentState)}`);
  createdTray.setContextMenu(contextMenu);

  createdTray.on("click", () => {
    toggleOverlay();
  });
}

app.whenReady().then(() => {
  createMainWindow();
  createOverlayWindow();
  createTray();

  ipcMain.handle("set-state", async (_event, state: AppState) => {
    setState(state);
  });

  ipcMain.handle("test-notification", async () => {
    showDetectionNotification();
  });

  ipcMain.handle("toggle-overlay", async () => {
    toggleOverlay();
  });

  ipcMain.handle("save-transcript", async () => {
    return saveTranscriptToFile();
  });
});

app.on("before-quit", () => {
  isQuitting = true;
});

app.on("window-all-closed", () => {
  // Keep app alive in tray on Windows.
});