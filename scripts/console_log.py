#!/usr/bin/env python3
"""Read the iOS console straight off the phone's CDC-ACM port.

The kernel presents itself as a CDC-ACM device (VID 0x1209, PID 0x0001, the
pid.codes test pair) and pushes the console ring out of bulk IN as a plain
byte stream, so whatever the serial port delivers is exactly the text the
screen shows.

    python3 console_log.py                 # auto-detect the port, write ios-console.log
    python3 console_log.py --list          # show the candidate ports and exit
    python3 console_log.py - out.log       # auto-detect, write out.log
    python3 console_log.py /dev/cu.usbmodem14301 out.log   # use a specific port
    python3 console_log.py --pyserial      # use pyserial instead of the raw reader

The script survives the device disappearing (reboot, fastboot, the kernel
re-enumerating) and keeps retrying, which is what makes it usable as "start
it, then boot the phone".

Windows
-------
Windows binds usbser.sys to the comm interface from the box -- its usbser.inf
matches "USB\\Class_02&SubClass_02&Prot_01", which is what patch 0101
describes the comm interface as -- so no extra driver is needed.

Why this does not simply use pyserial
-------------------------------------
Opening a COM port through pyserial means SetCommState, SetCommTimeouts and
friends.  On this phone those hang.  Measured here:

    pyserial.Serial("COM5", 115200)        ->  OSError(22) "the semaphore timeout
                                               period has expired" (Win32 121)
    CreateFileW("\\\\.\\COM5") + ReadFile  ->  works

Windows sends three class requests when it opens a COM port -- SET_LINE_CODING,
GET_LINE_CODING, SET_CONTROL_LINE_STATE -- and the kernel's control-transfer
state machine does not complete the status stage of the first one, so the host
waits for a reply that never arrives.

The bulk IN path is a different endpoint and is unaffected, which is why the
raw reader still gets the log.  So the default path here opens the port and
reads it without touching any of the line settings.  pyserial is only a
fallback (--pyserial) for a kernel that does answer.

The read uses overlapped I/O with a real event, so a read that times out is
cancelled instead of leaving a pending operation on the handle -- otherwise
the second read on the same handle would fail.

macOS
-----
There is no line state to set on a tty, so the "Windows stalls on
SET_LINE_CODING" problem simply does not exist: AppleUSBACMData has already
bound the interface and exported it as /dev/cu.usbmodem*.  The port is opened
O_RDWR|O_NONBLOCK and put in raw mode with CLOCAL|CREAD, no CRTSCTS and with
HUPCL **off** (so closing the log reader does not drop DTR and make the phone
think the host went away).  DTR/RTS are then asserted explicitly, because the
kernel's CDC-ACM only starts pushing the console ring once the host has
asserted DTR.

os.read on a hung-up tty returns 0 or raises EIO/ENXIO rather than blocking,
so both are treated as "device disappeared" and the reconnect loop takes over.

`ioreg` is used to list candidates with their USB identity so --list can show
what is there and auto-detection can prefer VID/PID 0x1209/0x0001 over "the
only usbmodem port on the machine".

Linux
-----
Same code path as macOS; the port is /dev/ttyACM*.
"""
import glob
import os
import platform
import plistlib
import select
import subprocess
import sys
import time

VID = 0x1209          # pid.codes test VID the kernel presents
PID = 0x0001

OUT = "ios-console.log"
USE_PYSERIAL = "--pyserial" in sys.argv
LIST_ONLY = "--list" in sys.argv

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    GENERIC_READ = 0x80000000
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    OPEN_EXISTING = 3
    FILE_FLAG_OVERLAPPED = 0x40000000
    FILE_ATTRIBUTE_NORMAL = 0x00000080
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    ERROR_IO_PENDING = 997
    ERROR_OPERATION_ABORTED = 995
    WAIT_OBJECT_0 = 0
    WAIT_TIMEOUT = 258
    INFINITE = 0xFFFFFFFF

    class OVERLAPPED(ctypes.Structure):
        """Windows OVERLAPPED; ctypes.wintypes does not define it."""

        _fields_ = [
            ("Internal", ctypes.c_ulong),
            ("InternalHigh", ctypes.c_ulong),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", ctypes.c_void_p),
        ]


# --------------------------------------------------------------------------
# port discovery
# --------------------------------------------------------------------------

def _pyserial_ports():
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return list(list_ports.comports())


