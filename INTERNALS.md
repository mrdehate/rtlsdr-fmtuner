# FM Tuner — Internal Reference

Detailed breakdown of how the FM tuner works under the hood. This is the authoritative reference for any future feature work or debugging.

---

## Architecture Overview

```
HTTP Request → Flask → rtl_fm → pipe → sox → Flask Response → browser
     ↑ stop/cleanup    ↑↑↑ pipe pair
```

The system is a **stateless REST server** backed by **two stateful signal-processing subprocesses**. Each request to `/stream` is independent — it kills any existing pipeline, spawns a fresh one, and streams audio until the client disconnects.

---

## The Two Subprocesses

### rtl_fm
Pulls raw I/Q samples from the RTL-SDR hardware, downconverts to baseband, demodulates FM, and outputs 48 kHz PCM audio.

Key arguments used:
- `-d FM-RADIO` — selects device 0 by name (safer than `-d 0` which can shift after reboot)
- `-f <freq>` — centre frequency in Hz (`app.py` converts MHz → Hz)
- `-M fm | wbfm` — demodulation mode: `fm` = narrow FM, `wbfm` = wideband FM
- `-s <samp_rate>` — input sample rate (170 kHz for narrow FM, 200 kHz for wideband FM)
- `-r 48000` — output sample rate (fixed)
- `-E dc` — DC offset correction (essential for RTL-SDR)
- `-g <gain>` — front-end gain in dB, or `0` for auto
- `-l <squelch>` — squelch level (0 = off)
- `-F 9` — internal LMS VNA (only used in `wide_alt` mode)

The process reads from the RTL-SDR USB device and **writes PCM audio to a unix pipe** (stdout).

### sox
Reads PCM audio from stdin (fed by rtl_fm), applies audio processing, and writes a WAV file to stdout (piped to Flask).

