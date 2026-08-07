# capture_temp

Camera-side tools, built on one rule: **only one process opens the camera at
a time** (VmbPy gives it out exclusively). Whichever script below is running
owns the camera; everything else works from the CSV it writes.

- `camlib.py` — the shared library. `open_camera()` handles discovery and
  lifecycle; `read_temperatures()`, `capture_image()`, `save_image()` and
  friends take the already-open handle. New scripts should import from here
  instead of talking to VmbPy directly.
- `capture_temp.py` — polls the camera's built-in temperature sensors and
  prints the readings to the terminal or logs them to a CSV file.
- `grab_image.py` — grabs a single frame and saves it as a 16-bit TIFF.
- `monitor.py` — the combined logger: owns the camera for a whole run and
  records temperatures *and* frames together, one folder per run.
- `temp_to_serial.py` — never touches the camera. It follows a CSV that
  `capture_temp.py` or `monitor.py` is writing and forwards each new reading
  to an Arduino over USB serial, so a sketch can react to the camera
  temperature while a run is logging.

Tested against an Alvium G1-130 VSWIR, which reports two temperatures:

- `Sensor` — the image sensor itself
- `Mainboard` — the camera's electronics board

Any Allied Vision camera that exposes the standard GenICam `DeviceTemperature`
feature should work; the script discovers the available sensors at runtime.

## Requirements:

- Python 3.10 or newer
- Vimba X installed at `/home/labuser/workspace/upstream/vimbax-sdk` (provides
  the VmbPy wheel and the GenTL transport layers the camera driver needs)
- A camera connected and reachable (for GigE cameras, check it shows up in
  Vimba X Viewer if unsure)

> There is a second, older Vimba X copy at `/opt/vimbax` (VmbPy 1.2.1). Ignore
> it — `GENICAM_GENTL64_PATH` points at the `upstream/vimbax-sdk` transport
> layers, so the wheel has to come from there too or the versions mismatch.

## Setup

One-time setup — create a virtual environment in this directory and install
the dependencies (VmbPy comes from the local SDK wheel; the pinned path is in
`requirements.txt`):

