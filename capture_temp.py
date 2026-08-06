#!/usr/bin/env python3
"""Poll an Allied Vision camera's temperature sensors via VmbPy.

Prints readings to stdout, or appends them to a CSV with --csv.

--cooling-test instead logs a named run to <test_name>_<timestamp>.csv, with
one column per sensor, for comparing mounts and heatsinking setups.
"""

import argparse
import contextlib
import csv
import os
import signal
import sys
import time
from datetime import datetime, timezone

from vmbpy import VmbCameraError, VmbFeatureError, VmbSystem

# --cooling-test defaults: a reading every 30 s for 10 minutes, matching the
# runs in camera-cooling-tests/run_cooling_test.py.
COOLING_INTERVAL = 30.0
COOLING_MINUTES = 10
COOLING_COUNT = int(COOLING_MINUTES * 60 / COOLING_INTERVAL) + 1


@contextlib.contextmanager
def defer_sigint():
    """Postpone Ctrl-C until the block exits.

    Interrupting VmbC while it is initializing leaves the library in a state
    that crashes on process exit, so hold the signal during startup.
    """
    interrupted = False

    def handler(signum, frame):
        nonlocal interrupted
        interrupted = True

    previous = signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
        if interrupted:
            raise KeyboardInterrupt


def parse_args():
    p = argparse.ArgumentParser(
        description="Poll camera temperature sensors and print or log them."
    )
    p.add_argument("--camera", default=None,
                   help="camera ID to open (default: first camera that has "
                        "a DeviceTemperature feature)")
    p.add_argument("--interval", type=float, default=None,
                   help=f"seconds between samples (default: 1.0, or "
                        f"{COOLING_INTERVAL:g} with --cooling-test)")
    p.add_argument("--count", type=int, default=None,
                   help="number of samples to take; 0 = run until Ctrl-C "
                        f"(default: 0, or {COOLING_COUNT} with "
                        "--cooling-test)")
    p.add_argument("--csv", default=None, metavar="FILE",
                   help="append samples to this CSV file instead of stdout")
    p.add_argument("--cooling-test", action="store_true",
                   help="log a named cooling test to <name>_<timestamp>.csv "
                        "with one column per sensor, echoing each reading")
    p.add_argument("--name", default=None, metavar="NAME",
                   help="test name for --cooling-test (default: ask for one)")

    args = p.parse_args()
    if args.cooling_test and args.csv:
        p.error("--cooling-test and --csv are mutually exclusive")
    if args.name is not None and not args.cooling_test:
        p.error("--name only applies with --cooling-test")
    if args.interval is None:
        args.interval = COOLING_INTERVAL if args.cooling_test else 1.0
    if args.count is None:
        args.count = COOLING_COUNT if args.cooling_test else 0
    return args


def cooling_filename(name):
    """Build <test_name>_<stamp>.csv, asking for the name if not given.

    Spaces become underscores so the filename stays tidy, and the timestamp
    keeps each run next to the results of previous ones.
    """
    if name is None:
        name = input("Name for this test (e.g. carbon_fiber): ")
    name = name.strip().replace(" ", "_") or "unnamed"
    return f"{name}_{datetime.now():%Y%m%d_%H%M%S}.csv"


def get_camera(vmb, camera_id, discovery_timeout=15.0):
    """Return the camera to poll.

    With --camera, open that ID. Otherwise pick the first camera that
    actually exposes DeviceTemperature — enumeration can also contain
    simulators or other cameras without one. GigE cameras may take several
    seconds to be discovered, so keep re-scanning until the timeout.
    """
    if camera_id is not None:
        try:
            return vmb.get_camera_by_id(camera_id)
        except VmbCameraError:
            sys.exit(f"error: camera '{camera_id}' not found")

    deadline = time.monotonic() + discovery_timeout
    seen = []
    while True:
        for cam in vmb.get_all_cameras():
            if cam.get_id() not in seen:
                seen.append(cam.get_id())
            try:
                with cam:
                    cam.get_feature_by_name("DeviceTemperature")
                return cam
            except (VmbCameraError, VmbFeatureError):
                continue
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)
    detail = f" (saw: {', '.join(seen)})" if seen else ""
    sys.exit("error: no camera with a DeviceTemperature feature "
             f"found{detail}")


