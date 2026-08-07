#!/usr/bin/env python3
"""Grab a single frame from an Allied Vision camera and save it as 16-bit TIFF.

Opens the camera, takes one shot, and releases it. Only one process can own
the camera at a time, so don't run this while monitor.py (or any other
camera script) is running — use monitor.py when temperatures and images
need to be logged together.
"""

import argparse
import sys
from datetime import datetime

from camlib import (capture_image, configure_exposure, open_camera,
                    save_image, setup_pixel_format)


def parse_args():
    p = argparse.ArgumentParser(
        description="Grab one camera frame and save it as a 16-bit TIFF."
    )
    p.add_argument("--camera", default=None,
                   help="camera ID to open (default: first camera that has "
                        "a DeviceTemperature feature)")
    p.add_argument("--out", default=None, metavar="FILE",
                   help="output path (default: image_<timestamp>.tif)")
    p.add_argument("--exposure-us", type=float, default=None, metavar="US",
                   help="fixed exposure time in microseconds "
                        "(default: leave the camera as it is)")
    p.add_argument("--gain-db", type=float, default=None, metavar="DB",
                   help="fixed gain in dB (default: leave the camera as it is)")
    p.add_argument("--warmup", type=int, default=0, metavar="N",
                   help="frames to grab and discard first, e.g. to let "
                        "auto-exposure settle (default: 0)")
    p.add_argument("--timeout-ms", type=int, default=5000, metavar="MS",
                   help="frame grab timeout (default: 5000)")
    return p.parse_args()


def main():
    args = parse_args()
    out = args.out or f"image_{datetime.now():%Y%m%d_%H%M%S}.tif"

    with open_camera(args.camera, require_temperature=False) as cam:
        setup_pixel_format(cam)
        configure_exposure(cam, args.exposure_us, args.gain_db)
        for _ in range(args.warmup):
            capture_image(cam, timeout_ms=args.timeout_ms)
        img = capture_image(cam, timeout_ms=args.timeout_ms)

    save_image(img, out)
    print(f"saved {out} ({img.shape[1]}x{img.shape[0]}, {img.dtype})",
          file=sys.stderr)
    print(out)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("interrupted, exiting", file=sys.stderr)
        sys.exit(130)
