import { contextBridge } from "electron";

contextBridge.exposeInMainWorld("voxdub", {
  version: "0.1.0"
});