#!/usr/bin/env python3
"""Poll an Allied Vision camera's temperature sensors via VmbPy.

Prints readings to stdout, or appends them to a CSV with --csv.
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
    p.add_argument("--interval", type=float, default=1.0,
                   help="seconds between samples (default: 1.0)")
    p.add_argument("--count", type=int, default=0,
                   help="number of samples to take; 0 = run until Ctrl-C")
    p.add_argument("--csv", default=None, metavar="FILE",
                   help="append samples to this CSV file instead of stdout")
    return p.parse_args()


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
        if args.csv:
            is_new = (not os.path.exists(args.csv)
                      or os.path.getsize(args.csv) == 0)
            csv_file = open(args.csv, "a", newline="")
            writer = csv.writer(csv_file)
            if is_new:
                writer.writerow(["timestamp", "sensor", "celsius"])
                csv_file.flush()

        try:
            taken = 0
            while args.count == 0 or taken < args.count:
                if taken > 0:
                    time.sleep(args.interval)
                timestamp = datetime.now(timezone.utc).isoformat()
                for sensor, celsius in read_temperatures(cam):
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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted, exiting", file=sys.stderr)
        sys.exit(130)
