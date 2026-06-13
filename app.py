from flask import Flask, render_template, request, jsonify, Response, send_from_directory
import subprocess
import os
import signal
import time
import threading
import json
import struct
import math

app = Flask(__name__)

# Load config
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')
with open(CONFIG_PATH) as f:
    CONFIG = json.load(f)

# Process state
rtl_proc = None
sox_proc = None
current_freq = CONFIG['defaults']['frequency']
current_gain = CONFIG['defaults']['gain']
squelch_level = CONFIG['defaults']['squelch']
stereo_mode = False
demod_mode = CONFIG['defaults']['demod_mode']
input_level = CONFIG['defaults']['input_level']
use_sox_deemph = CONFIG['defaults']['use_sox_deemph']
lowpass_freq = CONFIG['defaults']['lowpass_freq']
lowpass_enabled = CONFIG['defaults']['lowpass_enabled']

# Watchdog state
watchdog_active = False
watchdog_thread = None

# Scan state
scan_active = False
scan_thread = None
scan_results = {}
scan_lock = threading.Lock()

# Config for FM band scan
FM_LOW = 87_500_000
FM_HIGH = 108_000_000
STEP = 20_000  # kept for scan_status total_steps reporting only

# ── Scanner constants ────────────────────────────────────────────────────
_SCAN_SAMPLE_RATE = 200_000
_SCAN_DURATION = 0.4
_SCAN_BYTE_COUNT = _SCAN_SAMPLE_RATE * _SCAN_DURATION * 2


def _rms_db(raw_bytes: bytes) -> float:
    if len(raw_bytes) < 4:
        return -96.0
    count = len(raw_bytes) // 2
    samples = struct.unpack(f"<{count}h", raw_bytes)
    power = sum(s * s for s in samples) / count
    rms = math.sqrt(power)
    if rms < 1.0:
        return -96.0
    return 20.0 * math.log10(rms / 32768.0)


def _measure_channel(freq_hz: int) -> float:
    """Tune rtl_fm to freq_hz, return RMS power in dBFS. -96 on error.

    Calls _measure.py as a pure subprocess (no threading, no Flask) to
    avoid the Popen stdout pipe issue that occurs when rtl_fm is forked
    from inside a Python thread.
    """
    try:
        result = __import__("subprocess").run(
            ["python3", "/home/markd/fm-tuner/_measure.py",
             str(freq_hz), CONFIG["device"]],
            capture_output=True, timeout=3.0
        )
        return float(result.stdout.strip())
    except Exception:
        return -96.0

def _snap_to_fm_channel(freq_hz: int) -> float:
    """Round frequency Hz to nearest FM broadcast channel, return as string.

    Returns a string to preserve precision -- e.g. 89.3 MHz serializes
    as "89.3" rather than the IEEE 754 artifact 89.299999... that occurs
    when a float is JSON-serialized and parsed by JavaScript's toFixed().
    """
    channel = round((freq_hz - 87_500_000) / 200_000)
    mhz = 87.5 + channel * 0.2
    return round(mhz, 1)


def kill_all():
    """Kill all rtl_fm and sox processes."""
    for cmd in ['rtl_fm', 'sox']:
        subprocess.run(['pkill', '-9', '-f', cmd], stderr=subprocess.DEVNULL)
    time.sleep(0.2)


def cleanup_procs():
    """Cleanly terminate rtl_fm and sox process group."""
    global rtl_proc, sox_proc
    for proc in (rtl_proc, sox_proc):
        if proc:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
    rtl_proc = None
    sox_proc = None


def stop_watchdog():
    """Stop the watchdog thread."""
    global watchdog_active
    watchdog_active = False
    if watchdog_thread:
        watchdog_thread.join(timeout=1)


def start_watchdog(process_ref, timeout=8):
    """Restart the stream if no data flows for `timeout` seconds."""
    global watchdog_active
    watchdog_active = True

    process_ref = [None]

    def watchdog_inner():
        while watchdog_active:
            time.sleep(2)
            if not watchdog_active:
                break
            proc = process_ref[0]
            if proc is None:
                break
            try:
                if proc.poll() is not None:
                    print("[WATCHDOG] Process died, stream ended")
                    break
            except (ProcessLookupError, OSError):
                break

    t = threading.Thread(target=watchdog_inner, daemon=True)
    t.start()
    return t


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/tune', methods=['POST'])
def tune():
    global rtl_proc, sox_proc, current_freq, current_gain, squelch_level
    global stereo_mode, demod_mode, input_level, use_sox_deemph, lowpass_freq, lowpass_enabled

    stop_watchdog()
    cleanup_procs()

    d = request.json
    current_freq = float(d.get('frequency', current_freq))
    current_gain = d.get('gain', current_gain)
    squelch_level = int(d.get('squelch', squelch_level))
    stereo_mode = bool(d.get('stereo', stereo_mode))
    demod_mode = d.get('demod_mode', demod_mode)
    input_level = float(d.get('input_level', input_level))
    use_sox_deemph = bool(d.get('use_sox_deemph', use_sox_deemph))
    lowpass_enabled = bool(d.get('lowpass_enabled', lowpass_enabled))
    lowpass_freq = int(d.get('lowpass_freq', lowpass_freq))

    return jsonify({'status': 'success', 'frequency': current_freq})


