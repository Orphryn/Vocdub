"""VoxDub Audio Worker v0.4.0 — Production Build

Architecture:
  MIC mode:   sounddevice InputStream → VAD → stop stream → Whisper → resume
  SYSTEM mode: WASAPI loopback (PyAudioWPatch) → VAD → Whisper (stream stays alive) → resume

Known issues addressed:
  1. Translation hallucination ("no, no, no..." ×50) → output length cap + repetition penalty
  2. Music/SFX false triggers → energy + duration gating, Silero VAD inside Whisper
  3. Single-word fragments ("¡Ah!") → minimum word count for system audio
  4. Segfault 0xC0000005 → main-thread model preload, PortAudio teardown delay
  5. Slow transcription → tiny model for system audio, beam_size=1, 8 CPU threads
  6. Stream dying during transcription → loopback reader stays alive in detected state
  7. Noise floor contamination → separate thresholds per source, reset on switch
  8. Auto-resume race condition → check pa_stream before restarting
"""

import io, json, os, sys, threading, time, traceback
from collections import Counter

os.environ.update({
    "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
    "HF_HUB_DISABLE_SYMLINKS_WARNING": "1", "TOKENIZERS_PARALLELISM": "false",
})

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "buffer"):
        wrapped = io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace", line_buffering=True)
        if stream is sys.stdout:
            sys.stdout = wrapped
        else:
            sys.stderr = wrapped

try: import numpy as np
except ImportError: np = None
try: import sounddevice as sd
except ImportError: sd = None
try: import pyaudiowpatch as pyaudio
except ImportError: pyaudio = None
try: from faster_whisper import WhisperModel
except ImportError: WhisperModel = None
try: from transformers import MarianMTModel, MarianTokenizer
except ImportError: MarianMTModel = MarianTokenizer = None


# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

SAMPLE_RATE = 16000
CHANNELS = 1
BLOCKSIZE = 1024
BLOCK_DURATION = BLOCKSIZE / SAMPLE_RATE  # 0.064s per block

# CPU threading — adjust to your core count
CPU_THREADS = 8

# ── Mic VAD ───────────────────────────────────────────────────────────────────
MIC_THRESHOLD_MULTIPLIER = 3.0
MIC_THRESHOLD_MIN = 0.015
MIC_THRESHOLD_MAX = 0.15
MIC_FRAMES_REQUIRED = 5           # 320ms of voice to trigger
MIC_SILENCE_FRAMES = 45           # 2.9s silence to finalize
MIC_MAX_CAPTURE_FRAMES = 140      # 9.0s max capture
MIC_PRE_ROLL = 40
MIC_MAX_BUFFER = 320
MIC_NOISE_ALPHA = 0.02

# ── System Audio VAD ─────────────────────────────────────────────────────────
# Tuned from actual NVIDIA HDMI loopback measurements:
#   Silence: 0.000 - 0.003
#   Quiet music/ambience: 0.003 - 0.020
#   Clear dialogue: 0.050 - 0.220
#
# Threshold at 0.025 catches all dialogue, skips silence and quiet ambience.
# 8 frames required (512ms) prevents brief music hits from triggering.
# 35 silence frames (2.2s) bridges gaps between words/phrases within a sentence.
SYS_THRESHOLD = 0.025
SYS_FRAMES_REQUIRED = 8
SYS_SILENCE_FRAMES = 35           # 2.2s silence to finalize
SYS_MAX_CAPTURE_FRAMES = 110      # 7.0s max capture — full TV lines
SYS_PRE_ROLL = 20
SYS_MAX_BUFFER = 160
SYS_MIN_SPEECH_SECONDS = 1.5      # skip captures shorter than this
SYS_MIN_WORDS = 2                 # skip single-word results

# ── Shared ────────────────────────────────────────────────────────────────────
MIN_LANGUAGE_CONFIDENCE = 0.40
STREAM_TEARDOWN_DELAY = 0.35
AUTO_RESUME = True

