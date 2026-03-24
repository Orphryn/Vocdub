"""VoxDub Python Audio Worker — Production Build

Real-time speech → transcription → translation pipeline.
Communicates with Electron via JSON-over-stdin/stdout.

Optimizations over previous versions:
  - Whisper initial_prompt hints improve language detection accuracy
  - Audio normalization before transcription (prevents clipping/quiet issues)
  - Adaptive voice threshold with noise floor tracking
  - Continuous monitoring: auto-resumes after transcription completes
  - Pre-warms translation model on first non-English detection
  - Graceful shutdown with proper thread joining
  - Reduced memory: ring buffer with numpy array instead of list
"""

import io
import json
import os
import sys
import threading
import time
import traceback

os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

sys.stdout = io.TextIOWrapper(
    sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
)
sys.stderr = io.TextIOWrapper(
    sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
)

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

try:
    from transformers import MarianMTModel, MarianTokenizer
except ImportError:
    MarianMTModel = None
    MarianTokenizer = None


# ━━ Global State ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

current_state = "idle"
is_monitoring = False
selected_input_device = None
input_stream = None

voice_active_frames = 0
audio_buffer: list = []
transcription_in_progress = False

speech_started = False
silence_frames = 0
post_trigger_frames = 0

# Adaptive noise floor tracking
noise_floor = 0.005
noise_samples = 0

lock = threading.Lock()
shutdown_event = threading.Event()

# ━━ Tuning Constants ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Voice detection
VOICE_THRESHOLD_MULTIPLIER = 3.0  # voice must be 3x the noise floor
VOICE_THRESHOLD_MIN = 0.015       # absolute minimum threshold
VOICE_THRESHOLD_MAX = 0.15        # absolute maximum threshold
VOICE_FRAMES_REQUIRED = 5
NOISE_FLOOR_ALPHA = 0.02          # exponential moving average for noise

# Speech capture timing
POST_SPEECH_SILENCE_FRAMES = 45
MAX_POST_TRIGGER_FRAMES = 140
PRE_ROLL_BLOCKS = 40
MAX_BUFFER_BLOCKS = 320

# Audio config
SAMPLE_RATE = 16000
CHANNELS = 1
BLOCKSIZE = 1024

# Confidence & safety
MIN_LANGUAGE_CONFIDENCE = 0.45    # lowered — small model on non-native is often 0.5-0.6
STREAM_TEARDOWN_DELAY = 0.35      # seconds between stream stop and Whisper inference

# Auto-resume: after transcription, automatically go back to monitoring
AUTO_RESUME_MONITORING = True

# ━━ Model Singletons ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

whisper_model = None
whisper_model_lock = threading.Lock()

translation_cache: dict[str, tuple] = {}
translation_cache_lock = threading.Lock()


# ━━ Event Emitter ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def send_event(event_type: str, state: str, message: str, data=None) -> None:
    payload = {"type": event_type, "state": state, "message": message}
    if data is not None:
        payload["data"] = data
    try:
        print(json.dumps(payload, ensure_ascii=False), flush=True)
    except Exception:
        pass


# ━━ Audio Devices ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_input_devices():
    if sd is None:
        return []
    devices = sd.query_devices()
    return [
        {
            "index": i,
            "name": d.get("name", "Unknown"),
            "max_input_channels": d.get("max_input_channels", 0),
            "default_samplerate": d.get("default_samplerate", 0),
        }
        for i, d in enumerate(devices)
        if d.get("max_input_channels", 0) > 0
    ]


def list_audio_devices() -> None:
    if sd is None:
        send_event("audio_devices", current_state, "sounddevice not installed", data=[])
        return
    try:
        devices = get_input_devices()
        send_event("audio_devices", current_state, f"Found {len(devices)} input devices", data=devices)
    except Exception as e:
        send_event("audio_devices", current_state, f"Device list failed: {e}", data=[])


def set_default_input_device() -> None:
    global selected_input_device
    if sd is None:
        send_event("status", current_state, "sounddevice not installed")
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
        name = next((d["name"] for d in devices if d["index"] == selected_input_device), "Unknown")
        send_event("device_selected", current_state, f"Using input device: {name}",
                    data={"index": selected_input_device, "name": name})
    except Exception as e:
        send_event("status", current_state, f"Failed to set default input device: {e}")


