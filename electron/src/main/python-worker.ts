import { ChildProcessWithoutNullStreams, spawn } from "child_process";
import path from "path";

export type WorkerEvent = {
  type: string;
  state: "idle" | "monitoring" | "detected" | "dubbing";
  message: string;
  data?: unknown;
};

let workerProcess: ChildProcessWithoutNullStreams | null = null;
let lastOnEvent: ((event: WorkerEvent) => void) | null = null;
let intentionallyStopped = false;
let restartCount = 0;
const MAX_RESTARTS = 5;
const RESTART_DELAY_MS = 2000;

export function startPythonWorker(onEvent: (event: WorkerEvent) => void): void {
  if (workerProcess) {
    console.log("Python worker already running");
    return;
  }

  lastOnEvent = onEvent;
  intentionallyStopped = false;

  const workerPath = path.join(
    process.cwd(), "..", "local-agent", "src", "audio", "worker.py"
  );

  console.log("Starting Python worker at:", workerPath);

  workerProcess = spawn("python", [workerPath], {
    stdio: ["pipe", "pipe", "pipe"],
    env: {
      ...process.env,
      PYTHONIOENCODING: "utf-8",
      PYTHONUTF8: "1",
      LANG: "en_US.UTF-8",
      HF_HUB_DISABLE_SYMLINKS_WARNING: "1",
      TOKENIZERS_PARALLELISM: "false",
    },
  });

  workerProcess.stdout.setEncoding("utf8");
  workerProcess.stderr.setEncoding("utf8");

  let buffer = "";

  workerProcess.stdout.on("data", (chunk: string) => {
    buffer += chunk;
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const parsed: WorkerEvent = JSON.parse(trimmed);
        onEvent(parsed);
      } catch {
        // Non-JSON output (progress bars, etc.)
      }
    }
  });

  workerProcess.stderr.on("data", (data: string) => {
    const msg = data.trim();
    if (!msg) return;
    if (msg.includes("UserWarning:")) return;
    if (msg.includes("Loading weights:")) return;
    if (msg.includes("tie_word_embeddings")) return;
    if (msg.includes("FutureWarning")) return;
    console.error("Python worker stderr:", msg);
  });

  workerProcess.on("close", (code) => {
    console.log(`Python worker exited with code ${code}`);
    workerProcess = null;

    if (!intentionallyStopped && lastOnEvent && restartCount < MAX_RESTARTS) {
      restartCount++;
      console.log(`Auto-restarting worker (attempt ${restartCount}/${MAX_RESTARTS})...`);
      setTimeout(() => {
        if (!intentionallyStopped && lastOnEvent) {
          startPythonWorker(lastOnEvent);
        }
      }, RESTART_DELAY_MS);
    } else if (restartCount >= MAX_RESTARTS) {
      console.error("Python worker exceeded max restarts. Manual restart required.");
    }
  });
}

export function sendCommand(command: object): void {
  if (!workerProcess) {
    console.warn("Worker not running. Ignored:", command);
    return;
  }
  workerProcess.stdin.write(JSON.stringify(command) + "\n");
}

export function stopPythonWorker(): void {
  intentionallyStopped = true;
  if (workerProcess) {
    try {
      workerProcess.stdin.write(JSON.stringify({ action: "stop" }) + "\n");
    } catch { /* ignore */ }
    setTimeout(() => {
      if (workerProcess) {
        workerProcess.kill();
        workerProcess = null;
      }
    }, 500);
  }
}

export function resetRestartCounter(): void {
  restartCount = 0;
}