# ── Translation ───────────────────────────────────────────────────────────────
TRANSLATION_MODELS = {
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

JUNK_PHRASES = frozenset([
    "thank you for watching", "thanks for watching", "please subscribe",
    "subscribe to my channel", "thank you", "thanks", "bye", "you",
    "subtitles by the amara.org community", "amara.org",
    "ah", "oh", "uh", "um", "hmm", "hm", "eh", "mhm",
])


# ═══════════════════════════════════════════════════════════════════════════════
# STATE
# ═══════════════════════════════════════════════════════════════════════════════

state = "idle"
monitoring = False
audio_source = "mic"
selected_mic = None

# Streams
sd_stream = None
pa_inst = None
pa_stream = None
loopback_info = None
loopback_thread = None

# VAD
vad_voice_frames = 0
vad_buffer: list = []
vad_in_progress = False
vad_speech_on = False
vad_silence = 0
vad_trigger = 0
noise_floor = 0.005
noise_n = 0

# Language tracking
lang_history: list[str] = []

lock = threading.Lock()
shutdown = threading.Event()

# Models
_whisper_small = None
_whisper_tiny = None
_whisper_lock = threading.Lock()
_tl_cache: dict[str, tuple] = {}
_tl_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
# EVENTS
# ═══════════════════════════════════════════════════════════════════════════════

def emit(etype: str, msg: str, data=None):
    p = {"type": etype, "state": state, "message": msg}
    if data is not None:
        p["data"] = data
    try:
        print(json.dumps(p, ensure_ascii=False), flush=True)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# AUDIO UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

def normalize(a):
    pk = np.max(np.abs(a))
    return (a / pk * 0.95) if pk > 0.001 else a

def resample(a, src, dst):
    if src == dst: return a
    r = dst / src
    n = int(len(a) * r)
    ix = np.clip(np.arange(n) / r, 0, len(a) - 1)
    fl = np.floor(ix).astype(int)
    cl = np.minimum(fl + 1, len(a) - 1)
    fr = ix - fl
    return a[fl] * (1 - fr) + a[cl] * fr

def to_mono(a, ch):
    if ch <= 1: return a
    n = len(a) // ch
    return a[:n * ch].reshape(n, ch).mean(axis=1)


# ═══════════════════════════════════════════════════════════════════════════════
# VAD HELPERS (source-aware)
# ═══════════════════════════════════════════════════════════════════════════════

def _threshold():
    if audio_source == "system":
        return SYS_THRESHOLD
    t = noise_floor * MIC_THRESHOLD_MULTIPLIER
    return max(MIC_THRESHOLD_MIN, min(MIC_THRESHOLD_MAX, t))

def _frames_req():
    return SYS_FRAMES_REQUIRED if audio_source == "system" else MIC_FRAMES_REQUIRED

def _silence_req():
    return SYS_SILENCE_FRAMES if audio_source == "system" else MIC_SILENCE_FRAMES

def _max_trigger():
    return SYS_MAX_CAPTURE_FRAMES if audio_source == "system" else MIC_MAX_CAPTURE_FRAMES

def _pre_roll():
    return SYS_PRE_ROLL if audio_source == "system" else MIC_PRE_ROLL

def _max_buf():
    return SYS_MAX_BUFFER if audio_source == "system" else MIC_MAX_BUFFER

def _update_noise(level):
    global noise_floor, noise_n
    if audio_source != "mic" or vad_speech_on:
        return
    if noise_n < 50:
        noise_floor = (noise_floor * noise_n + level) / (noise_n + 1)
        noise_n += 1
    else:
        noise_floor = (1 - MIC_NOISE_ALPHA) * noise_floor + MIC_NOISE_ALPHA * level


# ═══════════════════════════════════════════════════════════════════════════════
# POST-PROCESSING PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════
#
# Sits between Whisper raw output and translation. Catches every known
# failure mode of Whisper and MarianMT:
#
#   1. Hallucination loops: "a ir a ir a ir a ir..." → blocked
#   2. Repetitive phrases: "No, no, no, no, no" → "No"
#   3. Junk/boilerplate: "Thank you for watching" → blocked
#   4. Music/SFX noise: "[Music]", "♪♪♪" → blocked
#   5. Single-word fragments in system mode → blocked
#   6. Encoding artifacts: fix common UTF-8 issues
#   7. Context dedup: skip if identical to last N transcriptions
#
# Each stage returns the cleaned text or "" to skip entirely.

import re

# ── Stage 0: Known junk phrases ──────────────────────────────────────────────

JUNK_PHRASES = frozenset([
    "thank you for watching", "thanks for watching", "please subscribe",
    "subscribe to my channel", "thank you", "thanks", "bye", "you",
    "subtitles by the amara.org community", "amara.org",
    "sous-titres par la communaute d'amara.org",
    "ah", "oh", "uh", "um", "hmm", "hm", "eh", "mhm", "shh",
    "music", "musica", "musique", "applause", "laughter", "risas",
])

# ── Stage 1: Hallucination / repetition detector ─────────────────────────────

def _detect_repetition(text: str) -> bool:
    """Detect if text contains excessive repetition (Whisper hallucination).
    
    Catches patterns like:
      - "a ir a ir a ir a ir a ir a ir"
      - "no, no, no, no, no, no, no"
      - "go go go go go go"
      - repeated 2-3 word phrases
    
    Returns True if text is hallucinated/repetitive.
    """
    words = text.lower().split()
    if len(words) < 4:
        return False
    
    # Check 1: Single word repeated many times
    # "no no no no no" or "go go go go"
    from collections import Counter
    counts = Counter(words)
    most_common_word, most_count = counts.most_common(1)[0]
    if most_count >= len(words) * 0.6 and len(words) >= 5:
        return True
    
    # Check 2: 2-gram repetition
    # "a ir a ir a ir" → bigram "a ir" appears 3+ times
    if len(words) >= 6:
        bigrams = [f"{words[i]} {words[i+1]}" for i in range(len(words)-1)]
        bi_counts = Counter(bigrams)
        top_bi, top_bi_n = bi_counts.most_common(1)[0]
        if top_bi_n >= 3 and top_bi_n >= len(bigrams) * 0.4:
            return True
    
    # Check 3: 3-gram repetition
    # "me voy a me voy a me voy a"
    if len(words) >= 9:
        trigrams = [f"{words[i]} {words[i+1]} {words[i+2]}" for i in range(len(words)-2)]
        tri_counts = Counter(trigrams)
        top_tri, top_tri_n = tri_counts.most_common(1)[0]
        if top_tri_n >= 3 and top_tri_n >= len(trigrams) * 0.35:
            return True
    
    # Check 4: Character-level repetition ratio
    # If any single character makes up >40% of the text (excluding spaces)
    chars = text.replace(" ", "").lower()
    if len(chars) > 10:
        char_counts = Counter(chars)
        top_char, top_char_n = char_counts.most_common(1)[0]
        if top_char_n / len(chars) > 0.4 and top_char not in "aeiouns":
            return True
    
    return False


def _collapse_repetition(text: str) -> str:
    """If text has mild repetition, collapse it rather than blocking entirely.
    
    "No, no, no, no" → "No"
    "Sí, sí, sí" → "Sí"
    But keeps natural repetition like "poco a poco" (little by little).
    """
    words = text.split()
    if len(words) < 3:
        return text
    
    # Check if it's a simple repeated word/phrase with punctuation
    # "No, no, no, no" → strip punctuation, check if all same
    stripped = [re.sub(r'[,;.!?¡¿\s]+', '', w).lower() for w in words]
    stripped = [w for w in stripped if w]
    
    if not stripped:
        return text
    
    counts = Counter(stripped)
    most, n = counts.most_common(1)[0]
    
    # If one word is >70% of all words, collapse to just that word
    if n >= len(stripped) * 0.7 and n >= 3:
        # Find the original cased version
        for w in words:
            if re.sub(r'[,;.!?¡¿\s]+', '', w).lower() == most:
                return w.rstrip(",;.!? ")
    
    return text


# ── Stage 2: Music / sound effect detection ──────────────────────────────────

MUSIC_PATTERNS = re.compile(
    r'^\s*[\[(\{]?\s*(music|musica|musique|musik|applause|laughter|risas|'
    r'singing|cantando|chanting|humming|instrumental|silence|inaudible|'
    r'foreign language|speaks? \w+)\s*[\])\}]?\s*$',
    re.IGNORECASE
)

MUSIC_CHARS = set("♪♫🎵🎶🎵🎤")


def _is_music_or_sfx(text: str) -> bool:
    """Detect music markers, sound effects, and non-speech audio descriptions."""
    if any(c in MUSIC_CHARS for c in text):
        return True
    if MUSIC_PATTERNS.match(text.strip()):
        return True
    # Pure punctuation or symbols
    cleaned = re.sub(r'[\s\W]+', '', text)
    if len(cleaned) < 2:
        return True
    return False


# ── Stage 3: Context deduplication ───────────────────────────────────────────

_recent_outputs: list[str] = []
MAX_RECENT = 5


def _is_duplicate(text: str) -> bool:
    """Skip if we just emitted the same or very similar text."""
    normalized = text.lower().strip().rstrip(".!?")
    for prev in _recent_outputs:
        if normalized == prev:
            return True
        # Fuzzy: if >80% of words overlap, it's a duplicate
        words_new = set(normalized.split())
        words_old = set(prev.split())
        if words_new and words_old:
            overlap = len(words_new & words_old) / max(len(words_new), len(words_old))
            if overlap > 0.8:
                return True
    return False


def _record_output(text: str):
    normalized = text.lower().strip().rstrip(".!?")
    _recent_outputs.append(normalized)
    if len(_recent_outputs) > MAX_RECENT:
        _recent_outputs.pop(0)


# ── Master post-processing function ──────────────────────────────────────────

def postprocess(raw_text: str) -> str:
    """Full post-processing pipeline. Returns cleaned text or "" to skip.
    
    Pipeline stages:
      0. Basic cleanup (whitespace, encoding, music chars)
      1. Junk phrase filter
      2. Music / SFX detection
      3. Hallucination / repetition detection
      4. Repetition collapsing (mild cases)
      5. Minimum length / word count gate
      6. Context deduplication
    """
    if not raw_text:
        return ""
    
    # Stage 0: Basic cleanup
    text = raw_text.strip()
    text = text.replace("...", "").replace("…", "")
    text = text.strip("♪🎵♫🎶🎤")
    text = re.sub(r'\s+', ' ', text).strip()
    
    if len(text) < 2:
        return ""
    
    # Stage 1: Known junk phrases
    normalized = text.rstrip(".!?¡¿,;: ").lower()
    if normalized in JUNK_PHRASES:
        return ""
    
    # Stage 2: Music / SFX
    if _is_music_or_sfx(text):
        return ""
    
    # Stage 3: Hallucination detection (severe repetition → block entirely)
    if _detect_repetition(text):
        emit("status", f"Hallucination blocked: {text[:80]}...")
        return ""
    
    # Stage 4: Mild repetition collapsing
    text = _collapse_repetition(text)
    
    # Stage 5: Minimum content gate
    if len(text) < 3:
        return ""
    if audio_source == "system":
        meaningful_words = [w for w in text.split() if len(w) > 1]
        if len(meaningful_words) < SYS_MIN_WORDS:
            return ""
    
    # Stage 6: Context deduplication
    if _is_duplicate(text):
        emit("status", f"Duplicate skipped: {text[:60]}")
        return ""
    
    return text.strip()


# ═══════════════════════════════════════════════════════════════════════════════
# LANGUAGE HINTS
# ═══════════════════════════════════════════════════════════════════════════════

def lang_hint():
    if len(lang_history) < 2:
        return None
    best, n = Counter(lang_history).most_common(1)[0]
    return best if n >= 2 and best != "en" else None

def record_lang(lang):
    lang_history.append(lang)
    if len(lang_history) > 5:
        lang_history.pop(0)


# ═══════════════════════════════════════════════════════════════════════════════
# DEVICES
# ═══════════════════════════════════════════════════════════════════════════════

def _mic_devices():
    if sd is None: return []
    return [{"index": i, "name": d.get("name", "?"), "channels": d.get("max_input_channels", 0)}
            for i, d in enumerate(sd.query_devices()) if d.get("max_input_channels", 0) > 0]

def set_mic():
    global selected_mic
    if sd is None: return
    try:
        di = sd.default.device[0]
        devs = _mic_devices()
        if di is None or di == -1:
            if not devs: return
            di = devs[0]["index"]
        selected_mic = int(di)
        name = next((d["name"] for d in devs if d["index"] == selected_mic), "?")
        emit("device_selected", f"Mic: {name}", {"name": name, "source": "mic"})
    except Exception as e:
        emit("status", f"Mic error: {e}")

def set_loopback():
    global loopback_info
    if pyaudio is None:
        emit("status", "PyAudioWPatch not installed — pip install PyAudioWPatch")
        return
    try:
        p = pyaudio.PyAudio()
        loopback_info = p.get_default_wasapi_loopback()
        p.terminate()
        name = loopback_info.get("name", "?")
        rate = int(loopback_info.get("defaultSampleRate", 0))
        ch = loopback_info.get("maxInputChannels", 0)
        emit("device_selected", f"Loopback: {name} ({rate}Hz {ch}ch)",
             {"name": name, "sampleRate": rate, "channels": ch, "source": "system"})
    except Exception as e:
        loopback_info = None
        emit("status", f"No loopback: {e}")

def list_devices():
    mic = _mic_devices()
    lb = []
    if pyaudio:
        try:
            p = pyaudio.PyAudio()
            for d in p.get_loopback_device_info_generator():
                lb.append({"index": d["index"], "name": d["name"],
                           "channels": d["maxInputChannels"], "rate": int(d["defaultSampleRate"])})
            p.terminate()
        except Exception: pass
    emit("audio_devices", f"{len(mic)} mic + {len(lb)} loopback", {"mic": mic, "loopback": lb})


# ═══════════════════════════════════════════════════════════════════════════════
# MODELS
# ═══════════════════════════════════════════════════════════════════════════════

def _load_whisper_small():
    global _whisper_small
    if _whisper_small: return _whisper_small
    with _whisper_lock:
        if _whisper_small: return _whisper_small
        if not WhisperModel: return None
        emit("status", "Loading Whisper small...")
        try:
            _whisper_small = WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=CPU_THREADS)
            emit("status", "Whisper small loaded")
        except Exception as e:
            emit("status", f"Whisper small failed: {e}")
    return _whisper_small

