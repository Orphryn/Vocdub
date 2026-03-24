export type AppState = "idle" | "monitoring" | "detected" | "dubbing";

let currentState: AppState = "idle";

const TRANSITIONS: Record<AppState, AppState[]> = {
  idle: ["monitoring"],
  monitoring: ["detected", "idle"],
  detected: ["dubbing", "monitoring", "idle"],  // added monitoring for auto-resume
  dubbing: ["idle"],
};

export function getState(): AppState {
  return currentState;
}

export function canTransitionTo(next: AppState): boolean {
  return next !== currentState && TRANSITIONS[currentState].includes(next);
}

export function setState(state: AppState): boolean {
  if (!canTransitionTo(state)) return false;
  currentState = state;
  return true;
}

export function forceState(state: AppState): void {
  currentState = state;
}

const STATE_LABELS: Record<AppState, string> = {
  idle: "Idle",
  monitoring: "Monitoring",
  detected: "Speech Detected",
  dubbing: "Dubbing Active",
};

const STATE_COLORS: Record<AppState, string> = {
  idle: "#6b7280",
  monitoring: "#3b82f6",
  detected: "#f59e0b",
  dubbing: "#22c55e",
};

export function getStateLabel(state: AppState): string {
  return STATE_LABELS[state];
}

export function getStateColor(state: AppState): string {
  return STATE_COLORS[state];
}