```bash
cd /home/labuser/workspace/projects/fan-cooling/capture_temperature
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

After that, either activate the venv each session (`source .venv/bin/activate`)
or call the interpreter directly as `.venv/bin/python`.

## Usage

Read the temperature once:

```bash
.venv/bin/python capture_temp.py --count 1
```

```
opened camera: DEV_000A471E3D7C (Allied Vision Alvium G1-130 VSWIR (DEV_000A471E3D7C))
2026-07-09T16:12:24.248046+00:00  Sensor: 40.00 C
2026-07-09T16:12:24.248046+00:00  Mainboard: 35.50 C
```

Poll continuously (1 sample/second, Ctrl-C to stop):

```bash
.venv/bin/python capture_temp.py
```

Log to a CSV every 5 seconds until stopped:

```bash
.venv/bin/python capture_temp.py --interval 5 --csv temps.csv
```

### Cooling test

`--cooling-test` records a named run for comparing mounts, heatsinks and fan
settings — a reading every 30 seconds for 10 minutes, written to a file named
after the test and echoed to the terminal as it goes:

```bash
.venv/bin/python capture_temp.py --cooling-test
```

```
Name for this test (e.g. carbon_fiber): fan_heatsink_12v
opened camera: DEV_000A471E3D7C (Allied Vision Alvium G1-130 VSWIR (DEV_000A471E3D7C))
logging 21 readings to fan_heatsink_12v_20260806_151301.csv
press Ctrl-C to stop early
15:13:01     0s  Sensor 31.70 C  Mainboard 28.40 C
15:13:31    30s  Sensor 32.20 C  Mainboard 28.90 C
```

Pass `--name` to skip the prompt (spaces in the name become underscores), and
`--interval` / `--count` to change the cadence or length. Stopping early with
Ctrl-C keeps everything written so far.

### Options

| Flag | Meaning | Default |
|------|---------|---------|
| `--camera ID` | which camera to open | first camera found |
| `--interval SECONDS` | time between samples | `1.0` (`30.0` with `--cooling-test`) |
| `--count N` | number of samples to take | `0` (run until Ctrl-C), `21` with `--cooling-test` |
| `--csv FILE` | append samples to this file instead of printing | off |
| `--cooling-test` | log a named run to `<name>_<timestamp>.csv` | off |
| `--name NAME` | test name for `--cooling-test` | ask for one |

### CSV format

There are two layouts. `--csv` writes one row per sensor, keyed by timestamp:

```
timestamp,sensor,celsius
2026-07-09T16:17:01.034538+00:00,Sensor,46.80
2026-07-09T16:17:01.034538+00:00,Mainboard,42.50
```

Timestamps are UTC (ISO-8601). The header is only written when the file is new
or empty, so re-running with the same `--csv` file appends to the existing log.
This is the layout `temp_to_serial.py` reads.

`--cooling-test` writes one row per sample instead, with a column per sensor
and the time since the run started, which is easier to plot and to compare
between runs:

```
time,seconds_elapsed,Sensor,Mainboard
15:13:01,0,31.70,28.40
15:13:31,30,32.20,28.90
```

Times here are local, and each run gets its own file, so nothing is appended.

Both are flushed after every sample, so you can watch a long run live with
`tail -f`.

Status messages (which camera was opened, interrupts) go to stderr, so stdout
stays clean for piping.

## Grabbing an image

`grab_image.py` opens the camera, takes one shot, saves it as a 16-bit TIFF
(Mono12 data in the low 12 bits, so values run 0–4095), and prints the saved
path to stdout:

```bash
.venv/bin/python grab_image.py                       # image_<timestamp>.tif
.venv/bin/python grab_image.py --out frame.tif --exposure-us 160000 --gain-db 10
```

`--exposure-us` / `--gain-db` switch the matching auto mode off and fix the
value; without them the camera keeps whatever it was doing (usually
auto-exposure — pass `--warmup N` to let it settle across N discarded frames
first). Don't run this while `monitor.py` or `capture_temp.py` has the
camera open.

## Logging temperatures and images together

`monitor.py` is the single owner for a whole run: every interval it reads all
temperature sensors and (optionally) grabs a frame, keeping the times, image
filenames, and temperatures together in one folder:

```bash
.venv/bin/python monitor.py --name heatsink_v2 --interval 5 --frame-every 6
```

```
runs/heatsink_v2_20260806_164608/
├── data.csv
└── frames/
    ├── frame_0000.tif
    └── frame_0001.tif
```

`data.csv` has one row per sample; the `filename` column is empty on samples
where no frame was taken:

```
timestamp,seconds_elapsed,filename,Sensor,Mainboard
2026-08-06T20:46:24.716534+00:00,0.000,frames/frame_0000.tif,45.30,41.20
2026-08-06T20:46:29.716675+00:00,5.000,,45.30,41.20
```

Defaults mirror `--cooling-test` (a sample every 30 s, 21 samples); `--count 0`
runs until Ctrl-C, and stopping early keeps everything written so far.
`--frame-every K` grabs a frame on every Kth sample (`0` = temperatures only)
— frames are ~2.6 MB each, so sampling temperature every 5 s with
`--frame-every 6` keeps the fan feed fresh while writing a frame only every
30 s. `--exposure-us`, `--gain-db`, and `--warmup` work as in `grab_image.py`.

`--live-csv FILE` additionally appends every reading to the rolling
`timestamp,sensor,celsius` CSV that `temp_to_serial.py` follows — that is how
the fan keeps running while a run logs. From `fan_test/`, `make feed-monitor`
launches the pair (`scripts/feed_monitor.sh`), just like `make feed` does for
the temperature-only feed.

## Feeding the temperature to an Arduino

`temp_to_serial.py` never talks to the camera — it just follows the CSV that
`capture_temp.py` (or `monitor.py --live-csv`) is writing, so the two run
side by side and either one can be restarted without disturbing the other.

Terminal 1 — log temperatures:

```bash
.venv/bin/python capture_temp.py --csv temps.csv
```

Terminal 2 — forward them to the Arduino:

```bash
.venv/bin/python temp_to_serial.py --csv temps.csv
```

On connect it sends the most recent reading already in the file, then one
line per new sample. Each line is just the value in °C followed by a newline
(e.g. `46.80\n`) — nothing else, so it's trivial to parse on the Arduino.

### Options

| Flag | Meaning | Default |
|------|---------|---------|
| `--csv FILE` | CSV file to follow (required) | — |
| `--port PORT` | Arduino serial port | `/dev/ttyACM0` |
| `--baud N` | must match `Serial.begin(N)` in the sketch | `9600` |
| `--sensor NAME` | which sensor to forward (`Sensor` or `Mainboard`) | `Sensor` |

### Reading it in a sketch

```cpp
float cameraTemp = 0.0;
unsigned long lastUpdate = 0;

