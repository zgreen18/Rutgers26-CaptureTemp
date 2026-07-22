# capture_temp

Two small tools:

- `capture_temp.py` — polls an Allied Vision camera's built-in temperature
  sensors and prints the readings to the terminal or logs them to a CSV file.
- `temp_to_serial.py` — follows that CSV and forwards each new reading to an
  Arduino over USB serial, so a sketch can react to the camera temperature.

Tested against an Alvium G1-130 VSWIR, which reports two temperatures:

- `Sensor` — the image sensor itself
- `Mainboard` — the camera's electronics board

Any Allied Vision camera that exposes the standard GenICam `DeviceTemperature`
feature should work; the script discovers the available sensors at runtime.

## Requirements

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
VmbPy from the local wheel:

```bash
cd /home/labuser/workspace/projects/fan-cooling/capture_temperature
python3 -m venv .venv
source .venv/bin/activate
pip install /home/labuser/workspace/upstream/vimbax-sdk/api/python/vmbpy-1.2.2-py3-none-manylinux_2_27_x86_64.whl
pip install pyserial   # only needed for temp_to_serial.py
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

### Options

| Flag | Meaning | Default |
|------|---------|---------|
| `--camera ID` | which camera to open | first camera found |
| `--interval SECONDS` | time between samples | `1.0` |
| `--count N` | number of samples to take | `0` (run until Ctrl-C) |
| `--csv FILE` | append samples to this file instead of printing | off |

### CSV format

```
timestamp,sensor,celsius
2026-07-09T16:17:01.034538+00:00,Sensor,46.80
2026-07-09T16:17:01.034538+00:00,Mainboard,42.50
```

Timestamps are UTC (ISO-8601). Each sample writes one row per sensor. The
header is only written when the file is new or empty, so re-running with the
same `--csv` file appends to the existing log. The file is flushed after every
sample, so you can watch a long run live with `tail -f temps.csv`.

Status messages (which camera was opened, interrupts) go to stderr, so stdout
stays clean for piping.

## Feeding the temperature to an Arduino

`temp_to_serial.py` never talks to the camera — it just follows the CSV that
`capture_temp.py` is writing, so the two run side by side and either one can
be restarted without disturbing the other.

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
- **One reader per camera:** reading all sensors works by switching the
  camera's `DeviceTemperatureSelector` back and forth, so don't run two
  instances of this script (or another feature-touching tool) against the same
  camera at the same time.

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
- **Permission denied opening `/dev/ttyACM0`** — your user needs to be in the
  `dialout` group: `sudo usermod -aG dialout $USER`, then log out and back in.
- **Arduino on a different port** — unplug it, run `ls /dev/ttyACM* /dev/ttyUSB*`,
  plug it back in, run it again; the device that appeared is your port. Pass
  it with `--port`.
