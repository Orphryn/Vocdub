import { ChildProcessWithoutNullStreams, spawn } from "child_process";
import path from "path";

export type WorkerEvent = {
  type: string;
  state: "idle" | "monitoring" | "detected" | "dubbing";
  message: string;
};

let workerProcess: ChildProcessWithoutNullStreams | null = null;

export function startPythonWorker(onEvent: (event: WorkerEvent) => void): void {
  if (workerProcess) return;

  const workerPath = path.join(
    process.cwd(),
    "..",
    "local-agent",
    "src",
    "audio",
    "worker.py"
  );

  workerProcess = spawn("python", [workerPath]);

  workerProcess.stdout.on("data", (data: Buffer) => {
    const lines = data.toString().trim().split(/\r?\n/);

    for (const line of lines) {
      try {
        const parsed: WorkerEvent = JSON.parse(line);
        onEvent(parsed);
      } catch {
        console.error("Bad JSON:", line);
      }
    }
  });

  workerProcess.stderr.on("data", (data: Buffer) => {
    console.error("Python error:", data.toString());
  });

  workerProcess.on("close", () => {
    workerProcess = null;
  });
}

export function sendCommand(command: object): void {
  if (!workerProcess) return;

  workerProcess.stdin.write(JSON.stringify(command) + "\n");
}

export function stopPythonWorker(): void {
  if (workerProcess) {
    workerProcess.kill();
    workerProcess = null;
  }
}