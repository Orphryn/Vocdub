import json
import sys
import threading

try:
    import numpy as np
except ImportError:
    np = None

try:
    import sounddevice as sd
except ImportError:
    sd = None

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None


current_state = "idle"
is_monitoring = False
selected_input_device = None
input_stream = None

voice_active_frames = 0
audio_buffer = []
transcription_in_progress = False

speech_started = False
silence_frames = 0
post_trigger_frames = 0

lock = threading.Lock()

VOICE_THRESHOLD = 0.02
VOICE_FRAMES_REQUIRED = 5
POST_SPEECH_SILENCE_FRAMES = 20      # ~1.28s silence at 1024/16000
MAX_POST_TRIGGER_FRAMES = 70         # ~4.5s max after speech starts
PRE_ROLL_BLOCKS = 40                 # keep ~2.5s before trigger
MAX_BUFFER_BLOCKS = 220              # total rolling buffer cap

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


def transcribe_audio_chunks(chunks):
    global transcription_in_progress

    send_event("status", current_state, "Transcription thread started")

    try:
        if np is None:
            send_event("status", current_state, "numpy is not installed")
            return

        if WhisperModel is None:
            send_event("status", current_state, "faster-whisper is not installed")
            return

        if not chunks:
            send_event("status", current_state, "No buffered audio to transcribe")
            return

        send_event("status", current_state, f"Transcription received {len(chunks)} audio chunks")
        send_event("status", current_state, "Loading Whisper model (base, int8 on CPU)...")

        model = WhisperModel("base", device="cpu", compute_type="int8")

        audio_data = np.concatenate(chunks, axis=0).astype("float32").flatten()
        send_event("status", current_state, f"Audio buffer length for transcription: {len(audio_data)} samples")

        segments, info = model.transcribe(
            audio_data,
            beam_size=1,
            vad_filter=True
        )

        text_parts = []
        for segment in segments:
            cleaned = segment.text.strip()
            if cleaned:
                text_parts.append(cleaned)

        text = " ".join(text_parts).strip()

        send_event(
            "status",
            current_state,
            f"Whisper finished. Extracted {len(text_parts)} text segments"
        )

        send_event(
            "transcription",
            "detected",
            text if text else "No speech recognized",
            data={
                "text": text,
                "language": getattr(info, "language", "unknown"),
                "language_probability": getattr(info, "language_probability", 0.0)
            }
        )

    except Exception as e:
        send_event("status", current_state, f"Transcription failed: {str(e)}")
    finally:
        with lock:
            transcription_in_progress = False


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


def finalize_utterance(level: float, buffered_chunks):
    global current_state
    global is_monitoring
    global transcription_in_progress

    send_event("voice_activity", "monitoring", f"Utterance finalized (level={level:.4f})")
    send_event("state_change", "detected", "Foreign language detected from live microphone activity")

    stop_input_stream()

    thread = threading.Thread(target=transcribe_audio_chunks, args=(buffered_chunks,), daemon=True)
    thread.start()


def audio_callback(indata, frames, time_info, status):
    global current_state
    global is_monitoring
    global voice_active_frames
    global audio_buffer
    global transcription_in_progress
    global speech_started
    global silence_frames
    global post_trigger_frames

    if status:
        return

    if np is None:
        return

    try:
        buffer_copy = indata.copy()
        level = float(np.sqrt(np.mean(buffer_copy ** 2)))
    except Exception:
        return

    buffered_chunks = None
    should_finalize = False

    with lock:
        if current_state != "monitoring" or not is_monitoring:
            return

        audio_buffer.append(buffer_copy)
        if len(audio_buffer) > MAX_BUFFER_BLOCKS:
            audio_buffer = audio_buffer[-MAX_BUFFER_BLOCKS:]

        if level > VOICE_THRESHOLD:
            voice_active_frames += 1
        else:
            voice_active_frames = max(0, voice_active_frames - 1)

        if transcription_in_progress:
            return

        # Before speech starts: keep only rolling pre-roll
        if not speech_started:
            if len(audio_buffer) > PRE_ROLL_BLOCKS:
                audio_buffer = audio_buffer[-PRE_ROLL_BLOCKS:]

            if voice_active_frames >= VOICE_FRAMES_REQUIRED:
                speech_started = True
                silence_frames = 0
                post_trigger_frames = 0
                send_event("voice_activity", "monitoring", f"Speech started (level={level:.4f})")

            return

        # After speech has started: keep collecting until silence or max length
        post_trigger_frames += 1

        if level > VOICE_THRESHOLD:
            silence_frames = 0
        else:
            silence_frames += 1

        if silence_frames >= POST_SPEECH_SILENCE_FRAMES or post_trigger_frames >= MAX_POST_TRIGGER_FRAMES:
            transcription_in_progress = True
            current_state = "detected"
            is_monitoring = False
            buffered_chunks = list(audio_buffer)
            audio_buffer = []
            should_finalize = True

    if should_finalize and buffered_chunks is not None:
        finalize_utterance(level, buffered_chunks)


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


def reset_monitoring_state():
    global voice_active_frames
    global audio_buffer
    global transcription_in_progress
    global speech_started
    global silence_frames
    global post_trigger_frames

    voice_active_frames = 0
    audio_buffer = []
    transcription_in_progress = False
    speech_started = False
    silence_frames = 0
    post_trigger_frames = 0


def handle_command(command: dict) -> None:
    global current_state
    global is_monitoring

    action = command.get("action")

    if action == "start_monitoring":
        with lock:
            if current_state == "monitoring":
                send_event("status", current_state, "Already monitoring")
                return

            is_monitoring = True
            reset_monitoring_state()
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
            reset_monitoring_state()
            current_state = "idle"

        stop_input_stream()
        send_event("state_change", "idle", "Stopped")

    elif action == "simulate_detection":
        with lock:
            if current_state != "monitoring":
                send_event("status", current_state, "Cannot simulate detection unless monitoring")
                return

            is_monitoring = False
            current_state = "detected"

        stop_input_stream()
        send_event("state_change", "detected", "Foreign language detected manually")

    elif action == "list_audio_devices":
        list_audio_devices()

    elif action == "set_default_input_device":
        set_default_input_device()

    else:
        send_event("status", current_state, f"Unknown command: {action}")


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
            send_event("status", current_state, f"Error: {str(e)}")

    stop_input_stream()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        stop_input_stream()
        sys.exit(0)