@app.route('/stream')
def stream():
    global rtl_proc, sox_proc, watchdog_thread, watchdog_active
    global current_freq, current_gain, squelch_level
    global stereo_mode, demod_mode, input_level, use_sox_deemph, lowpass_freq, lowpass_enabled

    stop_watchdog()
    cleanup_procs()

    fhz = int(current_freq * 1e6)
    gain_arg = "0" if current_gain == "auto" else str(current_gain)
    squelch_arg = f"-l {squelch_level}" if squelch_level > 0 else ""

    stereo = stereo_mode
    demod = demod_mode
    use_wbfm = stereo or demod in ('wide', 'wide_alt')

    if use_wbfm:
        rtl_args = [
            "rtl_fm", "-d", CONFIG['device'], "-f", str(fhz),
            "-M", "wbfm", "-s", "200000", "-r", "48000", "-E", "dc", "-g", gain_arg
        ]
        if demod == 'wide_alt':
            rtl_args.extend(["-F", "9"])
    else:
        rtl_args = [
            "rtl_fm", "-d", CONFIG['device'], "-f", str(fhz),
            "-M", "fm", "-s", "170000", "-r", "48000", "-E", "dc", "-g", gain_arg
        ]
    if squelch_arg:
        rtl_args.append(squelch_arg)

    sox_args = ["sox", f"-v {input_level}"]
    sox_args.extend(["-t", "raw", "-r", "48000", "-e", "signed", "-b", "16", "-c", "1"])
    sox_args.append("-")
    sox_args.extend(["-t", "wav", "-"])

    effective_lp_freq = lowpass_freq
    if lowpass_enabled:
        effective_lp_freq = min(lowpass_freq, 15000)
        sox_args.extend(["lowpass", str(effective_lp_freq)])

    if use_sox_deemph:
        sox_args.append("deemph")

    r_pipe, w_pipe = os.pipe()
    err_log = open(os.path.join(CONFIG['log_dir'], "rtl_err.log"), "w")
    sox_err_log = open(os.path.join(CONFIG['log_dir'], "sox_err.log"), "w")

    rtl_proc = subprocess.Popen(
        rtl_args,
        stdout=w_pipe,
        stderr=err_log,
        preexec_fn=os.setsid
    )
    os.close(w_pipe)

    sox_proc = subprocess.Popen(
        sox_args,
        stdin=r_pipe,
        stdout=subprocess.PIPE,
        stderr=sox_err_log,
        preexec_fn=os.setsid
    )
    os.close(r_pipe)

    proc_ref = [sox_proc]
    watchdog_active = True

    def watchdog_inner():
        while watchdog_active:
            time.sleep(3)
            if not watchdog_active:
                break
            proc = proc_ref[0]
            if proc is None:
                break
            try:
                if proc.poll() is not None:
                    print("[WATCHDOG] sox exited, ending stream")
                    break
            except (ProcessLookupError, OSError):
                break

    watchdog_thread = threading.Thread(target=watchdog_inner, daemon=True)
    watchdog_thread.start()

    def generate():
        try:
            while True:
                proc = proc_ref[0]
                if proc is None:
                    break
                try:
                    chunk = proc.stdout.read(8192)
                    if not chunk:
                        break
                    yield chunk
                except (OSError, ValueError):
                    break
        except GeneratorExit:
            pass
        finally:
            stop_watchdog()
            cleanup_procs()
            err_log.close()
            sox_err_log.close()

    return Response(generate(), mimetype='audio/wav')


@app.route('/status')
def status():
    return jsonify({
        'frequency': current_freq, 'gain': current_gain, 'squelch': squelch_level,
        'stereo': stereo_mode, 'demod_mode': demod_mode,
        'input_level': input_level, 'use_sox_deemph': use_sox_deemph,
        'lowpass_freq': lowpass_freq, 'lowpass_enabled': lowpass_enabled,
    })


@app.route('/stations')
def get_stations():
    with open(os.path.join(os.path.dirname(__file__), 'stations.json')) as f:
        return jsonify(json.load(f))


@app.route('/config')
def get_config():
    return jsonify(CONFIG)


