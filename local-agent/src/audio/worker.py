import json
import sys
import time


def send_event(event_type: str, state: str, message: str) -> None:
    payload = {
        "type": event_type,
        "state": state,
        "message": message
    }
    print(json.dumps(payload), flush=True)


def handle_command(command: dict) -> None:
    action = command.get("action")

    if action == "start_monitoring":
        send_event("state_change", "monitoring", "Monitoring started")

    elif action == "detect_language":
        time.sleep(1)
        send_event("state_change", "detected", "Foreign language detected")

    elif action == "start_dubbing":
        send_event("state_change", "dubbing", "Dubbing started")

    elif action == "stop":
        send_event("state_change", "idle", "Stopped")

    else:
        send_event("status", "idle", f"Unknown command: {action}")


def main() -> None:
    send_event("status", "idle", "Worker ready")

    while True:
        line = sys.stdin.readline()

        if not line:
            break

        try:
            command = json.loads(line.strip())
            handle_command(command)
        except Exception as e:
            send_event("status", "idle", f"Error: {str(e)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)