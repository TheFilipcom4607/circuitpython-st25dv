"""Run a CircuitPython script on a plugged-in board and capture its console.

Copies a script to CIRCUITPY/code.py and reloads the board, then reads the USB
serial console until a sentinel line appears or the timeout expires. Prints
everything it saw.

The reload is deliberate rather than left to auto-reload. Stopping whatever is
already running means sending Ctrl-C, and dropping into the REPL is exactly
what *disables* auto-reload, so a script copied afterwards would never run:
every test would report a timeout with an empty console, which looks precisely
like dead hardware. Ctrl-D after the copy reloads it.

    pip install pyserial
    python run_on_board.py test_st25dv.py
    python run_on_board.py examples/read_tag.py --timeout 180 --until "done"
    python run_on_board.py --list          # just show what was detected

Exit status: 0 if the sentinel was seen and no "FAIL"/"Traceback" appeared,
1 otherwise. Intended to be driven by an agent, so every failure explains
itself on stdout rather than raising.
"""
import argparse
import glob
import os
import shutil
import sys
import time

DEFAULT_SENTINEL = "passed,"          # the self-test suite's tally line
BAD_MARKERS = ("Traceback (most recent", "  FAIL ")


def find_circuitpy():
    """The CIRCUITPY drive, or None. Covers macOS, Linux and Windows."""
    candidates = []
    candidates += glob.glob("/Volumes/CIRCUITPY*")                 # macOS
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "*"
    candidates += glob.glob("/media/%s/CIRCUITPY*" % user)         # Linux
    candidates += glob.glob("/run/media/%s/CIRCUITPY*" % user)
    candidates += glob.glob("/media/CIRCUITPY*")
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":                       # Windows
        drive = "%s:\\" % letter
        if os.path.exists(os.path.join(drive, "boot_out.txt")):
            candidates.append(drive)
    for path in candidates:
        if os.path.isdir(path):
            return path
    return None


def find_port():
    """The board's serial port, or None."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    ports = list(list_ports.comports())
    for port in ports:                       # Adafruit/RP2040 VID first
        if port.vid in (0x239A, 0x2E8A):
            return port.device
    for port in ports:
        text = "%s %s" % (port.description or "", port.manufacturer or "")
        if "circuitpython" in text.lower() or "board in fs mode" in text.lower():
            return port.device
    for pattern in ("/dev/cu.usbmodem*", "/dev/ttyACM*"):
        found = sorted(glob.glob(pattern))
        if found:
            return found[0]
    return ports[0].device if ports else None


def describe():
    drive, port = find_circuitpy(), find_port()
    print("CIRCUITPY drive : %s" % (drive or "NOT FOUND"))
    print("serial port     : %s" % (port or "NOT FOUND"))
    if drive:
        boot = os.path.join(drive, "boot_out.txt")
        if os.path.exists(boot):
            print("boot_out.txt    : %s"
                  % open(boot).read().strip().replace("\n", " | "))
        lib = os.path.join(drive, "lib")
        if os.path.isdir(lib):
            print("lib/            : %s" % ", ".join(sorted(os.listdir(lib))))
    return drive, port


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("script", nargs="?")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--until", default=DEFAULT_SENTINEL)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--drive")
    parser.add_argument("--port")
    args = parser.parse_args()

    drive = args.drive or find_circuitpy()
    port = args.port or find_port()
    if args.list or not args.script:
        describe()
        return 0
    if not drive:
        print("!! no CIRCUITPY drive found. Is the board plugged in and not "
              "in bootloader (RPI-RP2) mode?")
        return 1
    if not port:
        print("!! no serial port found. Install pyserial, and on Linux make "
              "sure you are in the 'dialout' group.")
        return 1
    if not os.path.exists(os.path.join(drive, "lib", "st25dv.mpy")) \
            and not os.path.exists(os.path.join(drive, "lib", "st25dv.py")):
        print("!! no st25dv in %s/lib -- copy st25dv.mpy there first." % drive)
        return 1

    try:
        import serial
    except ImportError:
        print("!! pyserial is not installed: pip install pyserial")
        return 1

    print("== %s -> %s/code.py (console %s) ==" % (args.script, drive, port))
    try:
        conn = serial.Serial(port, 115200, timeout=0.2)
    except Exception as err:                       # noqa: BLE001
        print("!! could not open %s: %s" % (port, err))
        return 1

    with conn:
        conn.write(b"\x03")                        # Ctrl-C: stop whatever runs
        time.sleep(0.3)
        conn.reset_input_buffer()
        shutil.copyfile(args.script, os.path.join(drive, "code.py"))
        try:                                       # flush to the board now
            os.sync()
        except AttributeError:
            pass
        time.sleep(0.6)                            # let the write land
        conn.write(b"\x04")                        # Ctrl-D: reload. The Ctrl-C
        conn.flush()                               # above dropped us into the
        time.sleep(0.2)                            # REPL, disabling auto-reload

        deadline = time.time() + args.timeout
        buf = b""
        seen_sentinel = False
        while time.time() < deadline:
            chunk = conn.read(4096)
            if not chunk:
                continue
            buf += chunk
            sys.stdout.write(chunk.decode("utf-8", "replace"))
            sys.stdout.flush()
            if args.until and args.until.encode() in buf:
                seen_sentinel = True
                time.sleep(0.4)                    # let the tail arrive
                rest = conn.read(4096)
                if rest:
                    buf += rest
                    sys.stdout.write(rest.decode("utf-8", "replace"))
                break

    text = buf.decode("utf-8", "replace")
    bad = [m for m in BAD_MARKERS if m in text]
    print()
    print("== %s ==" % ("sentinel seen" if seen_sentinel else "TIMED OUT"))
    if bad:
        print("== markers found: %s ==" % ", ".join(repr(b) for b in bad))
    return 0 if (seen_sentinel and not bad) else 1


if __name__ == "__main__":
    sys.exit(main())