@app.route('/log')
def get_log():
    lines = []
    for path, label in [
        (os.path.join(CONFIG['log_dir'], 'fm_tuner.log'), 'flask'),
        (os.path.join(CONFIG['log_dir'], 'rtl_err.log'), 'rtl_fm'),
        (os.path.join(CONFIG['log_dir'], 'sox_err.log'), 'sox'),
    ]:
        try:
            with open(path) as f:
                content = f.read().strip()
            if content:
                lines.append(f'--- {label} ({path}) ---')
                lines.extend(content.splitlines()[-20:])
        except FileNotFoundError:
            pass
    return '\n'.join(lines) or 'No log data', 200, {'Content-Type': 'text/plain; charset=utf-8'}


# ─── SCAN ────────────────────────────────────────────────────────────────────


def run_scan():
    """Background FM band scan using rtl_power FFT."""
    global scan_active, scan_results
    scan_results = {}
    scan_active = True

    try:
        cmd = [
            'rtl_power', '-d', 'FM-RADIO',
            '-f', '87500000:108000000:50000',
            '-i', '1', '-g', '40', '-1', '-'
        ]
        proc = __import__('subprocess').Popen(
            cmd, stdout=__import__('subprocess').PIPE,
            stderr=__import__('subprocess').DEVNULL
        )
        raw_out, _ = proc.communicate(timeout=60)
    except Exception:
        scan_active = False
        return

    lines = raw_out.decode('utf-8', errors='replace').strip().split('\n')
    all_freqs = []
    all_powers = []

    for line in lines:
        parts = line.split(',')
        if len(parts) < 7:
            continue
        try:
            low = int(parts[2])
            high = int(parts[3])
            powers = [float(x) for x in parts[6:] if x.strip()]
            if not powers:
                continue
            step = (high - low) / len(powers)
            all_freqs.extend([low + i * step for i in range(len(powers))])
            all_powers.extend(powers)
        except (ValueError, IndexError):
            continue

    # Find local maxima (peaks)
    threshold = 5
    peaks = []
    for i in range(1, len(all_powers) - 1):
        if (all_powers[i] > threshold
                and all_powers[i] >= all_powers[i - 1]
                and all_powers[i] >= all_powers[i + 1]):
            peaks.append((all_freqs[i], all_powers[i]))

    # Deduplicate: keep strongest peak per FM channel (within 180 kHz)
    peaks.sort(key=lambda x: -x[1])
    filtered = []
    for freq, db in peaks:
        too_close = any(abs(freq - f[0]) < 180_000 for f in filtered)
        if not too_close:
            filtered.append((freq, db))

    # Store in scan_results using FM channel center as key
    for freq_hz, db in filtered:
        ch = round((freq_hz - 87_500_000) / 200_000)
        ch_center = 87_500_000 + ch * 200_000
        if ch_center not in scan_results:
            scan_results[ch_center] = []
        scan_results[ch_center].append(db)

    scan_active = False

@app.route('/scan', methods=['POST'])
def start_scan():
    """Start a background FM band scan. Device must be free."""
    global scan_active, scan_thread, scan_results

    if scan_active:
        return jsonify({'status': 'already running'}), 409

    stop_watchdog()
    cleanup_procs()
    kill_all()
    time.sleep(0.5)

    scan_results = {}
    scan_active = True
    scan_thread = threading.Thread(target=run_scan, daemon=True)
    scan_thread.start()

    return jsonify({'status': 'started'})


@app.route('/scan_status')
def scan_status():
    """Poll scan route for live display."""
    with scan_lock:
        total_steps = 103
        done = len(scan_results)
        averaged = {freq: sum(vals) / len(vals) for freq, vals in scan_results.items()}
        top_stations = sorted(averaged.items(), key=lambda x: x[1], reverse=True)[:20]
        stations = [{'frequency': _snap_to_fm_channel(f), 'db': round(db, 1)} for f, db in top_stations]
        return jsonify({
            'active': scan_active,
            'progress': done,
            'total': total_steps,
            'stations': stations,
        })


@app.route('/scan_results')
def scan_results_endpoint():
    """Return finished scan results, sorted by signal strength descending."""
    with scan_lock:
        if scan_active:
            return jsonify({'status': 'in progress'}), 202
        if not scan_results:
            return jsonify({'status': 'no results'}), 204
        averaged = {freq: sum(vals) / len(vals) for freq, vals in scan_results.items()}
        sorted_results = sorted(averaged.items(), key=lambda x: x[1], reverse=True)
        return jsonify([
            {'frequency': _snap_to_fm_channel(freq), 'db': round(db, 1)}
            for freq, db in sorted_results
        ])


@app.route('/save_stations', methods=['POST'])
def save_stations():
    """Save a list of stations to stations.json."""
    stations = request.json
    if not isinstance(stations, list):
        return jsonify({'status': 'invalid format'}), 400
    path = os.path.join(os.path.dirname(__file__), 'stations.json')
    with open(path, 'w') as f:
        json.dump(stations, f, indent=2)
    return jsonify({'status': 'saved', 'count': len(stations)})


if __name__ == '__main__':
    kill_all()
    app.run(host=CONFIG['host'], port=CONFIG['port'])