def read_temperatures(cam):
    """Return a list of (sensor_name, celsius) for every available sensor.

    Iterating mutates DeviceTemperatureSelector, so this is not safe to run
    concurrently against the same camera, and the selector is left pointing
    at the last sensor read.
    """
    temp = cam.get_feature_by_name("DeviceTemperature")
    try:
        selector = cam.get_feature_by_name("DeviceTemperatureSelector")
    except VmbFeatureError:
        return [("Device", temp.get())]

    readings = []
    for entry in selector.get_available_entries():
        selector.set(entry)
        readings.append((str(entry), temp.get()))
    return readings


def main():
    args = parse_args()

    # Settle the filename before touching the camera, so waiting at the prompt
    # does not hold a camera open and Ctrl-C there costs nothing.
    filename = cooling_filename(args.name) if args.cooling_test else None

    with contextlib.ExitStack() as stack:
        with defer_sigint():
            vmb = stack.enter_context(VmbSystem.get_instance())
        cam = get_camera(vmb, args.camera)
        with defer_sigint():
            stack.enter_context(cam)

        try:
            cam.get_feature_by_name("DeviceTemperature")
        except VmbFeatureError:
            sys.exit(f"error: camera '{cam.get_id()}' has no "
                     "DeviceTemperature feature")

        print(f"opened camera: {cam.get_id()} ({cam.get_name()})",
              file=sys.stderr)

        csv_file = None
        writer = None
        # Cooling mode puts each sensor in its own column, but which sensors
        # exist is only known once the first sample has been read.
        columns = None
        if args.cooling_test:
            csv_file = open(filename, "w", newline="")
            writer = csv.writer(csv_file)
            how_many = (f"{args.count} reading{'s' * (args.count != 1)}"
                        if args.count else "readings")
            print(f"logging {how_many} to {filename}", file=sys.stderr)
            print("press Ctrl-C to stop early", file=sys.stderr)
        elif args.csv:
            is_new = (not os.path.exists(args.csv)
                      or os.path.getsize(args.csv) == 0)
            csv_file = open(args.csv, "a", newline="")
            writer = csv.writer(csv_file)
            if is_new:
                writer.writerow(["timestamp", "sensor", "celsius"])
                csv_file.flush()

        try:
            taken = 0
            start = time.monotonic()
            while args.count == 0 or taken < args.count:
                # Aim at a fixed offset from the start rather than sleeping a
                # whole interval after each sample, so the time spent reading
                # does not accumulate into drift over a long run.
                wait = taken * args.interval - (time.monotonic() - start)
                if wait > 0:
                    time.sleep(wait)

                elapsed = round(time.monotonic() - start)
                readings = read_temperatures(cam)

                if args.cooling_test:
                    now = datetime.now().strftime("%H:%M:%S")
                    if columns is None:
                        columns = [sensor for sensor, _ in readings]
                        writer.writerow(["time", "seconds_elapsed", *columns])
                    # Look sensors up by name, so one dropping out mid-run
                    # leaves a gap instead of shifting every later column.
                    values = dict(readings)
                    writer.writerow([now, elapsed] + [
                        f"{values[c]:.2f}" if c in values else ""
                        for c in columns])
                    shown = "  ".join(f"{sensor} {celsius:.2f} C"
                                      for sensor, celsius in readings)
                    print(f"{now}  {elapsed:4d}s  {shown}")
                else:
                    timestamp = datetime.now(timezone.utc).isoformat()
                    for sensor, celsius in readings:
                        if writer:
                            writer.writerow([timestamp, sensor,
                                             f"{celsius:.2f}"])
                        else:
                            print(f"{timestamp}  {sensor}: {celsius:.2f} C")

                if csv_file:
                    csv_file.flush()
                taken += 1
        finally:
            if csv_file:
                csv_file.close()
            if args.cooling_test:
                print(f"saved to {filename}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted, exiting", file=sys.stderr)
        sys.exit(130)