Key processing applied:
- `-v <input_level>` — input volume multiplier (Booster)
- `lowpass <freq>` — low-pass filter at `effective_lp_freq` Hz (if enabled). **Capped at 10,000 Hz** internally to prevent sox instability — setting the slider to 20 kHz doesn't actually produce 20 kHz cutoff
- `deemph` — 75μs de-emphasis (alternate to rtl_fm's built-in deemph)
- Output format: 16-bit signed PCM, 48 kHz, mono → WAV format framing → Flask

The process reads from the pipe from rtl_fm and **writes WAV chunks to Flask**.

---

## Parsing State

The application stores current tuning state in **module-level global variables**:

| Variable | Type | Default | Description |
|---|---|---|---|
| `rtl_proc` | `subprocess.Popen \| None` | None | Active rtl_fm process |
| `sox_proc` | `subprocess.Popen \| None` | None | Active sox process |
| `current_freq` | float | 91.7 | Tuned frequency (MHz) |
| `current_gain` | int \| str | 40 | Gain in dB, or `"auto"` |
| `squelch_level` | int | 0 | Squelch 0–200 |
| `stereo_mode` | bool | False | Stereo mode (wide FM only) |
| `demod_mode` | str | `"narrow"` | One of `"narrow"`, `"wide"`, `"wide_alt"` |
| `input_level` | float | 1.0 | Sox volume multiplier |
| `use_sox_deemph` | bool | False | Use sox deemph instead of RTL |
| `lowpass_freq` | int | 12000 | Requested lowpass cutoff (Hz) |
| `lowpass_enabled` | bool | True | Whether lowpass is active |

These are set by **`/tune`** and read by **`/stream`** when spawning the pipeline.

---

## `/tune` Endpoint

**Method:** `POST`  
**Content-Type:** `application/json`  
**Purpose:** Update tuning parameters without starting a stream

```
1. Stop watchdog + cleanup any existing rtl_fm/sox processes
2. Read JSON body → update all the global state variables
3. Return confirmation JSON
```

What it does NOT do:
- It does NOT start the stream (that requires accessing `/stream`)
- It does NOT contact rtl_fm or sox in any way
- It is purely for updating the state store that `/stream` will use next

This endpoint is lightweight and safe to call frequently (e.g. from a slider drag). The actual retuning happens when `/stream` is accessed.

---

## `/stream` Endpoint

**Method:** `GET`  
**Purpose:** Start the signal chain and stream audio as HTTP chunked transfer

```
1. Stop watchdog + cleanup any existing rtl_fm/sox processes
2. Build rtl_fm argument list based on current state
3. Build sox argument list based on current state (with lowpass cap)
4. Create a unix pipe pair (r_pipe, w_pipe)
5. Spawn rtl_fm with stdout → w_pipe (preexec_fn=os.setsid)
6. Spawn sox with stdin ← r_pipe, stdout → Flask (preexec_fn=os.setsid)
7. Start watchdog thread to monitor sox liveness
8. Return Response(generate(), mimetype='audio/wav')
```

### Pipe Setup Detail

```python
r_pipe, w_pipe = os.pipe()        # r_pipe read end, w_pipe write end
rtl_proc = Popen(rtl_args, stdout=w_pipe, stderr=err_log, preexec_fn=os.setsid)
os.close(w_pipe)                  # rtl_fm inherited w_pipe as stdout
sox_proc = Popen(sox_args, stdin=r_pipe, stdout=subprocess.PIPE, stderr=sox_err_log, preexec_fn=os.setsid)
os.close(r_pipe)                  # sox inherited r_pipe as stdin
```

The `os.setsid` in `preexec_fn` puts each process in its own process group — this is critical because it allows `killpg` ( 而不是 `kill`) to terminate the entire chain including any forked children.

### The Generator Loop

```python
def generate():
    while True:
        chunk = sox_proc.stdout.read(8192)
        if not chunk: break       # EOF = stream ended cleanly
        yield chunk              # 8KB chunks → Flask → HTTP → browser
```

Flask's `Response` streams this generator chunk-by-chunk via WSGI. The browser sees a continuous audio stream.

### Watchdog Thread

A daemon thread runs alongside the generator:
```python
watchdog_active = True
def watchdog_inner():
    while watchdog_active:
        time.sleep(3)
        if proc.poll() is not None:  # sox died
            print("[WATCHDOG] sox exited")
            break
```

If sox dies, the generator loop gets an empty read on next iteration, exits cleanly, and the `finally:` block runs `cleanup_procs()` + close logs.

### Lifecycle of One Stream Request

```
Browser requests /stream
        ↓
cleanup_procs() kills any previous pipeline
        ↓
rtl_fm spawned → pipe → sox spawned → Flask generator
        ↓
Browser starts receiving audio
        ↓
(sox or rtl_fm dies OR browser disconnects)
        ↓
generate() gets empty chunk → generator exits
        ↓
finally: cleanup_procs() + stop_watchdog() + close logs
        ↓
Flask sends last chunk to browser → connection closes
```

---

## Demodulation Modes

| Mode | rtl_fm args | Notes |
|---|---|---|
| `narrow` (default) | `-M fm -s 170000` | Default. Sharp tuning, good adjacent station rejection |
| `wide` | `-M wbfm -s 200000` | Wide FM, for stations with multipath issues. Enables stereo implicitly |
| `wide_alt` | `-M wbfm -s 200000 -F 9` | Same as wide but with LMS VNA — alternative demod path |

The `stereo` flag only has meaning for wide FM modes. `wide_alt` uses rtl_fm's alternate demodulator. The narrow/wide split is the primary tradeoff between selectivity (narrow) and multipath tolerance (wide).

---

## Lowpass Filter — The Instability Issue

**Root cause:** The sox `lowpass` filter at cutoff frequencies above ~10 kHz, combined with the sample rate of 48 kHz, requires heavy Chebyshev filter computation that under certain conditions (especially at higher input levels) can cause sox to crash or deadlock.

**The fix:** `effective_lp_freq = min(lowpass_freq, 10000)` — the slider goes up to 20,000 Hz but the actual filter cutoff sent to sox is capped at 10,000 Hz. This eliminates the instability while still providing useful rolloff above ~8 kHz.

If lowpass is needed for a specific reason (e.g. removing high-frequency noise from a weak station), keeping it **below 10 kHz** is safe and stable.

---

## Process Management

### `kill_all()`
Uses `pkill -9 -f <cmd>` — brute-force process kill by name pattern. Runs on startup (`if __name__ == '__main__'`) to clear any orphans from a crash.

### `cleanup_procs()`
Preferred method during normal operation:
- Iterates `rtl_proc` and `sox_proc` globals
- For each: `os.killpg(os.getpgid(proc.pid), signal.SIGTERM)` — kills process group
- Sets both globals to `None`

Uses SIGTERM (not SIGKILL) for graceful termination. SIGKILL (`pkill -9`) is reserved for `kill_all()` because orphaned processes may ignores SIGTERM.

### Process Group Why

Both `preexec_fn=os.setsid` and `killpg` are needed because:
- Without `os.setsid`: processes are in the same group as Flask → killing them could affect Flask
- Without `killpg`: only the parent process dies, orphaned children keep the pipe open → deadlock

---

## Error Logs

Two files capture stderr from the subprocesses:
- `/tmp/rtl_err.log` — rtl_fm stderr
- `/tmp/sox_err.log` — sox stderr

These are opened fresh on each `/stream` call and closed in the generator's `finally:` block. They are the first stop when diagnosing audio problems.

Sample rtl_fm error log showing a clean signal detection:
```
[rtl_fm output]
Found 2 device(s):
  0:  RTLSDRBlog, Blog V4, SN: FM-RADIO
  1:  RTLSDRBlog, Blog V4, SN: ADSB1090
Using device 0: Generic RTL2832U OEM
Found Rafael Micro R828D tuner
Signal caught, exiting!        ← rtl_fm received no signal, exited cleanly
User cancel, exiting...        ← this is from SIGTERM
```

"Signal caught, exiting!" with no User cancel message means rtl_fm found no signal and exited on its own. "User cancel, exiting" is SIGTERM from cleanup.

---

## Dependencies

**System packages (on the Pi):**
```
rtl-sdr
sox
python3
python3-flask
```

**Location:**
- App: `/home/markd/fm-tuner/app.py`
- Templates: `/home/markd/fm-tuner/templates/index.html`
- Logs: `/tmp/rtl_err.log`, `/tmp/sox_err.log` (tmpfs, cleared on reboot)

**Running as:** the `markd` user (not root). Start manually or via systemd user service.

---

## Future Feature Considerations

### Adding Icecast
The current design streams directly over HTTP. For whole-house multi-client streaming, Icecast would let multiple simultaneous clients share one rtl_fm process instead of each HTTP request spawning a new pipeline. That would require:
1. Icecast server config on port 8000
2. Liquidsoap or darkice between sox output and Icecast input
3. Change `/stream` to forward to the Icecast mount instead of serving directly

### Stereo Support
`rtl_fm -M wbfm` can output Stereo I/Q. The current app doesn't set up stereo properly — implementing it would require splitting the I/Q into L/R channels after rtl_fm (which doesn't natively output stereo PCM easily).

### RDS Decoder
RTL-SDR can decode RDS (Radio Data System) from strong stations — station name, song title, etc. This would require piping rtl_fm output to an RDS decoder (like `rtl_fm -M fm -s 170k -r 48k | some_rds_decoder`).

### Multiple Simultaneous Stations
Would need multiple RTL-SDR devices, one per station, or a channelized approach where one RTL-SDR is tuned to a multiplex and demuxed into multiple station streams.
