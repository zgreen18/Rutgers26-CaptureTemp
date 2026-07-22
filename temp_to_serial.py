#!/usr/bin/env python3
"""Forward camera temperatures from a capture_temp.py CSV to an Arduino.

Follows the CSV like `tail -f`, and writes each new reading for the chosen
sensor to the serial port as a bare value terminated by a newline, e.g.
b"46.80\\n". Run capture_temp.py --csv <file> separately; this script never
touches the camera, so either side can be restarted on its own.
"""

import argparse
import csv
import io
import os
import sys
import threading
import time

import serial


def parse_args():
    p = argparse.ArgumentParser(
        description="Tail a capture_temp.py CSV and forward readings "
                    "over serial."
    )
    p.add_argument("--csv", required=True, metavar="FILE",
                   help="CSV file capture_temp.py is appending to")
    p.add_argument("--port", default="/dev/ttyACM0",
                   help="serial port of the Arduino (default: /dev/ttyACM0)")
    p.add_argument("--baud", type=int, default=9600,
                   help="baud rate, must match Serial.begin() in the sketch "
                        "(default: 9600)")
    p.add_argument("--sensor", default="Sensor",
                   help="which sensor column value to forward "
                        "(default: Sensor)")
    p.add_argument("--poll", type=float, default=0.2,
                   help="seconds between checks for new CSV lines "
                        "(default: 0.2)")
    p.add_argument("--echo", action="store_true",
                   help="print lines the Arduino sends back (e.g. its "
                        "'temp=... pwm=...' acknowledgements)")
    return p.parse_args()


def parse_reading(line, sensor):
    """Return the celsius value if this CSV line is for `sensor`, else None."""
    try:
        row = next(csv.reader(io.StringIO(line)))
    except (csv.Error, StopIteration):
        return None
    if len(row) != 3 or row[0] == "timestamp":
        return None
    _, name, celsius = row
    if name != sensor:
        return None
    try:
        return float(celsius)
    except ValueError:
        return None


def last_reading(path, sensor):
    """Most recent value for `sensor` already in the file, or None."""
    value = None
    with open(path) as f:
        for line in f:
            parsed = parse_reading(line, sensor)
            if parsed is not None:
                value = parsed
    return value


def follow(path, poll):
    """Yield complete new lines appended to `path`, forever.

    Reopens the file if it is replaced or truncated (e.g. the log was
    rotated or restarted from scratch).
    """
    f = open(path, "rb")
    f.seek(0, os.SEEK_END)
    try:
        while True:
            pos = f.tell()
            line = f.readline()
            if line.endswith(b"\n"):
                yield line.decode("utf-8", errors="replace")
                continue
            # Nothing new, or a line the writer hasn't finished yet: rewind
            # and retry, reopening if the file was replaced or truncated.
            f.seek(pos)
            try:
                disk = os.stat(path)
                mine = os.fstat(f.fileno())
                replaced = ((disk.st_ino, disk.st_dev)
                            != (mine.st_ino, mine.st_dev))
                truncated = disk.st_size < pos
            except FileNotFoundError:
                replaced, truncated = False, False  # transient; retry
            if replaced or truncated:
                f.close()
                f = open(path, "rb")
            time.sleep(poll)
    finally:
        f.close()


def start_echo(port):
    """Print every line the device sends back, from a daemon thread.

    pyserial is safe for one reader thread alongside one writer thread.
    """
    def run():
        while True:
            try:
                line = port.readline()
            except (serial.SerialException, OSError, TypeError):
                return  # port closed; process is exiting
            text = line.decode("ascii", errors="replace").strip()
            if text:
                print(f"arduino: {text}", file=sys.stderr)

    threading.Thread(target=run, daemon=True).start()


def main():
    args = parse_args()

    if not os.path.exists(args.csv):
        print(f"waiting for {args.csv} to appear "
              "(camera startup can take ~15 s)...", file=sys.stderr)
        while not os.path.exists(args.csv):
            time.sleep(1.0)

    with serial.serial_for_url(args.port, baudrate=args.baud,
                               timeout=1) as port:
        print(f"opened {args.port} at {args.baud} baud", file=sys.stderr)
        # Opening the port resets most Arduinos; give the sketch time to
        # boot so it doesn't miss the first value.
        time.sleep(2.0)

        if args.echo:
            start_echo(port)

        def send(celsius):
            port.write(f"{celsius:.2f}\n".encode("ascii"))
            port.flush()
            print(f"sent {celsius:.2f}", file=sys.stderr)

        current = last_reading(args.csv, args.sensor)
        if current is not None:
            send(current)

        for line in follow(args.csv, args.poll):
            celsius = parse_reading(line, args.sensor)
            if celsius is not None:
                send(celsius)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted, exiting", file=sys.stderr)
        sys.exit(130)
