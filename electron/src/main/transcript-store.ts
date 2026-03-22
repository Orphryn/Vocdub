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

export function saveTranscript(state: AppState): string {
  const documentsPath = path.join(os.homedir(), "Documents");
  const folder = path.join(documentsPath, "VoxDub");

  if (!fs.existsSync(folder)) {
    fs.mkdirSync(folder, { recursive: true });
  }

  const timestamp = new Date().toISOString().replace(/[:.]/g, "-");

  const filePath = path.join(
    folder,
    `transcript-${state}-${timestamp}.txt`
  );

  const content = [
    "VoxDub Transcript Export",
    `State: ${getStateLabel(state)}`,
    `Created: ${new Date().toString()}`,
    "",
    ...transcriptLines[state]
  ].join("\n");

  fs.writeFileSync(filePath, content, "utf-8");

  return filePath;
}