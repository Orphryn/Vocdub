"""Test WASAPI loopback capture — run while playing YouTube audio."""
import pyaudiowpatch as pyaudio
import numpy as np
import time

p = pyaudio.PyAudio()
dev = p.get_default_wasapi_loopback()

print(f"Device: {dev['name']}")
print(f"Rate: {int(dev['defaultSampleRate'])}Hz, Channels: {dev['maxInputChannels']}")

stream = p.open(
    format=pyaudio.paFloat32,
    channels=dev["maxInputChannels"],
    rate=int(dev["defaultSampleRate"]),
    input=True,
    input_device_index=dev["index"],
    frames_per_buffer=4096,
)

print("")
print("Reading 3 seconds... PLAY SOMETHING LOUD ON YOUTUBE NOW!")
print("")
time.sleep(1)

total = []
for i in range(20):
    data = stream.read(4096, exception_on_overflow=False)
    arr = np.frombuffer(data, dtype=np.float32)
    level = np.sqrt(np.mean(arr ** 2))
    total.append(level)
    print(f"  Block {i:2d}: level={level:.6f}")

stream.stop_stream()
stream.close()
p.terminate()

avg = sum(total) / len(total)
print(f"\nAverage level: {avg:.6f}")

if avg < 0.0001:
    print("")
    print("RESULT: SILENCE — loopback is NOT capturing audio from this device")
    print("")
    print("FIX OPTIONS:")
    print("  1. Plug in headphones → VoxDub will capture from headphone loopback")
    print("  2. Plug speakers into PC 3.5mm jack → creates a Realtek loopback device")
    print("  3. Your NVIDIA HDMI audio driver does not support loopback capture")
else:
    print("")
    print(f"RESULT: AUDIO DETECTED! Loopback is working (avg level: {avg:.6f})")
    print("VoxDub threshold may need adjustment.")