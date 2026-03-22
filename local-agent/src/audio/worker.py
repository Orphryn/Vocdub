import json
import sys
import time
import threading

try:
    import sounddevice as sd
except ImportError:
    sd = None


current_state = "idle"
is_monitoring = False
detection_sent = False
lock = threading.Lock()


def send_event(event_type: str, state: str, message: str, data=None) -> None:
    payload = {
        "type": event_type,
        "state": state,
        "message": message
    }

    if data is not None:
        payload["data"] = data

    print(json.dumps(payload), flush=True)


def monitoring_loop() -> None:
    global current_state
    global is_monitoring
    global detection_sent

    while True:
        time.sleep(1)

        with lock:
            if not is_monitoring:
                continue

            if current_state != "monitoring":
                continue

            if detection_sent:
                continue

        time.sleep(2)

        with lock:
            if not is_monitoring:
                continue

            if current_state != "monitoring":
                continue

            if detection_sent:
                continue

            detection_sent = True
            current_state = "detected"

        send_event("state_change", "detected", "Foreign language detected automatically")


def list_audio_devices() -> None:
    if sd is None:
        send_event(
            "audio_devices",
            current_state,
            "sounddevice is not installed",
            data=[]
        )
        return

    try:
        devices = sd.query_devices()
        simplified = []

        for index, device in enumerate(devices):
            simplified.append({
                "index": index,
                "name": device.get("name", "Unknown"),
                "max_input_channels": device.get("max_input_channels", 0),
                "max_output_channels": device.get("max_output_channels", 0),
                "default_samplerate": device.get("default_samplerate", 0)
            })

        send_event(
            "audio_devices",
            current_state,
            f"Found {len(simplified)} audio devices",
            data=simplified
        )
    except Exception as e:
        send_event(
            "audio_devices",
            current_state,
            f"Failed to list audio devices: {str(e)}",
            data=[]
        )


def handle_command(command: dict) -> None:
    global current_state
    global is_monitoring
    global detection_sent

    action = command.get("action")

    if action == "start_monitoring":
        with lock:
            if current_state == "monitoring":
                send_event("status", current_state, "Already monitoring")
                return

            is_monitoring = True
            detection_sent = False
            current_state = "monitoring"

        send_event("state_change", "monitoring", "Monitoring started")

    elif action == "start_dubbing":
        with lock:
            if current_state != "detected":
                send_event("status", current_state, "Cannot start dubbing unless language is detected")
                return

            current_state = "dubbing"

        send_event("state_change", "dubbing", "Dubbing started")

    elif action == "stop":
        with lock:
            is_monitoring = False
            detection_sent = False
            current_state = "idle"

        send_event("state_change", "idle", "Stopped")

    elif action == "simulate_detection":
        with lock:
            if current_state != "monitoring":
                send_event("status", current_state, "Cannot simulate detection unless monitoring")
                return

            detection_sent = True
            current_state = "detected"

        send_event("state_change", "detected", "Foreign language detected manually")

    elif action == "list_audio_devices":
        list_audio_devices()

    else:
        send_event("status", current_state, f"Unknown command: {action}")


def main() -> None:
    send_event("status", "idle", "Worker ready")

    thread = threading.Thread(target=monitoring_loop, daemon=True)
    thread.start()

    while True:
        line = sys.stdin.readline()

        if not line:
            break

        try:
            command = json.loads(line.strip())
            handle_command(command)
        except Exception as e:
            send_event("status", current_state, f"Error: {str(e)}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)