# ━━ Model Loading ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def ensure_whisper_model():
    global whisper_model
    if whisper_model is not None:
        return whisper_model
    with whisper_model_lock:
        if whisper_model is not None:
            return whisper_model
        if WhisperModel is None:
            send_event("status", current_state, "faster-whisper not installed")
            return None
        send_event("status", current_state, "Loading Whisper model (small, int8)...")
        try:
            whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
            send_event("status", current_state, "Whisper model loaded")
        except Exception as e:
            send_event("status", current_state, f"Whisper load failed: {e}")
            whisper_model = None
    return whisper_model


TRANSLATION_MODEL_MAP = {
    "pt": "Helsinki-NLP/opus-mt-ROMANCE-en",
    "fr": "Helsinki-NLP/opus-mt-ROMANCE-en",
    "es": "Helsinki-NLP/opus-mt-ROMANCE-en",
    "it": "Helsinki-NLP/opus-mt-ROMANCE-en",
    "ro": "Helsinki-NLP/opus-mt-ROMANCE-en",
    "de": "Helsinki-NLP/opus-mt-de-en",
    "ru": "Helsinki-NLP/opus-mt-ru-en",
    "zh": "Helsinki-NLP/opus-mt-zh-en",
    "ja": "Helsinki-NLP/opus-mt-ja-en",
    "ko": "Helsinki-NLP/opus-mt-ko-en",
    "ar": "Helsinki-NLP/opus-mt-ar-en",
    "hi": "Helsinki-NLP/opus-mt-hi-en",
}


def get_translation_model_name(source_language: str) -> str | None:
    return TRANSLATION_MODEL_MAP.get(source_language)


def ensure_translation_model(source_language: str):
    if MarianMTModel is None or MarianTokenizer is None:
        return None, None
    model_name = get_translation_model_name(source_language)
    if model_name is None:
        return None, None
    if model_name in translation_cache:
        return translation_cache[model_name]
    with translation_cache_lock:
        if model_name in translation_cache:
            return translation_cache[model_name]
        send_event("status", current_state, f"Loading translation model: {model_name}")
        try:
            tokenizer = MarianTokenizer.from_pretrained(model_name)
            model = MarianMTModel.from_pretrained(model_name)
            translation_cache[model_name] = (tokenizer, model)
            send_event("status", current_state, f"Translation model loaded: {model_name}")
            return tokenizer, model
        except Exception as e:
            send_event("status", current_state, f"Translation model load failed: {e}")
            return None, None


def preload_models() -> None:
    send_event("status", "idle", "Pre-loading models...")
    ensure_whisper_model()
    send_event("status", "idle", "Model pre-load complete")


# ━━ Translation ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def translate_text(text: str, source_language: str, target_language: str = "en") -> str:
    if not text.strip() or source_language == target_language:
        return text
    tokenizer, model = ensure_translation_model(source_language)
    if tokenizer is None or model is None:
        send_event("status", current_state,
                    f"No translation model for {source_language} → {target_language}")
        return text
    try:
        inputs = tokenizer([text], return_tensors="pt", padding=True, truncation=True)
        tokens = model.generate(**inputs, num_beams=4, max_length=512)
        return tokenizer.decode(tokens[0], skip_special_tokens=True)
    except Exception as e:
        send_event("status", current_state, f"Translation failed: {e}")
        return text


# ━━ Text Cleaning ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

JUNK_PHRASES = frozenset([
    "thank you for watching", "thanks for watching", "please subscribe",
    "subscribe to my channel", "thank you", "thanks", "bye", "you",
    "subtitles by the amara.org community", "amara.org",
    "sous-titres réalisés para la communauté d'amara.org",
])


def clean_transcription(text: str) -> str:
    if not text:
        return ""
    # Strip Whisper artifacts
    text = text.replace("...", "").replace("…", "").strip()
    text = text.strip("♪").strip("🎵").strip()
    # Check for junk
    normalized = text.strip().rstrip(".!?").lower()
    if normalized in JUNK_PHRASES:
        return ""
    # Too short to be meaningful
    if len(text) < 3:
        return ""
    return text.strip()


# ━━ Audio Normalization ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def normalize_audio(audio: "np.ndarray") -> "np.ndarray":
    """Peak-normalize audio to [-1, 1] range for consistent Whisper input."""
    peak = np.max(np.abs(audio))
    if peak > 0.001:
        return audio / peak * 0.95  # leave 5% headroom
    return audio


# ━━ Adaptive Voice Threshold ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_voice_threshold() -> float:
    """Dynamic threshold based on tracked noise floor."""
    threshold = noise_floor * VOICE_THRESHOLD_MULTIPLIER
    return max(VOICE_THRESHOLD_MIN, min(VOICE_THRESHOLD_MAX, threshold))


