#!/usr/bin/env python3
"""Poll an Allied Vision camera's temperature sensors via VmbPy.

Prints readings to stdout, or appends them to a CSV with --csv.

--cooling-test instead logs a named run to <test_name>_<timestamp>.csv, with
one column per sensor, for comparing mounts and heatsinking setups.
"""

import argparse
import csv
import sys
import time
from datetime import datetime, timezone

from camlib import open_camera, open_live_csv, read_temperatures

# --cooling-test defaults: a reading every 30 s for 10 minutes, matching the
# runs in camera-cooling-tests/run_cooling_test.py.
COOLING_INTERVAL = 30.0
COOLING_MINUTES = 10
COOLING_COUNT = int(COOLING_MINUTES * 60 / COOLING_INTERVAL) + 1


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


def main():
    args = parse_args()

    # Settle the filename before touching the camera, so waiting at the prompt
    # does not hold a camera open and Ctrl-C there costs nothing.
    filename = cooling_filename(args.name) if args.cooling_test else None

    with open_camera(args.camera) as cam:
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
            csv_file, writer = open_live_csv(args.csv)

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