def _load_whisper_base():
    """Whisper base — used for system audio. 80% accuracy, ~1s/chunk on CPU.
    Clean digital audio from loopback doesn't need the full 'small' model,
    but 'base' is significantly more accurate than 'tiny' for multi-language."""
    global _whisper_tiny
    if _whisper_tiny: return _whisper_tiny
    with _whisper_lock:
        if _whisper_tiny: return _whisper_tiny
        if not WhisperModel: return None
        emit("status", "Loading Whisper base...")
        try:
            _whisper_tiny = WhisperModel("base", device="cpu", compute_type="int8", cpu_threads=CPU_THREADS)
            emit("status", "Whisper base loaded")
        except Exception as e:
            emit("status", f"Whisper base failed: {e}")
    return _whisper_tiny

def whisper():
    return _load_whisper_base() if audio_source == "system" else _load_whisper_small()

def _load_translator(lang):
    if not MarianMTModel or not MarianTokenizer: return None, None
    name = TRANSLATION_MODELS.get(lang)
    if not name: return None, None
    if name in _tl_cache: return _tl_cache[name]
    with _tl_lock:
        if name in _tl_cache: return _tl_cache[name]
        emit("status", f"Loading translator: {name}")
        try:
            tok = MarianTokenizer.from_pretrained(name)
            mdl = MarianMTModel.from_pretrained(name)
            _tl_cache[name] = (tok, mdl)
            emit("status", f"Translator loaded")
            return tok, mdl
        except Exception as e:
            emit("status", f"Translator failed: {e}")
            return None, None

