#!/usr/bin/env python3
"""Log camera temperatures and frames together over a timed run.

Opens the camera once (this process is the single owner) and, on every
interval, reads all temperature sensors and optionally grabs a frame,
keeping timestamps, image filenames, and temperatures together:

    <runs-dir>/<name>_<timestamp>/
        data.csv                timestamp,seconds_elapsed,filename,<sensors>
        frames/frame_0000.tif   16-bit TIFFs

--live-csv additionally appends each reading to the rolling 3-column CSV
that temp_to_serial.py tails, so the fan keeps running while a run logs.
Frames are ~2.6 MB each; use --frame-every to image less often than you
sample temperatures (e.g. --interval 5 --frame-every 6 keeps the fan feed
fresh while saving a frame every 30 s).
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone

from camlib import (capture_image, configure_exposure, open_camera,
                    open_live_csv, read_temperatures, save_image,
                    setup_pixel_format)

# Defaults match capture_temp.py --cooling-test: a sample every 30 s for
# 10 minutes.
DEFAULT_INTERVAL = 30.0
DEFAULT_COUNT = 21


def parse_args():
    p = argparse.ArgumentParser(
        description="Log camera temperatures and frames to a per-run "
                    "directory."
    )
    p.add_argument("--name", default=None, metavar="NAME",
                   help="run name (default: ask for one)")
    p.add_argument("--camera", default=None,
                   help="camera ID to open (default: first camera that has "
                        "a DeviceTemperature feature)")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                   help="seconds between samples "
                        f"(default: {DEFAULT_INTERVAL:g})")
    p.add_argument("--count", type=int, default=DEFAULT_COUNT,
                   help="number of samples to take; 0 = run until Ctrl-C "
                        f"(default: {DEFAULT_COUNT})")
    p.add_argument("--runs-dir", default="runs", metavar="DIR",
                   help="directory to create the run under (default: runs)")
    p.add_argument("--frame-every", type=int, default=1, metavar="K",
                   help="grab a frame every K samples; 0 = temperatures "
                        "only (default: 1)")
    p.add_argument("--exposure-us", type=float, default=None, metavar="US",
                   help="fixed exposure time in microseconds "
                        "(default: leave the camera as it is)")
    p.add_argument("--gain-db", type=float, default=None, metavar="DB",
                   help="fixed gain in dB (default: leave the camera as it is)")
    p.add_argument("--warmup", type=int, default=0, metavar="N",
                   help="frames to grab and discard before the run "
                        "(default: 0)")
    p.add_argument("--live-csv", default=None, metavar="FILE",
                   help="also append readings to this rolling "
                        "timestamp,sensor,celsius CSV for temp_to_serial.py")

    args = p.parse_args()
    if args.frame_every < 0:
        p.error("--frame-every must be 0 or more")
    return args


def run_dirname(name):
    """Build <name>_<stamp>, asking for the name if not given.

    Spaces become underscores so the directory stays tidy, and the
    timestamp keeps each run next to previous ones.
    """
    if name is None:
        name = input("Name for this run (e.g. carbon_fiber): ")
    name = name.strip().replace(" ", "_") or "unnamed"
    return f"{name}_{datetime.now():%Y%m%d_%H%M%S}"


def main():
    args = parse_args()

    # Settle the run name before touching the camera, so waiting at the
    # prompt does not hold a camera open and Ctrl-C there costs nothing.
    run_dir = os.path.join(args.runs_dir, run_dirname(args.name))

    with open_camera(args.camera) as cam:
        if args.frame_every:
            setup_pixel_format(cam)
            configure_exposure(cam, args.exposure_us, args.gain_db)
            for _ in range(args.warmup):
                capture_image(cam)
            os.makedirs(os.path.join(run_dir, "frames"))
        else:
            os.makedirs(run_dir)

        data_file = open(os.path.join(run_dir, "data.csv"), "w", newline="")
        data_writer = csv.writer(data_file)

        live_file = None
        live_writer = None
        if args.live_csv:
            live_file, live_writer = open_live_csv(args.live_csv)

        how_many = (f"{args.count} sample{'s' * (args.count != 1)}"
                    if args.count else "samples")
        print(f"logging {how_many} to {run_dir}", file=sys.stderr)
        print("press Ctrl-C to stop early", file=sys.stderr)

        # Each sensor gets its own column, but which sensors exist is only
        # known once the first sample has been read.
        columns = None
        try:
            taken = 0
            frames = 0
            start = time.monotonic()
            while args.count == 0 or taken < args.count:
                # Aim at a fixed offset from the start rather than sleeping a
                # whole interval after each sample, so time spent reading and
                # saving frames does not accumulate into drift.
                wait = taken * args.interval - (time.monotonic() - start)
                if wait > 0:
                    time.sleep(wait)

                timestamp = datetime.now(timezone.utc).isoformat()
                elapsed = time.monotonic() - start
                # Temperatures first: they drive the fan, so their cadence
                # matters more than the frame's.
                readings = read_temperatures(cam)

                filename = ""
                if args.frame_every and taken % args.frame_every == 0:
                    filename = os.path.join("frames",
                                            f"frame_{frames:04d}.tif")
                    save_image(capture_image(cam),
                               os.path.join(run_dir, filename))
                    frames += 1

                if columns is None:
                    columns = [sensor for sensor, _ in readings]
                    data_writer.writerow(
                        ["timestamp", "seconds_elapsed", "filename", *columns])
                # Look sensors up by name, so one dropping out mid-run
                # leaves a gap instead of shifting every later column.
                values = dict(readings)
                data_writer.writerow(
                    [timestamp, f"{elapsed:.3f}", filename]
                    + [f"{values[c]:.2f}" if c in values else ""
                       for c in columns])
                data_file.flush()

                if live_writer:
                    for sensor, celsius in readings:
                        live_writer.writerow([timestamp, sensor,
                                              f"{celsius:.2f}"])
                    live_file.flush()

                now = datetime.now().strftime("%H:%M:%S")
                shown = "  ".join(f"{sensor} {celsius:.2f} C"
                                  for sensor, celsius in readings)
                tag = f"  -> {filename}" if filename else ""
                print(f"{now}  {elapsed:6.1f}s  {shown}{tag}")
                taken += 1
        finally:
            data_file.close()
            if live_file:
                live_file.close()
            print(f"saved to {run_dir}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted, exiting", file=sys.stderr)
        sys.exit(130)
