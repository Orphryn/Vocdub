import json
import sys
import time
import threading

try:
    import numpy as np
    import sounddevice as sd
except ImportError:
    np = None
    sd = None


current_state = "idle"
is_monitoring = False
detection_sent = False
selected_input_device = None
input_stream = None
voice_active_frames = 0
lock = threading.Lock()

VOICE_THRESHOLD = 0.02
VOICE_FRAMES_REQUIRED = 5
SAMPLE_RATE = 16000
CHANNELS = 1
BLOCKSIZE = 1024


def send_event(event_type: str, state: str, message: str, data=None) -> None:
    payload = {
        "type": event_type,
        "state": state,
        "message": message
    }

    if data is not None:
        payload["data"] = data

    print(json.dumps(payload), flush=True)


def get_input_devices():
    if sd is None:
        return []

    devices = sd.query_devices()
    results = []

    for index, device in enumerate(devices):
        if device.get("max_input_channels", 0) > 0:
            results.append({
                "index": index,
                "name": device.get("name", "Unknown"),
                "max_input_channels": device.get("max_input_channels", 0),
                "max_output_channels": device.get("max_output_channels", 0),
                "default_samplerate": device.get("default_samplerate", 0)
            })

    return results


def list_audio_devices() -> None:
    if sd is None:
      send_event("audio_devices", current_state, "sounddevice is not installed", data=[])
      return

    try:
        devices = get_input_devices()
        send_event(
            "audio_devices",
            current_state,
            f"Found {len(devices)} audio input devices",
            data=devices
        )
    except Exception as e:
        send_event(
            "audio_devices",
            current_state,
            f"Failed to list audio devices: {str(e)}",
            data=[]
        )


def set_default_input_device() -> None:
    global selected_input_device

    if sd is None:
        send_event("status", current_state, "sounddevice is not installed")
        return

    try:
        default_input = sd.default.device[0]
        devices = get_input_devices()

        if default_input is None or default_input == -1:
            if not devices:
                send_event("status", current_state, "No input devices available")
                return

            default_input = devices[0]["index"]

        selected_input_device = int(default_input)

        selected_name = "Unknown"
        for device in devices:
            if device["index"] == selected_input_device:
                selected_name = device["name"]
                break

        send_event(
            "device_selected",
            current_state,
            f"Using input device: {selected_name}",
            data={"index": selected_input_device, "name": selected_name}
        )
    except Exception as e:
        send_event("status", current_state, f"Failed to set default input device: {str(e)}")


def audio_callback(indata, frames, time_info, status):
    global current_state
    global is_monitoring
    global detection_sent
    global voice_active_frames

    if status:
        return

    if np is None:
        return

    try:
        level = float(np.sqrt(np.mean(indata ** 2)))
    except Exception:
        return

    with lock:
        if current_state != "monitoring" or not is_monitoring:
            return

        if level > VOICE_THRESHOLD:
            voice_active_frames += 1
        else:
            voice_active_frames = max(0, voice_active_frames - 1)

        if detection_sent:
            return

        if voice_active_frames < VOICE_FRAMES_REQUIRED:
            return

        detection_sent = True
        current_state = "detected"
        is_monitoring = False

    send_event("voice_activity", "monitoring", f"Voice activity threshold crossed (level={level:.4f})")
    send_event("state_change", "detected", "Foreign language detected from live microphone activity")


def start_input_stream_if_needed() -> None:
    global input_stream
    global selected_input_device

    if sd is None:
        send_event("status", current_state, "sounddevice is not installed")
        return

    if np is None:
        send_event("status", current_state, "numpy is not installed")
        return

    if selected_input_device is None:
        set_default_input_device()

    if selected_input_device is None:
        send_event("status", current_state, "No input device selected")
        return

    if input_stream is not None:
        return

    try:
        input_stream = sd.InputStream(
            device=selected_input_device,
            channels=CHANNELS,
            samplerate=SAMPLE_RATE,
            blocksize=BLOCKSIZE,
            dtype="float32",
            callback=audio_callback
        )
        input_stream.start()
        send_event("status", current_state, "Audio input stream started")
    except Exception as e:
        input_stream = None
        send_event("status", current_state, f"Failed to start input stream: {str(e)}")


def stop_input_stream() -> None:
    global input_stream

    if input_stream is not None:
        try:
            input_stream.stop()
            input_stream.close()
        except Exception:
            pass
        finally:
            input_stream = None


def handle_command(command: dict) -> None:
    global current_state
    global is_monitoring
    global detection_sent
    global voice_active_frames

    action = command.get("action")

    if action == "start_monitoring":
        with lock:
            if current_state == "monitoring":
                send_event("status", current_state, "Already monitoring")
                return

            is_monitoring = True
            detection_sent = False
            voice_active_frames = 0
            current_state = "monitoring"

        start_input_stream_if_needed()
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
            voice_active_frames = 0
            current_state = "idle"

        stop_input_stream()
        send_event("state_change", "idle", "Stopped")

    elif action == "simulate_detection":
        with lock:
            if current_state != "monitoring":
                send_event("status", current_state, "Cannot simulate detection unless monitoring")
                return

            detection_sent = True
            current_state = "detected"
            is_monitoring = False

        send_event("state_change", "detected", "Foreign language detected manually")

    elif action == "list_audio_devices":
        list_audio_devices()

    elif action == "set_default_input_device":
        set_default_input_device()

    else:
        send_event("status", current_state, f"Unknown command: {action}")


def health_loop() -> None:
    while True:
        time.sleep(1)

        with lock:
            active = is_monitoring
            state = current_state

        if active and state == "monitoring":
            pass


def main() -> None:
    send_event("status", "idle", "Worker ready")

    thread = threading.Thread(target=health_loop, daemon=True)
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

    stop_input_stream()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        stop_input_stream()
        sys.exit(0)