def preload():
    emit("status", "Pre-loading models...")
    _load_whisper_small()
    _load_whisper_base()
    emit("status", "Models ready")

def translate(text, src, tgt="en"):
    if not text.strip() or src == tgt: return text
    tok, mdl = _load_translator(src)
    if not tok: return text
    try:
        inp = tok([text], return_tensors="pt", padding=True, truncation=True)
        in_len = inp["input_ids"].shape[1]
        out = mdl.generate(
            **inp,
            num_beams=4,
            max_length=min(max(in_len * 3, 20), 200),
            repetition_penalty=2.0,
            no_repeat_ngram_size=3,
        )
        result = tok.decode(out[0], skip_special_tokens=True)
        # Truncate runaway output
        if len(result) > len(text) * 4:
            result = result[:len(text) * 3].rstrip() + "..."
        return result
    except Exception:
        return text


# ═══════════════════════════════════════════════════════════════════════════════
# TRANSCRIPTION
# ═══════════════════════════════════════════════════════════════════════════════

def transcribe(chunks):
    global vad_in_progress

    try:
        if not np or not chunks: return
        mdl = whisper()
        if not mdl: return

        audio = np.concatenate(chunks, axis=0).astype("float32").flatten()
        audio = normalize(audio)
        dur = len(audio) / SAMPLE_RATE

        # Gate: too short for meaningful speech
        if audio_source == "system" and dur < SYS_MIN_SPEECH_SECONDS:
            emit("status", f"Too short ({dur:.1f}s) — skipped")
            return

        emit("status", f"Transcribing {dur:.1f}s...")

        kw = {
            "beam_size": 1 if audio_source == "system" else 5,
            "vad_filter": True,
            "temperature": 0.0,
            "condition_on_previous_text": False,
            "vad_parameters": {
                "min_speech_duration_ms": 400,
                "min_silence_duration_ms": 250,
                "speech_pad_ms": 150,
                "threshold": 0.3,
            },
        }

        hint = lang_hint()
        if hint:
            kw["language"] = hint

        segs, info = mdl.transcribe(audio, **kw)
        parts = [s.text.strip() for s in segs if s.text.strip()]
        raw = " ".join(parts).strip()
        lang = getattr(info, "language", "?")
        prob = getattr(info, "language_probability", 0.0)

        text = postprocess(raw)
        if not text:
            emit("status", "Filtered by post-processor — skipped")
            return

        record_lang(lang)

        if prob < MIN_LANGUAGE_CONFIDENCE:
            emit("status", f"Low confidence ({prob:.2f}) — skipped")
            emit("transcription", text, {"text": text, "language": lang,
                 "language_probability": prob, "low_confidence": True})
            return

        # Record for deduplication before emitting
        _record_output(text)

        emit("transcription", text, {"text": text, "language": lang,
             "language_probability": prob, "low_confidence": False})

        if lang and lang != "en":
            tl = translate(text, lang)
            # Post-process the translation too — MarianMT can hallucinate
            if _detect_repetition(tl):
                tl = _collapse_repetition(tl)
                if _detect_repetition(tl):
                    tl = text  # fall back to original if translation is gibberish
                    emit("status", "Translation hallucination — showing original")
            emit("translation", tl, {"translated_text": tl,
                 "source_language": lang, "target_language": "en"})
        else:
            emit("translation", text, {"translated_text": text,
                 "source_language": "en", "target_language": "en"})

    except Exception as e:
        emit("status", f"Transcription error: {e}")
        emit("status", traceback.format_exc())
    finally:
        with lock:
            vad_in_progress = False
        if AUTO_RESUME and not shutdown.is_set():
            time.sleep(0.05)
            _auto_resume()


