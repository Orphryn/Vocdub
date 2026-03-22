import { ChildProcessWithoutNullStreams, spawn } from "child_process";
import path from "path";

export type WorkerEvent = {
  type: string;
  state: "idle" | "monitoring" | "detected" | "dubbing";
  message: string;
  data?: unknown;
};

let workerProcess: ChildProcessWithoutNullStreams | null = null;

export function startPythonWorker(onEvent: (event: WorkerEvent) => void): void {
  if (workerProcess) {
    console.log("Python worker already running");
    return;
  }

  const workerPath = path.join(
    process.cwd(),
    "..",
    "local-agent",
    "src",
    "audio",
    "worker.py"
  );

  console.log("Starting Python worker at:", workerPath);

  workerProcess = spawn("python", [workerPath]);

  workerProcess.stdout.on("data", (data: Buffer) => {
    const output = data.toString().trim();

    if (!output) {
      return;
    }

    const lines = output.split(/\r?\n/);

    for (const line of lines) {
      try {
        const parsed: WorkerEvent = JSON.parse(line);
        onEvent(parsed);
      } catch {
        console.error("Failed to parse Python worker output:", line);
      }
    }
  });

  workerProcess.stderr.on("data", (data: Buffer) => {
    console.error("Python worker stderr:", data.toString());
  });

  workerProcess.on("close", (code) => {
    console.log(`Python worker exited with code ${code}`);
    workerProcess = null;
  });
}

export function sendCommand(command: object): void {
  if (!workerProcess) {
    console.warn("Python worker is not running. Command ignored:", command);
    return;
  }

  console.log("Sending command to Python worker:", command);
  workerProcess.stdin.write(JSON.stringify(command) + "\n");
}

export function stopPythonWorker(): void {
  if (workerProcess) {
    workerProcess.kill();
    workerProcess = null;
  }
}