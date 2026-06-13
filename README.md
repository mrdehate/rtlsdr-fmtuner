# FM Radio Tuner

Networked FM radio receiver built on a Raspberry Pi with an RTL-SDR Blog v4 dongle. Streams local FM stations over HTTP with a web interface and REST API.

---

## Hardware Requirements

- **Raspberry Pi** (any Pi with USB — tested on Pi 4)
- **RTL-SDR Blog v4** dongle (or any `rtl_sdr`-compatible device)
- USB penetration for the SDR dongle
- Network access from your Mac/PC/phone to the Pi

---

## Software Setup

### 1. Install dependencies

On the Pi:

```bash
# RTL-SDR toolchain
sudo apt install librtlsdr-dev rtl-sdr

# Build rtl_fm from source (required for FM demodulation)
cd /tmp
git clone https://github.com/steve-m/librtlsdr.git
cd librtlsdr && mkdir build && cd build
cmake .. && make && sudo make install && sudo ldconfig

# Verify
rtl_test -d 0
```

```bash
# sox (audio processing)
sudo apt install sox

# Flask (web server)
pip3 install flask
```

### 2. Locate your RTL-SDR device

```bash
# List all detected RTL-SDR devices
rtl_test -d 0
# Note the serial/name printed — e.g. "RTL2838UHIDIR" or "FM-RADIO"
```

If you have multiple SDRs and they swap positions, using the **serial name** instead of device index is more reliable.

### 3. Configure

Edit `config.json`:

```json
{
  "device": "FM-RADIO",
  "port": 8081,
  "host": "0.0.0.0",
  "log_dir": "/tmp",
  "defaults": {
    "frequency": 99.1,
    "gain": 40,
    "squelch": 0,
    "demod_mode": "narrow",
    "input_level": 1.0,
    "use_sox_deemph": false,
    "lowpass_enabled": true,
    "lowpass_freq": 12000
  }
}
```

| Setting | Description |
|---|---|
| `device` | RTL-SDR device name or serial. Run `rtl_test -d 0` to find yours |
| `port` | HTTP port for the web UI and API |
| `host` | Network interface to bind (always `0.0.0.0` for network access) |
| `log_dir` | Where to write the Flask, rtl_fm, and sox log files |
| `defaults.*` | Default tuning parameters |

### 4. Configure preset stations

Edit `stations.json`:

```json
[
  {"frequency": 99.1, "name": "99.1 FM", "signal": 5},
  {"frequency": 94.9, "name": "94.9 FM", "signal": 4}
]
```

| Field | Description |
|---|---|
| `frequency` | Station frequency in MHz |
| `name` | Station name |
| `signal` | Signal strength 1–5 (used for star rating display) |

### 5. Run it

```bash
cd ~/fm-tuner
python3 app.py
```

Then open `http://hostname.local:8081` in your browser.

To run in the background:

```bash
nohup python3 app.py >> /tmp/fm_tuner.log 2>&1 &
```

---

## Auto-Start on Boot (systemd)

On the Pi, as your user:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/fm-tuner.service << 'EOF'
[Unit]
Description=FM Radio Tuner
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/YOUR_USER/fm-tuner/app.py
Restart=always
WorkingDirectory=/home/YOUR_USER/fm-tuner
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload
systemctl --user enable fm-tuner
systemctl --user start fm-tuner
```

---

## Web Interface

**Left column — Now Playing:**
- Large frequency display with current station
- Play/Stop button → triggers `/stream` via audio element
- Mode badge (Narrow FM / Wide FM)
- Status dots: Stream, RTL-SDR Device
- Live log — polls server-side logs every 4 seconds

**Right column — Controls:**
- Frequency slider + preset station buttons (from `stations.json`)
- Collapsible sections: Demodulation, Audio Mode, Filters & Levels, Gain & Squelch
- All controls retune on change (debounced 400ms)

---

## REST API

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Web UI |
| `/stream` | GET | Audio stream (`audio/wav`) — triggers `rtl_fm → sox` pipeline |
| `/tune` | POST | Update tuning parameters |
| `/status` | GET | Current settings as JSON |
| `/stations` | GET | Preset station list from `stations.json` |
| `/config` | GET | Full config.json contents |
| `/log` | GET | Last 20 lines of server logs as plain text |
| `/scan` | POST | Start background FM band scan via rtl_fm |
| `/scan_status` | GET | Scan progress + live station list: `{active, progress, total, stations}` |
| `/scan_results` | GET | Finished scan results sorted by dB strength descending |
| `/save_stations` | POST | Save station list to `stations.json` (replaces file) |

**`/tune` request body:**
```json
{
  "frequency": 99.1,
  "gain": 40,
  "squelch": 0,
  "stereo": false,
  "demod_mode": "narrow",
  "input_level": 1.0,
  "use_sox_deemph": false,
  "lowpass_enabled": true,
  "lowpass_freq": 12000
}
```

---

## Home Assistant Integration

**media_player:**
```yaml
media_player:
  - platform: url
    name: "FM Radio"
    api_url: "http://hostname.local:8081/stream"
```

**Tuning via shell_command:**
```yaml
shell_command:
  radio_tune: >
    curl -X POST http://hostname.local:8081/tune
    -H "Content-Type: application/json"
    -d '{{ states("input_text.radio_tune_json") }}'
```

---

## Architecture

```
RTL-SDR Blog v4 dongle
    ↓
rtl_fm (tuner + FM demodulation, DC offset correction)
    ↓  (unix pipe)
sox (lowpass filter, volume, de-emphasis)
    ↓  (unix pipe)
Flask (HTTP stream server / web UI)
    ↓
Browser / Home Assistant
```

- **rtl_fm** — handles the RTL2832 chip, tunes to frequency, FM demodulates
- **sox** — audio processing. The **lowpass filter** is capped at 10 kHz internally — above this at 48 kHz sample rate sox's Chebyshev filters can deadlock
- **Flask** — serves the audio stream as chunked HTTP `audio/wav`

---

## Troubleshooting

### Stream cuts out after a few minutes
- The sox `lowpass` filter is the most common cause — keep it **OFF** or leave `lowpass_freq` at the default 12 kHz (internally capped to 10 kHz)
- Check logs at `http://hostname.local:8081/log` for sox or rtl_fm errors

### No audio / device busy
- Another process is holding the RTL-SDR. Run `pkill -9 -f rtl_fm` and `pkill -9 -f sox`
- If that doesn't work, check `lsusb` to verify the device isstill connected

### Device not found
- Try `rtl_test -d 0` to verify the dongle appears as device 0
- If you have multiple SDRs, use the **device serial name** in `config.json` (`-d FM-RADIO`) rather than index (`-d 0`)
- The RTL-SDR Blog v4 with RTL2838 chip shows up as `RTL2838UHIDIR` by default

### Stream won't start in browser
- Some browsers block autoplay — click the page first, then press Play
- Try Chrome — it's the most reliable for this kind of continuous audio stream

### Preset buttons show "Failed to load stations"
- Check that `stations.json` is in the same directory as `app.py`
- Check `curl http://localhost:8081/stations` on the Pi

---

## Known Limitations

- **Mono only** — RTL-SDR Blog v4 + rtl_fm produces mono audio in FM mode. "Wide FM" enables 200 kHz wideband mode (vs 170 kHz narrow) — still mono
- **One frequency at a time** — one RTL-SDR, one frequency
- **Lowpass capped** — slider allows up to 20 kHz but the backend caps to 10 kHz to prevent sox instability
