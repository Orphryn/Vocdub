import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("voxdub", {
  version: "0.2.0",
  setState: (state: "idle" | "monitoring" | "detected" | "dubbing") =>
    ipcRenderer.invoke("set-state", state),
  testNotification: () => ipcRenderer.invoke("test-notification"),
  toggleOverlay: () => ipcRenderer.invoke("toggle-overlay"),
  saveTranscript: () => ipcRenderer.invoke("save-transcript"),
  listAudioDevices: () => ipcRenderer.invoke("list-audio-devices"),
  setDefaultInputDevice: () => ipcRenderer.invoke("set-default-input-device"),
  setAutoResume: (enabled: boolean) => ipcRenderer.invoke("set-auto-resume", enabled),
});