import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("voxdub", {
  version: "0.1.0",
  setState: (state: "idle" | "monitoring" | "detected" | "dubbing") =>
    ipcRenderer.invoke("set-state", state),
  testNotification: () => ipcRenderer.invoke("test-notification"),
  toggleOverlay: () => ipcRenderer.invoke("toggle-overlay")
});