def find_ports_win():
    """[{'device','vid','pid','desc'}] for every serial port Windows has."""
    out = []
    for p in _pyserial_ports():
        out.append({"device": p.device, "vid": p.vid, "pid": p.pid,
                    "desc": p.description or ""})
    return out


def find_ports_posix():
    """[{'device','vid','pid','desc'}] for the USB serial ports on this host.

    ioreg gives us the USB identity of each controller-side node; the tty
    name is not linked to it in the plist, so the match is done the way a
    human would: the USB serial number / product name usually appears in the
    /dev/cu.usbmodem* name, otherwise there is only one such port anyway.
    """
    devs = []
    if IS_MAC:
        devs = _mac_usb_devices()
        cands = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/cu.usbserial*"))
    elif IS_LINUX:
        cands = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    else:
        cands = []

    out = []
    for dev in cands:
        out.append({"device": dev, "vid": None, "pid": None, "desc": ""})

    # Attach the USB identity where the names let us.
    if devs:
        unused = list(devs)
        for rec in out:
            base = os.path.basename(rec["device"]).lower()
            for d in list(unused):
                keys = [k for k in (d.get("serial"), d.get("name"), d.get("loc"))
                        if k]
                if any(str(k).lower().replace("-", "").replace("0x", "") in base
                       for k in keys):
                    rec.update(vid=d.get("vid"), pid=d.get("pid"),
                               desc=d.get("name") or "")
                    unused.remove(d)
                    break
        # Exactly one unknown port and exactly one leftover device: pair them.
        unknown = [r for r in out if r["vid"] is None]
        if len(unknown) == 1 and len(unused) == 1:
            d = unused[0]
            unknown[0].update(vid=d.get("vid"), pid=d.get("pid"),
                              desc=d.get("name") or "")
    return out


def _mac_usb_devices():
    """USB devices macOS has published, with their VID/PID and identity."""
    try:
        raw = subprocess.run(["ioreg", "-a", "-l", "-p", "IOUSB", "-w0"],
                             capture_output=True, timeout=20).stdout
        tree = plistlib.loads(raw)
    except Exception:
        return []

    found = []
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        vid = node.get("idVendor")
        pid = node.get("idProduct")
        if isinstance(vid, int) and isinstance(pid, int):
            found.append({
                "vid": vid, "pid": pid,
                "name": node.get("USB Product Name") or "",
                "serial": node.get("USB Serial Number") or "",
                "loc": ("%08x" % node["locationID"]
                        if isinstance(node.get("locationID"), int) else ""),
            })
        stack.extend(node.values())
    return found


def describe(rec):
    bits = [rec["device"]]
    if rec.get("vid") is not None:
        bits.append("%04x:%04x" % (rec["vid"], rec["pid"]))
    else:
        bits.append("vid/pid unknown")
    if rec.get("desc"):
        bits.append(rec["desc"])
    return "  " + "  ".join(bits)


def is_phone(rec):
    return (rec.get("vid"), rec.get("pid")) == (VID, PID)


def find_port():
    """The tty of the phone, by USB VID/PID, or the only USB serial port."""
    ports = find_ports_posix() if (IS_MAC or IS_LINUX) else find_ports_win()
    for p in ports:
        if is_phone(p):
            return p["device"]
    if IS_WIN:
        cands = [p["device"] for p in ports
                 if "USB 串行" in (p["desc"] or "")
                 or "USB Serial" in (p["desc"] or "")]
        return cands[0] if cands else None
    # macOS/Linux: /dev/cu.usbmodem* and /dev/ttyACM* are already only USB.
    cands = [p["device"] for p in ports]
    return cands[0] if len(cands) == 1 else None


# --------------------------------------------------------------------------
# readers
# --------------------------------------------------------------------------

