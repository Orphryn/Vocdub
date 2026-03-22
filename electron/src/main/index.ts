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

  mainWindow.loadURL(
    "data:text/html,<h1 style='font-family:sans-serif;padding:20px;'>VoxDub Main Window</h1>"
  );
}

function createOverlayWindow(): void {
  overlayWindow = new BrowserWindow({
    width: 320,
    height: 90,
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
    <body style="margin:0;background:#111;color:#fff;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;">
      <div style="padding:16px;border-radius:12px;background:#1e1e1e;">
        VoxDub Overlay
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
      label: "Show Overlay",
      click: () => {
        if (overlayWindow) {
          overlayWindow.show();
          overlayWindow.focus();
        }
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
        app.quit();
      }
    }
  ]);

  createdTray.setToolTip("VoxDub");
  createdTray.setContextMenu(contextMenu);

  createdTray.on("click", () => {
    if (overlayWindow) {
      if (overlayWindow.isVisible()) {
        overlayWindow.hide();
      } else {
        overlayWindow.show();
        overlayWindow.focus();
      }
    }
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