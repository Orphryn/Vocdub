import { app, BrowserWindow, Tray, Menu, nativeImage, Notification } from "electron";
import path from "path";

let tray: Tray | null = null;
let mainWindow: BrowserWindow | null = null;
let overlayWindow: BrowserWindow | null = null;

function createMainWindow(): void {
  mainWindow = new BrowserWindow({
    width: 900,
    height: 600,
    show: true,
    webPreferences: {
      preload: path.join(__dirname, "../preload/preload.js"),
      contextIsolation: true,
      nodeIntegration: false
    }
  });

  mainWindow.loadURL(`
    data:text/html,
    <body style="margin:0;font-family:Arial,sans-serif;background:#f4f4f4;color:#111;">
      <div style="padding:24px;">
        <h1>VoxDub Control Center</h1>
        <p>Status: <strong>Idle</strong></p>
        <p>This is the Phase 1 Electron shell.</p>
        <ul>
          <li>Main window works</li>
          <li>Tray menu works</li>
          <li>Overlay window works</li>
          <li>Notification test works</li>
        </ul>
      </div>
    </body>
  `);

  mainWindow.on("close", (event) => {
    event.preventDefault();
    mainWindow?.hide();
  });
}

function createOverlayWindow(): void {
  overlayWindow = new BrowserWindow({
    width: 340,
    height: 100,
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

  overlayWindow.loadURL(`
    data:text/html,
    <body style="margin:0;background:#111;color:#fff;font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;">
      <div style="padding:16px 20px;border-radius:12px;background:#1e1e1e;box-shadow:0 4px 12px rgba(0,0,0,0.35);">
        <strong>VoxDub Overlay</strong><br />
        <span style="font-size:13px;color:#bbb;">Ready for live dubbing controls</span>
      </div>
    </body>
  `);
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
      type: "separator"
    },
    {
      label: "Quit VoxDub",
      click: () => {
        app.exit();
      }
    }
  ]);

  createdTray.setToolTip("VoxDub");
  createdTray.setContextMenu(contextMenu);

  createdTray.on("click", () => {
    toggleOverlay();
  });
}

app.whenReady().then(() => {
  createMainWindow();
  createOverlayWindow();
  createTray();
});

app.on("window-all-closed", () => {
  // Keep app alive in tray on Windows.
});