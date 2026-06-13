#!/usr/bin/env python3
"""Standalone per-frequency power measurer. Run as separate process."""
import sys
import struct
import math
import subprocess
import os

SAMPLE_RATE = 200_000
DURATION = 0.4
BYTE_COUNT = int(SAMPLE_RATE * DURATION * 2)  # s16le = 2 bytes/sample

# Default gain for scanning (low to avoid ADC saturation on strong stations)
DEFAULT_SCAN_GAIN = 15  # dB — much lower than the default 40 dB

def rms_db(raw: bytes) -> float:
    """Return RMS power in dBFS for signed 16-bit little-endian audio.
    
    rtl_fm outputs signed 16-bit PCM. Full-scale (0 dBFS) = 32768.
    So dBFS = 20 * log10(rms / 32768).
    
    This gives consistent, calibrated readings across all gain levels:
      -Gain 10: ~-22 dBFS (real signal, not saturated)
      -Gain 40: ~-11 dBFS (same signal, amplified but still in range)
    """
    if len(raw) < 4:
        return -96.0
    count = len(raw) // 2
    samples = struct.unpack(f"<{count}h", raw)
    power = sum(s * s for s in samples) / count
    rms = math.sqrt(power)
    if rms < 1.0:
        return -96.0
    return 20.0 * math.log10(rms / 32768.0)

def main():
    if len(sys.argv) != 3:
        print('-96.0')
        sys.exit(1)

    freq_hz = int(sys.argv[1])
    device = sys.argv[2]

    # Allow optional gain override via RTL_FM_GAIN env var
    scan_gain = os.environ.get('RTL_FM_GAIN', str(DEFAULT_SCAN_GAIN))

    cmd = [
        'rtl_fm', '-d', device,
        '-f', str(freq_hz),
        '-M', 'fm',
        '-s', str(SAMPLE_RATE),
        '-l', '0',
        '-g', scan_gain,
    ]

    try:
        # Standalone subprocess — no threading, no Flask
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid
        )
        raw = os.read(proc.stdout.fileno(), BYTE_COUNT)
        proc.kill()
        proc.wait()
        result = rms_db(raw)
    except Exception:
        result = -96.0

    print(f'{result:.1f}')

if __name__ == '__main__':
    main()
