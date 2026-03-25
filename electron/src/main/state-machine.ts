export type AppState = "idle" | "monitoring" | "detected" | "dubbing";

let currentState: AppState = "idle";

const allowedTransitions: Record<AppState, AppState[]> = {
  idle: ["monitoring"],
  monitoring: ["detected", "idle"],
  detected: ["dubbing", "monitoring", "idle"],  // monitoring added for auto-resume
  dubbing: ["idle"],
};

export function getState(): AppState {
  return currentState;
}

export function canTransitionTo(nextState: AppState): boolean {
  if (nextState === currentState) return false;
  return allowedTransitions[currentState].includes(nextState);
}

export function setState(state: AppState): boolean {
  if (!canTransitionTo(state)) return false;
  currentState = state;
  return true;
}

export function forceState(state: AppState): void {
  currentState = state;
}

export function getStateLabel(state: AppState): string {
  switch (state) {
    case "idle": return "Idle";
    case "monitoring": return "Monitoring";
    case "detected": return "Speech Detected";
    case "dubbing": return "Dubbing Active";
  }
}

export function getStateColor(state: AppState): string {
  switch (state) {
    case "idle": return "#6b7280";
    case "monitoring": return "#3b82f6";
    case "detected": return "#f59e0b";
    case "dubbing": return "#22c55e";
  }
}