def _auto_resume():
    global state, monitoring
    with lock:
        if state != "detected": return
        monitoring = True
        _reset_vad()
        state = "monitoring"
    emit("state_change", "Auto-resumed")
    if audio_source == "mic":
        _start_mic()
    elif pa_stream is None:
        _start_system()


# ═══════════════════════════════════════════════════════════════════════════════
# VAD PIPELINE
# ═══════════════════════════════════════════════════════════════════════════════

def _process_block(block):
    """Core VAD. Called for every 64ms audio block from either source."""
    global state, monitoring
    global vad_voice_frames, vad_buffer, vad_in_progress
    global vad_speech_on, vad_silence, vad_trigger

    if not np: return

    try:
        level = float(np.sqrt(np.mean(block ** 2)))
    except Exception:
        return

    _update_noise(level)
    thresh = _threshold()

    chunks = None
    finalize = False

    with lock:
        if state != "monitoring" or not monitoring:
            return

        vad_buffer.append(block.copy())
        if len(vad_buffer) > _max_buf():
            vad_buffer = vad_buffer[-_max_buf():]

        if level > thresh:
            vad_voice_frames += 1
        else:
            vad_voice_frames = max(0, vad_voice_frames - 1)

        if vad_in_progress:
            return

        if not vad_speech_on:
            if len(vad_buffer) > _pre_roll():
                vad_buffer = vad_buffer[-_pre_roll():]
            if vad_voice_frames >= _frames_req():
                vad_speech_on = True
                vad_silence = 0
                vad_trigger = 0
                emit("voice_activity", f"Speech (level={level:.4f} thresh={thresh:.4f})")
            return

        vad_trigger += 1
        if level > thresh:
            vad_silence = 0
        else:
            vad_silence += 1

        if vad_silence >= _silence_req() or vad_trigger >= _max_trigger():
            vad_in_progress = True
            state = "detected"
            monitoring = False
            chunks = list(vad_buffer)
            vad_buffer = []
            finalize = True

    if finalize and chunks:
        threading.Thread(target=_finalize, args=(level, chunks), daemon=True).start()


