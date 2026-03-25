import fs from "fs";
import path from "path";
import os from "os";
import { AppState, getStateLabel } from "./state-machine";

export const transcriptLines: Record<AppState, string[]> = {
  idle: ["[System] VoxDub idle — ready to monitor."],
  monitoring: ["[System] Monitoring audio input for speech..."],
  detected: ["[System] Speech detected — processing transcription."],
  dubbing: ["[System] Dubbing pipeline active."],
};

export function saveTranscript(state: AppState, liveLines: string[] = []): string {
  const folder = path.join(os.homedir(), "Documents", "VoxDub");
  if (!fs.existsSync(folder)) fs.mkdirSync(folder, { recursive: true });
  const ts = new Date().toISOString().replace(/[:.]/g, "-");
  const filePath = path.join(folder, `voxdub-${state}-${ts}.txt`);
  const content = [
    "═══════════════════════════════════════",
    "  VoxDub Transcript Export",
    "═══════════════════════════════════════",
    `  State: ${getStateLabel(state)}`,
    `  Time:  ${new Date().toLocaleString()}`,
    "═══════════════════════════════════════",
    "", ...transcriptLines[state], "",
    "─── Live Session ───", ...liveLines, "", "─── End ───",
  ].join("\n");
  fs.writeFileSync(filePath, content, "utf-8");
  return filePath;
}