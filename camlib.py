"""Shared camera access for the fan-cooling scripts.

Two rules keep scripts from fighting over the camera:

1. One owner per process. VmbPy hands a camera exclusively to whichever
   process opens it, so exactly one script may hold open_camera() at a
   time. Everything else (e.g. temp_to_serial.py feeding the Arduino)
   must consume that owner's CSV output instead of opening the camera.

2. Sequential access within the owner. read_temperatures() mutates
   DeviceTemperatureSelector, so temperature reads and frame grabs must
   happen one after another on the same handle, never concurrently.
"""

import contextlib
import csv
import os
import signal
import sys
import time

import numpy as np
import tifffile
from vmbpy import PixelFormat, VmbCameraError, VmbFeatureError, VmbSystem


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


def _find_camera(vmb, camera_id, discovery_timeout):
    """Return the camera to open.

    With an explicit ID, open that camera. Otherwise pick the first camera
    that actually exposes DeviceTemperature — enumeration can also contain
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


@contextlib.contextmanager
def open_camera(camera_id=None, discovery_timeout=15.0,
                require_temperature=True):
    """Bring up VmbPy and yield an open camera handle.

    The process holding this context owns the camera exclusively; scripts
    that need both temperatures and frames must share one open_camera()
    block rather than each opening the camera themselves.

    require_temperature=False skips the DeviceTemperature check on the
    opened camera, for image-only use (e.g. against the SDK simulator,
    which has no temperature feature — note discovery without a camera_id
    still selects by that feature).
    """
    with contextlib.ExitStack() as stack:
        with defer_sigint():
            vmb = stack.enter_context(VmbSystem.get_instance())
        cam = _find_camera(vmb, camera_id, discovery_timeout)
        with defer_sigint():
            try:
                stack.enter_context(cam)
            except VmbCameraError as e:
                sys.exit(f"error: could not open camera '{cam.get_id()}' "
                         f"(in use by another process?): {e}")

        if require_temperature:
            try:
                cam.get_feature_by_name("DeviceTemperature")
            except VmbFeatureError:
                sys.exit(f"error: camera '{cam.get_id()}' has no "
                         "DeviceTemperature feature")

        print(f"opened camera: {cam.get_id()} ({cam.get_name()})",
              file=sys.stderr)
        yield cam


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


def setup_pixel_format(cam):
    """Switch the camera to Mono12, or Mono16 if Mono12 is unavailable.

    One-time setup after open_camera(), before the first grab; mutates
    camera state.
    """
    for fmt in (PixelFormat.Mono12, PixelFormat.Mono16):
        try:
            cam.set_pixel_format(fmt)
            return fmt
        except ValueError:
            continue
    sys.exit(f"error: camera '{cam.get_id()}' supports neither Mono12 "
             "nor Mono16")


def configure_exposure(cam, exposure_us=None, gain_db=None):
    """Fix exposure and/or gain, turning the matching auto mode off first.

    Auto modes silently override fixed settings, so each has to be switched
    off before its value sticks. Values left as None keep whatever the
    camera is currently doing; a feature the camera lacks is a warning,
    not an error.
    """
    settings = (("ExposureAuto", "ExposureTime", exposure_us),
                ("GainAuto", "Gain", gain_db))
    for auto_name, name, value in settings:
        if value is None:
            continue
        try:
            with contextlib.suppress(VmbFeatureError):
                cam.get_feature_by_name(auto_name).set("Off")
            cam.get_feature_by_name(name).set(value)
        except (VmbCameraError, VmbFeatureError):
            print(f"warning: could not set {name} on '{cam.get_id()}'",
                  file=sys.stderr)


def capture_image(cam, timeout_ms=5000):
    """Grab one frame and return it as a 2-D uint16 array.

    Call only from the process that owns the handle, and never while a
    temperature read is in progress on the same camera.
    """
    frame = cam.get_frame(timeout_ms=timeout_ms)
    return frame.as_numpy_ndarray().squeeze().astype(np.uint16)


def save_image(img, path):
    """Write a frame as 16-bit grayscale TIFF, the project's image format."""
    tifffile.imwrite(path, img)


def open_live_csv(path):
    """Open the rolling timestamp,sensor,celsius CSV for appending.

    Writes the header only when the file is new or empty, so restarts keep
    appending to the same feed that temp_to_serial.py tails. Returns the
    open file and a csv writer; the caller closes the file.
    """
    is_new = not os.path.exists(path) or os.path.getsize(path) == 0
    csv_file = open(path, "a", newline="")
    writer = csv.writer(csv_file)
    if is_new:
        writer.writerow(["timestamp", "sensor", "celsius"])
        csv_file.flush()
    return csv_file, writer