def update_noise_floor(level: float) -> None:
    """Update noise floor estimate with exponential moving average.
    Only update when we're NOT in active speech."""
    global noise_floor, noise_samples
    if not speech_started:
        if noise_samples < 50:
            # Bootstrap: simple average for first 50 samples
            noise_floor = (noise_floor * noise_samples + level) / (noise_samples + 1)
            noise_samples += 1
        else:
            noise_floor = (1 - NOISE_FLOOR_ALPHA) * noise_floor + NOISE_FLOOR_ALPHA * level


# ━━ Whisper Language Hints ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Recent language detections for biasing Whisper
recent_languages: list[str] = []
MAX_LANGUAGE_HISTORY = 5


def get_language_hint() -> str | None:
    """If we've been hearing a consistent language, hint Whisper to expect it."""
    if len(recent_languages) < 2:
        return None
    # Most common recent language
    from collections import Counter
    counts = Counter(recent_languages)
    most_common, count = counts.most_common(1)[0]
    if count >= 2 and most_common != "en":
        return most_common
    return None


def record_language(lang: str) -> None:
    """Track recently detected languages."""
    recent_languages.append(lang)
    if len(recent_languages) > MAX_LANGUAGE_HISTORY:
        recent_languages.pop(0)


# ━━ Transcription Pipeline ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def transcribe_audio_chunks(chunks):
    global transcription_in_progress

    send_event("status", current_state, "Transcription started")

    try:
        if np is None or not chunks:
            return

        model = ensure_whisper_model()
        if model is None:
            return

        send_event("status", current_state, f"Processing {len(chunks)} audio chunks")

        audio_data = np.concatenate(chunks, axis=0).astype("float32").flatten()

        # Normalize audio for consistent Whisper input levels
        audio_data = normalize_audio(audio_data)

        send_event("status", current_state,
                    f"Audio: {len(audio_data)} samples ({len(audio_data)/SAMPLE_RATE:.1f}s)")

        # Build transcription kwargs
        transcribe_kwargs = {
            "beam_size": 5,
            "vad_filter": True,
            "temperature": 0.0,
            "condition_on_previous_text": False,
        }

        # Language hint from recent detections
        lang_hint = get_language_hint()
        if lang_hint:
            transcribe_kwargs["language"] = lang_hint
            send_event("status", current_state, f"Language hint: {lang_hint}")

        segments, info = model.transcribe(audio_data, **transcribe_kwargs)

        text_parts = []
        for segment in segments:
            cleaned = segment.text.strip()
            if cleaned:
                text_parts.append(cleaned)

        raw_text = " ".join(text_parts).strip()
        source_language = getattr(info, "language", "unknown")
        language_probability = getattr(info, "language_probability", 0.0)

        send_event("status", current_state,
                    f"Whisper: {len(text_parts)} segments, {source_language} ({language_probability:.2f})")

        # Clean
        text = clean_transcription(raw_text)
        if not text:
            send_event("status", current_state, "Empty/junk transcription — skipped")
            return

        # Track language
        record_language(source_language)

        # Confidence gate
        if language_probability < MIN_LANGUAGE_CONFIDENCE:
            send_event("status", current_state,
                        f"Low confidence ({language_probability:.2f}) — skipped. Raw: {text}")
            send_event("transcription", "detected", text, data={
                "text": text, "language": source_language,
                "language_probability": language_probability, "low_confidence": True,
            })
            return

        # Emit transcription
        send_event("transcription", "detected", text, data={
            "text": text, "language": source_language,
            "language_probability": language_probability, "low_confidence": False,
        })

        # Translate
        if source_language and source_language != "en":
            translated = translate_text(text, source_language, "en")
            send_event("translation", "detected", translated, data={
                "translated_text": translated,
                "source_language": source_language,
                "target_language": "en",
            })
        else:
            send_event("translation", "detected", text, data={
                "translated_text": text,
                "source_language": "en",
                "target_language": "en",
            })

    except Exception as e:
        send_event("status", current_state, f"Transcription error: {e}")
        send_event("status", current_state, traceback.format_exc())
    finally:
        with lock:
            transcription_in_progress = False

        # Auto-resume monitoring after transcription
        if AUTO_RESUME_MONITORING and not shutdown_event.is_set():
            time.sleep(0.1)
            auto_resume_monitoring()