def _finalize(level, chunks):
    emit("voice_activity", f"Finalized (level={level:.4f})")
    emit("state_change", "Transcribing...")

    if audio_source != "system":
        _stop_all()
        time.sleep(STREAM_TEARDOWN_DELAY)

    transcribe(chunks)


def _reset_vad():
    global vad_voice_frames, vad_buffer, vad_in_progress
    global vad_speech_on, vad_silence, vad_trigger
    vad_voice_frames = 0
    vad_buffer = []
    vad_in_progress = False
    vad_speech_on = False
    vad_silence = 0
    vad_trigger = 0


# ═══════════════════════════════════════════════════════════════════════════════
# MIC STREAM
# ═══════════════════════════════════════════════════════════════════════════════

def _mic_cb(indata, frames, ti, status):
    if status or not np: return
    try: _process_block(indata.copy().flatten())
    except Exception: pass

def _start_mic():
    global sd_stream
    if not sd or not np: return
    if selected_mic is None: set_mic()
    if selected_mic is None: return
    if sd_stream: return
    try:
        sd_stream = sd.InputStream(device=selected_mic, channels=CHANNELS,
                                    samplerate=SAMPLE_RATE, blocksize=BLOCKSIZE,
                                    dtype="float32", callback=_mic_cb)
        sd_stream.start()
        emit("status", "Mic started")
    except Exception as e:
        sd_stream = None
        emit("status", f"Mic failed: {e}")

