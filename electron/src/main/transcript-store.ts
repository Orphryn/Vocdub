import fs from "fs";
import path from "path";
import os from "os";
import { AppState, getStateLabel } from "./state-machine";

export const transcriptLines: Record<AppState, string[]> = {
  idle: [
    "[System] VoxDub is idle.",
    "[System] Waiting for media monitoring to begin."
  ],
  monitoring: [
    "[System] Monitoring selected audio input device...",
    "[Detector] Voice activity threshold armed.",
    "[Detector] Waiting for speech."
  ],
  detected: [
    "[Detector] Speech activity detected.",
    "[Detector] Foreign language detection trigger fired.",
    "[Prompt] Ready to start dubbing."
  ],
  dubbing: [
    "[STT] Local transcription pipeline active.",
    "[Translate] Translation pipeline not wired yet.",
    "[TTS] Dubbed playback pipeline not wired yet.",
    "[System] Overlay active."
  ]
};

export function saveTranscript(state: AppState, extraLines: string[] = []): string {
  const documentsPath = path.join(os.homedir(), "Documents");
  const folder = path.join(documentsPath, "VoxDub");

  if (!fs.existsSync(folder)) {
    fs.mkdirSync(folder, { recursive: true });
  }

  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
  const filePath = path.join(folder, `transcript-${state}-${timestamp}.txt`);

  const content = [
    "VoxDub Transcript Export",
    `State: ${getStateLabel(state)}`,
    `Created: ${new Date().toString()}`,
    "",
    ...transcriptLines[state],
    "",
    "[Live Session Events]",
    ...extraLines
  ].join("\n");

  fs.writeFileSync(filePath, content, "utf-8");

  return filePath;
}