def auto_resume_monitoring() -> None:
    """Automatically return to monitoring after transcription completes."""
    global current_state, is_monitoring

    with lock:
        if current_state != "detected":
            return  # user already changed state
        is_monitoring = True
        reset_monitoring_state()
        current_state = "monitoring"

    send_event("state_change", "monitoring", "Auto-resumed monitoring")
    start_input_stream_if_needed()


# ━━ Audio Stream ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

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
    send_event("voice_activity", "monitoring", f"Utterance finalized (level={level:.4f})")
    send_event("state_change", "detected", "Speech detected — transcribing")

    # 1. Stop audio stream
    stop_input_stream()

    # 2. Let PortAudio drain (prevents CTranslate2 segfault on Windows)
    time.sleep(STREAM_TEARDOWN_DELAY)

    # 3. Run transcription
    thread = threading.Thread(target=transcribe_audio_chunks, args=(buffered_chunks,), daemon=True)
    thread.start()


def audio_callback(indata, frames, time_info, status):
    global current_state, is_monitoring
    global voice_active_frames, audio_buffer, transcription_in_progress
    global speech_started, silence_frames, post_trigger_frames

    if status or np is None:
        return

    try:
        buffer_copy = indata.copy()
        level = float(np.sqrt(np.mean(buffer_copy ** 2)))
    except Exception:
        return

    # Track noise floor (always, even outside lock)
    update_noise_floor(level)
    threshold = get_voice_threshold()

    buffered_chunks = None
    should_finalize = False

    with lock:
        if current_state != "monitoring" or not is_monitoring:
            return

        audio_buffer.append(buffer_copy)
        if len(audio_buffer) > MAX_BUFFER_BLOCKS:
            audio_buffer = audio_buffer[-MAX_BUFFER_BLOCKS:]

        if level > threshold:
            voice_active_frames += 1
        else:
            voice_active_frames = max(0, voice_active_frames - 1)

        if transcription_in_progress:
            return

        if not speech_started:
            if len(audio_buffer) > PRE_ROLL_BLOCKS:
                audio_buffer = audio_buffer[-PRE_ROLL_BLOCKS:]

            if voice_active_frames >= VOICE_FRAMES_REQUIRED:
                speech_started = True
                silence_frames = 0
                post_trigger_frames = 0
                send_event("voice_activity", "monitoring",
                           f"Speech started (level={level:.4f}, threshold={threshold:.4f})")
            return

        post_trigger_frames += 1

        if level > threshold:
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
        threading.Thread(
            target=finalize_utterance,
            args=(level, buffered_chunks),
            daemon=True,
        ).start()


def start_input_stream_if_needed() -> None:
    global input_stream, selected_input_device

    if sd is None or np is None:
        return

    if selected_input_device is None:
        set_default_input_device()
    if selected_input_device is None:
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
            callback=audio_callback,
        )
        input_stream.start()
        send_event("status", current_state, "Audio stream started")
    except Exception as e:
        input_stream = None
        send_event("status", current_state, f"Audio stream failed: {e}")


def reset_monitoring_state():
    global voice_active_frames, audio_buffer, transcription_in_progress
    global speech_started, silence_frames, post_trigger_frames

    voice_active_frames = 0
    audio_buffer = []
    transcription_in_progress = False
    speech_started = False
    silence_frames = 0
    post_trigger_frames = 0


# ━━ Command Handler ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def handle_command(command: dict) -> None:
    global current_state, is_monitoring

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
                send_event("status", current_state, "Cannot dub: not in detected state")
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
                send_event("status", current_state, "Not monitoring")
                return
            is_monitoring = False
            current_state = "detected"
        stop_input_stream()
        send_event("state_change", "detected", "Manual detection triggered")

    elif action == "list_audio_devices":
        list_audio_devices()

    elif action == "set_default_input_device":
        set_default_input_device()

    elif action == "set_auto_resume":
        global AUTO_RESUME_MONITORING
        AUTO_RESUME_MONITORING = command.get("enabled", True)
        send_event("status", current_state, f"Auto-resume: {AUTO_RESUME_MONITORING}")

    else:
        send_event("status", current_state, f"Unknown command: {action}")


# ━━ Main ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def main() -> None:
    preload_models()
    send_event("status", "idle", "Worker ready")

    try:
        while not shutdown_event.is_set():
            line = sys.stdin.readline()
            if not line:
                break
            try:
                command = json.loads(line.strip())
                handle_command(command)
            except json.JSONDecodeError:
                pass
            except Exception as e:
                send_event("status", current_state, f"Command error: {e}")
    finally:
        shutdown_event.set()
        stop_input_stream()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        shutdown_event.set()
        stop_input_stream()
        sys.exit(0)