if IS_WIN:
    class RawPort:
        """A COM port read through Win32, without configuring the line."""

        def __init__(self, name):
            self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32 = self.k32

            k32.CreateFileW.restype = ctypes.c_void_p
            k32.CreateFileW.argtypes = [
                ctypes.c_wchar_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
            k32.ReadFile.restype = wintypes.BOOL
            k32.ReadFile.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(OVERLAPPED)]
            k32.GetOverlappedResult.restype = wintypes.BOOL
            k32.GetOverlappedResult.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(OVERLAPPED),
                ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
            k32.CreateEventW.restype = ctypes.c_void_p
            k32.CreateEventW.argtypes = [
                ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, ctypes.c_wchar_p]
            k32.SetEvent.restype = wintypes.BOOL
            k32.SetEvent.argtypes = [ctypes.c_void_p]
            k32.ResetEvent.restype = wintypes.BOOL
            k32.ResetEvent.argtypes = [ctypes.c_void_p]
            k32.WaitForSingleObject.restype = wintypes.DWORD
            k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
            k32.CancelIo.restype = wintypes.BOOL
            k32.CancelIo.argtypes = [ctypes.c_void_p]
            k32.CloseHandle.restype = wintypes.BOOL
            k32.CloseHandle.argtypes = [ctypes.c_void_p]

            self.name = name
            self.buf = ctypes.create_string_buffer(65536)
            self.nread = wintypes.DWORD()
            self.ov = OVERLAPPED()

            h = k32.CreateFileW(
                "\\\\.\\" + name,
                GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_OVERLAPPED | FILE_ATTRIBUTE_NORMAL,
                None)
            if h is None or h == INVALID_HANDLE_VALUE:
                raise OSError(ctypes.get_last_error(),
                              "CreateFileW(\\\\.\\%s) failed" % name)
            self.h = h
            self.ev = k32.CreateEventW(None, True, False, None)
            if not self.ev:
                k32.CloseHandle(self.h)
                raise OSError(ctypes.get_last_error(), "CreateEventW failed")

        def read(self, timeout=0.5):
            """Read whatever is waiting; returns b"" on timeout."""
            k32 = self.k32
            k32.ResetEvent(self.ev)
            self.nread.value = 0
            ctypes.memset(ctypes.byref(self.ov), 0, ctypes.sizeof(self.ov))
            self.ov.hEvent = self.ev

            ok = k32.ReadFile(self.h, self.buf, 65536,
                              ctypes.byref(self.nread), ctypes.byref(self.ov))
            if not ok:
                err = ctypes.get_last_error()
                if err == ERROR_IO_PENDING:
                    r = k32.WaitForSingleObject(self.ev, int(timeout * 1000))
                    if r == WAIT_TIMEOUT:
                        # Cancel, then reap the completion so the handle stays
                        # usable for the next read.
                        k32.CancelIo(self.h)
                        k32.WaitForSingleObject(self.ev, INFINITE)
                        k32.GetOverlappedResult(self.h, ctypes.byref(self.ov),
                                                ctypes.byref(self.nread), False)
                        return b""
                elif err in (ERROR_OPERATION_ABORTED, 1167, 6):
                    raise OSError(err, "the device disappeared")
                else:
                    raise OSError(err, "ReadFile failed")
            n = self.nread.value
            if not n:
                return b""
            return self.buf.raw[:n]

        def close(self):
            try:
                self.k32.CloseHandle(self.ev)
            except Exception:
                pass
            try:
                self.k32.CloseHandle(self.h)
            except Exception:
                pass


class PosixPort:
    """A /dev/cu.* (macOS) or /dev/ttyACM* (Linux) port in raw mode.

    The tty has already been bound by the USB serial driver, so there is no
    control request to send and nothing that can stall: the whole class is
    termios plus select() plus read().
    """

    def __init__(self, path):
        import termios

        self.path = path
        self.termios = termios
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)

        try:
            attrs = termios.tcgetattr(self.fd)
            iflag, oflag, cflag, lflag, _, _, cc = attrs

            def clr(value, *names):
                for n in names:
                    value &= ~getattr(termios, n, 0)
                return value

            iflag = clr(iflag, "IXON", "IXOFF", "IXANY", "ICRNL", "INLCR",
                        "IGNCR", "ISTRIP", "INPCK", "BRKINT", "PARMRK")
            oflag = clr(oflag, "OPOST", "ONLCR")
            lflag = clr(lflag, "ECHO", "ECHONL", "ICANON", "ISIG", "IEXTEN")
            # HUPCL off: closing the log must not hang up the phone's CDC-ACM.
            # CRTSCTS off: the kernel does not do flow control, and a stuck
            # RTS would silently stall the ring.
            cflag = clr(cflag, "CSIZE", "PARENB", "CSTOPB", "CRTSCTS", "HUPCL")
            cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
            cc[termios.VMIN] = 0
            cc[termios.VTIME] = 0
            speed = getattr(termios, "B115200", 115200)
            termios.tcsetattr(self.fd, termios.TCSANOW,
                              [iflag, oflag, cflag, lflag, speed, speed, cc])

            # Assert DTR/RTS: the kernel only starts pushing the console ring
            # once the host says it is there.
            try:
                termios.tcsetattr  # noqa: B018  (keep the module reference alive)
                import fcntl
                bits = (getattr(termios, "TIOCM_DTR", 0)
                        | getattr(termios, "TIOCM_RTS", 0))
                if bits:
                    fcntl.ioctl(self.fd, termios.TIOCMBIS, bits.to_bytes(4, "little"))
            except Exception:
                pass
        except Exception:
            os.close(self.fd)
            raise

    def read(self, timeout=0.5):
        """Read whatever is waiting; returns b"" on timeout."""
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return b""
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return b""
        except OSError as e:
            # EIO on a hung-up tty is how macOS reports "device gone".
            raise OSError(e.errno, "the device disappeared")
        if not data:
            # 0 bytes on a non-blocking tty that selected readable = HUP.
            raise OSError(5, "the device disappeared")
        return data

    def close(self):
        try:
            os.close(self.fd)
        except Exception:
            pass


