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


def main() -> None:
    send_event("status", "idle", "Python worker started")
    time.sleep(2)

    send_event("state_change", "monitoring", "Monitoring started")
    time.sleep(3)

    send_event("state_change", "detected", "Foreign language detected")
    time.sleep(3)

    send_event("state_change", "dubbing", "Dubbing started")
    time.sleep(3)

    send_event("state_change", "idle", "Returning to idle")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)