def _stop_mic():
    global sd_stream
    if sd_stream:
        try: sd_stream.stop(); sd_stream.close()
        except Exception: pass
        sd_stream = None


# ═══════════════════════════════════════════════════════════════════════════════
# SYSTEM AUDIO (WASAPI LOOPBACK)
# ═══════════════════════════════════════════════════════════════════════════════

def _loopback_loop():
    """Continuously reads from WASAPI loopback. Stays alive during transcription.
    
    CRITICAL: This thread must NOT exit except when state is idle.
    It retries on read errors instead of breaking.
    """
    global pa_stream
    if not pa_stream or not loopback_info or not np:
        emit("status", "Loopback reader: missing prerequisites")
        return

    rate = int(loopback_info.get("defaultSampleRate", 48000))
    ch = int(loopback_info.get("maxInputChannels", 2))
    chunk = int(rate * 0.064)
    errors = 0

    emit("status", f"Loopback: {rate}Hz {ch}ch {chunk}frames/read")

    while not shutdown.is_set():
        # Only exit on explicit idle — stay alive for monitoring AND detected
        with lock:
            if state == "idle" and not monitoring:
                break
            stream_ok = pa_stream is not None

        if not stream_ok:
            emit("status", "Loopback: stream closed, exiting")
            break

        try:
            raw = pa_stream.read(chunk, exception_on_overflow=False)
            a = np.frombuffer(raw, dtype=np.float32)
            mono = to_mono(a, ch)
            r = resample(mono, rate, SAMPLE_RATE).astype(np.float32)
            _process_block(r)
            errors = 0  # reset error count on success
        except Exception as e:
            errors += 1
            if errors <= 3:
                # Transient error — wait briefly and retry
                time.sleep(0.1)
                continue
            if not shutdown.is_set():
                emit("status", f"Loopback failed after {errors} errors: {e}")
            break

    emit("status", "Loopback reader exited")