def open_port(name):
    if USE_PYSERIAL:
        import serial
        return serial.Serial(name, 115200, timeout=0.5)
    if IS_WIN:
        return RawPort(name)
    return PosixPort(name)


# --------------------------------------------------------------------------

def main():
    if LIST_ONLY:
        ports = find_ports_posix() if (IS_MAC or IS_LINUX) else find_ports_win()
        print("%d serial port(s):" % len(ports))
        for p in ports:
            print(describe(p) + ("   <== the phone" if is_phone(p) else ""))

        # A phone that is plugged in but not in the kernel (
        # still booting, still in fastboot, still in Android) shows up as a
        # USB device with no tty at all.  Saying so here saves an hour of
        # "the cable must be broken" -- it happened during this bring-up.
        if IS_MAC:
            usbs = _mac_usb_devices()
            print("\n%d USB device(s) on the bus:" % len(usbs))
            for d in usbs:
                print("  %04x:%04x  %-34s serial=%s loc=%s"
                      % (d["vid"], d["pid"], d["name"] or "?",
                         d["serial"] or "-", d["loc"] or "-"))
            if usbs and not ports:
                print("\n  note: a device is on the bus but exposed no serial port.")
                print("        fastboot (18d1:4ee7) and Android/adb (18d1:4ee7)")
                print("        both look like this; the iOS kernel is 1209:0001.")
                print("        Also check System Settings > Privacy & Security >")
                print("        'Allow accessories to connect' if the device was")
                print("        blocked by 'transport restrictions' in the log.")

        pick = find_port()
        print("\nauto-detect would use: %s" % (pick or "nothing (plug the phone in)"))
        return 0

    # positional: [port] [outfile]
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    want = None
    out_path = OUT
    if len(args) >= 1 and args[0] != "-":
        want = args[0]
    if len(args) >= 2:
        out_path = args[1]
    out = open(out_path, "ab", buffering=0)
    if USE_PYSERIAL:
        mode = "pyserial"
    elif IS_WIN:
        mode = "raw win32 (no line config)"
    else:
        mode = "raw tty (%s, no line config)" % platform.system().lower()
    print("writing to %s (Ctrl-C to stop), mode: %s"
          % (os.path.abspath(out_path), mode), flush=True)
    total = 0
    while True:
        port = want or find_port()
        if not port:
            time.sleep(1.0)
            continue
        try:
            ser = open_port(port)
        except Exception as e:
            print("open %s failed: %s" % (port, e), flush=True)
            time.sleep(1.0)
            continue
        print("=== %s open ===" % port, flush=True)
        try:
            while True:
                buf = ser.read(4096)
                if buf:
                    out.write(buf)
                    total += len(buf)
                    sys.stdout.write(buf.decode("utf-8", "replace"))
                    sys.stdout.flush()
        except KeyboardInterrupt:
            try:
                ser.close()
            except Exception:
                pass
            out.close()
            print("\n[stopped, %d bytes]" % total, flush=True)
            return 0
        except Exception as e:
            print("\n=== lost %s: %s (%d bytes so far) ===" % (port, e, total),
                  flush=True)
            try:
                ser.close()
            except Exception:
                pass
            time.sleep(1.0)


if __name__ == "__main__":
    sys.exit(main())