void setup() {
  Serial.begin(9600);
}

void loop() {
  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    cameraTemp = line.toFloat();
    lastUpdate = millis();
  }
  // ... act on cameraTemp; treat it as stale if
  // millis() - lastUpdate grows too large ...
}
```

Two things to keep in mind on the Arduino side:

- **The board resets when the port opens.** The forwarder waits 2 seconds
  after connecting before sending, so the sketch won't miss the first value.
- **Handle staleness.** If the forwarder or the camera logger stops, no new
  lines arrive. Track `millis()` since the last update and fall back to a
  safe behavior if it's been too long.

## Good to know

- **Warm-up drift:** readings climb for several minutes after the camera
  powers on. During testing the sensor went from 40 °C to ~47 °C in the first
  few minutes. Let the camera reach steady state before comparing numbers.
- **Startup takes a few seconds** — camera discovery (especially over GigE)
  runs before the first sample appears. A Ctrl-C during this window is held
  briefly and honored right after startup finishes.
- **One owner per camera:** VmbPy hands the camera out exclusively, so only
  one of `capture_temp.py` / `grab_image.py` / `monitor.py` can run at a
  time. A second script fails cleanly while the first one runs — with
  `--camera ID` it reports `could not open camera '...' (in use by another
  process?)`; without it, discovery skips the busy camera and times out with
  `no camera with a DeviceTemperature feature found`. When you need
  temperatures and images at once, that's `monitor.py`'s job, and anything
  that only needs temperature numbers (like `temp_to_serial.py`) should read
  the CSV instead of the camera. Within the owning process the same rule
  applies in miniature: reading all sensors switches
  `DeviceTemperatureSelector` back and forth, so temperature reads and frame
  grabs happen strictly one after another (camlib's docstrings spell this
  out).

## Troubleshooting

- **`error: no cameras found`** — check the camera is powered and connected,
  and that `GENICAM_GENTL64_PATH` includes
  `/home/labuser/workspace/upstream/vimbax-sdk/cti`
  (`echo $GENICAM_GENTL64_PATH`). If it's missing, run
  `source /home/labuser/workspace/upstream/vimbax-sdk/cti/Set_GenTL_Path.sh`
  or fix your shell profile.
- **`error: camera '...' not found`** — the `--camera` ID doesn't match.
  Run
  `.venv/bin/python /home/labuser/workspace/upstream/vimbax-sdk/api/examples/VmbPy/list_cameras.py`
  to see the IDs of connected cameras.
- **`error: camera '...' has no DeviceTemperature feature`** — this camera
  model doesn't report temperature; nothing the script can do about it.
- **`error: could not open camera '...' (in use by another process?)`** —
  another script already owns the camera (see "One owner per camera" above).
  Stop it, or use its CSV output instead.
- **Permission denied opening `/dev/ttyACM0`** — your user needs to be in the
  `dialout` group: `sudo usermod -aG dialout $USER`, then log out and back in.
- **Arduino on a different port** — unplug it, run `ls /dev/ttyACM* /dev/ttyUSB*`,
  plug it back in, run it again; the device that appeared is your port. Pass
  it with `--port`.