def _start_system():
    global pa_inst, pa_stream, loopback_thread, loopback_info
    if not pyaudio:
        emit("status", "PyAudioWPatch not installed")
        return
    if not np:
        emit("status", "numpy not installed")
        return

    # If stream already active, don't restart
    if pa_stream is not None:
        emit("status", "System stream already active")
        return
    # If loopback thread is still alive, don't start another
    if loopback_thread is not None and loopback_thread.is_alive():
        emit("status", "Loopback thread still running")
        return

    # ALWAYS re-detect the loopback device.
    # This handles headphone plug/unplug: Windows changes the default
    # output device, so the loopback target changes too.
    # Without this, unplugging headphones would leave VoxDub capturing
    # from the old (now inactive) headphone device.
    set_loopback()
    if not loopback_info:
        emit("status", "No loopback device available")
        return

    try:
        rate = int(loopback_info.get("defaultSampleRate", 48000))
        ch = int(loopback_info.get("maxInputChannels", 2))
        pa_inst = pyaudio.PyAudio()
        pa_stream = pa_inst.open(format=pyaudio.paFloat32, channels=ch, rate=rate,
                                  input=True, input_device_index=loopback_info["index"],
                                  frames_per_buffer=int(rate * 0.064))
        emit("status", f"System audio started ({rate}Hz {ch}ch)")
        loopback_thread = threading.Thread(target=_loopback_loop, daemon=True)
        loopback_thread.start()
    except Exception as e:
        pa_stream = None
        if pa_inst:
            try: pa_inst.terminate()
            except Exception: pass
            pa_inst = None
        emit("status", f"System audio failed: {e}")


def _stop_system():
    global pa_stream, pa_inst, loopback_thread
    if pa_stream:
        try: pa_stream.stop_stream(); pa_stream.close()
        except Exception: pass
        pa_stream = None
    if pa_inst:
        try: pa_inst.terminate()
        except Exception: pass
        pa_inst = None
    loopback_thread = None


# ═══════════════════════════════════════════════════════════════════════════════
# STREAM CONTROL
# ═══════════════════════════════════════════════════════════════════════════════

def _start_source():
    if audio_source == "mic": _start_mic()
    else: _start_system()

def _stop_all():
    _stop_mic()
    _stop_system()


# ═══════════════════════════════════════════════════════════════════════════════
# COMMANDS
# ═══════════════════════════════════════════════════════════════════════════════

def handle(cmd):
    global state, monitoring, audio_source, AUTO_RESUME, noise_floor, noise_n

    action = cmd.get("action")

    if action == "start_monitoring":
        with lock:
            if state == "monitoring":
                emit("status", "Already monitoring")
                return
            monitoring = True
            _reset_vad()
            state = "monitoring"
        _start_source()
        emit("state_change", f"Monitoring ({audio_source})")

    elif action == "start_dubbing":
        with lock:
            if state != "detected": return
            state = "dubbing"
        emit("state_change", "Dubbing started")

    elif action == "stop":
        with lock:
            monitoring = False
            _reset_vad()
            state = "idle"
        _stop_all()
        emit("state_change", "Stopped")

    elif action == "simulate_detection":
        with lock:
            if state != "monitoring": return
            monitoring = False
            state = "detected"
        _stop_all()
        emit("state_change", "Manual detection")

    elif action == "set_audio_source":
        src = cmd.get("source", "mic")
        if src not in ("mic", "system"): return

        was_mon = False
        with lock:
            if state == "monitoring":
                was_mon = True
                monitoring = False
                _reset_vad()
        if was_mon: _stop_all()

        audio_source = src
        noise_floor = 0.005
        noise_n = 0
        emit("status", f"Source: {audio_source}")

        if src == "mic": set_mic()
        else: set_loopback()

        if was_mon:
            with lock:
                monitoring = True
                _reset_vad()
                state = "monitoring"
            _start_source()
            emit("state_change", f"Monitoring ({audio_source})")

    elif action == "list_audio_devices": list_devices()
    elif action == "set_default_input_device": set_mic()
    elif action == "set_default_loopback_device": set_loopback()
    elif action == "set_auto_resume":
        AUTO_RESUME = cmd.get("enabled", True)
        emit("status", f"Auto-resume: {AUTO_RESUME}")
    else:
        emit("status", f"Unknown command: {action}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    preload()
    emit("status", "Worker ready")
    try:
        while not shutdown.is_set():
            line = sys.stdin.readline()
            if not line: break
            try: handle(json.loads(line.strip()))
            except json.JSONDecodeError: pass
            except Exception as e: emit("status", f"Error: {e}")
    finally:
        shutdown.set()
        _stop_all()

if __name__ == "__main__":
    try: main()
    except KeyboardInterrupt:
        shutdown.set()
        _stop_all()
        sys.exit(0)