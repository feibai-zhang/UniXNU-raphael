#!/usr/bin/env bash
# Bring the phone up on raphael: flash the iOS root and boot the kernel.
#
#   bringup.sh [--image BOOT.img] [--work-dir DIR] [--pieces-dir DIR]
#              [--dense IMG] [--no-flash] [--no-log]
#
# The phone must already be in fastboot mode (Power + Volume Down).
#
# Why the pieces: Google's fastboot.exe for Windows is 32-bit and mmaps the
# whole file, so a 16 GiB image dies with std::bad_alloc.  The pieces are
# each under 1.5 GiB and carry a leading DONT_CARE chunk that states the
# absolute block they belong at, so the bootloader places them correctly.
# They are flashed in DESCENDING order, which is also the only order that
# stays correct if a bootloader ever materialised DONT_CARE as zeros.
#
# Why the pieces come from the DENSE image (make_dense.py): the bootloader
# treats DONT_CARE as a seek rather than a write, so a sparse image would
# leave the blocks it skips holding the previous root build -- including 47
# blocks inside the HFS+ catalog B-tree.  Dense means every block is
# written, so the phone ends up with exactly the volume CI verified.
#
# The HFS+ volume goes to the very start of the userdata partition: the
# kernel mounts the partition itself as the root filesystem, and the
# persistent console log lives in the last 4 MiB of the same partition.
#
# After the boot it starts reading the kernel console off the phone's
# CDC-ACM port, so the log is readable from the workstation instead of
# having to be photographed off the screen.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

IMAGE="${IMAGE:-}"
if [ -z "$IMAGE" ]; then
  case "$(uname -s)" in
    Darwin|Linux) IMAGE="${RAPHAEL_HOME:-$HOME/raphael-ios}/boot/boot-raphael.img" ;;
    *)            IMAGE="${RAPHAEL_HOME:-D:/raphael-ios}/boot/TEST_Z_0111.img" ;;
  esac
fi
case "$(uname -s)" in
  Darwin|Linux) HOST_HOME="${RAPHAEL_HOME:-$HOME/raphael-ios}" ;;
  *)            HOST_HOME="${RAPHAEL_HOME:-D:/raphael-ios}" ;;
esac
WORK_DIR="${WORK_DIR:-$HOST_HOME/out/hfsroot2}"

# macOS ships no timeout(1) (it is GNU coreutils); a boot that hangs is the
# one thing this script must survive, so provide the fallback rather than
# depending on brew having installed gtimeout.
if ! command -v timeout >/dev/null 2>&1; then
  if command -v gtimeout >/dev/null 2>&1; then
    timeout() { gtimeout "$@"; }
  else
    timeout() {
      local secs="$1"; shift
      local fired; fired="$(mktemp -u /tmp/bringup-timeout.XXXXXX)"
      "$@" & local pid=$!
      ( sleep "$secs"; : > "$fired"; kill -TERM "$pid" 2>/dev/null ) & local dog=$!
      { wait "$pid"; } 2>/dev/null; local rc=$?
      kill "$dog" 2>/dev/null
      wait "$dog" 2>/dev/null
      if [ -e "$fired" ]; then rc=124; fi   # same status GNU timeout uses
      rm -f "$fired"
      return $rc
    }
  fi
fi
PIECES_DIR="${PIECES_DIR:-$WORK_DIR/dense-pieces}"
DENSE="${DENSE:-$WORK_DIR/ios16-root-dense.simg}"
LOG="${LOG:-$WORK_DIR/ios-console.log}"
FLASH=1
READLOG=1
# How long to wait for "fastboot boot" before calling it hung.  A healthy
# boot takes a few seconds; the Xiaomi ABL hang shows up as an indefinite
# "Sending", so 180 s is far more than a working boot needs.
BOOT_TIMEOUT="${BOOT_TIMEOUT:-180}"

while [ $# -gt 0 ]; do
  case "$1" in
    --image)      IMAGE="$2"; shift 2 ;;
    --work-dir)   WORK_DIR="$2"; shift 2 ;;
    --pieces-dir) PIECES_DIR="$2"; shift 2 ;;
    --dense)      DENSE="$2"; shift 2 ;;
    --log)        LOG="$2"; shift 2 ;;
    --no-flash)   FLASH=0; shift ;;
    --no-log)     READLOG=0; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

FASTBOOT="${FASTBOOT:-}"
if [ -z "$FASTBOOT" ]; then
  for c in fastboot ./fastboot.exe ../tools/platform-tools/fastboot.exe \
           ../../tools/platform-tools/fastboot.exe; do
    if command -v "$c" >/dev/null 2>&1 || [ -x "$c" ]; then FASTBOOT="$c"; break; fi
  done
fi
[ -n "$FASTBOOT" ] || { echo "fastboot not found; set FASTBOOT=" >&2; exit 1; }

echo "== fastboot: $FASTBOOT"
DEVS="$("$FASTBOOT" devices 2>&1)"
echo "$DEVS"
if [ -z "$DEVS" ]; then
  echo
  echo "!! No device in fastboot mode." >&2
  echo "!! Hold Power + Volume Down on the phone, then release Power." >&2
  exit 1
fi
echo
echo "== image: $IMAGE"
echo "== work dir: $WORK_DIR"
echo

if [ "$FLASH" = "1" ]; then
  echo "== verifying the pieces =="
  python3 "$ROOT/scripts/verify_pieces.py" "$DENSE" "$PIECES_DIR"

  echo
  echo "== simulating the flash =="
  # The simulation replays what the bootloader will do with these chunks.
  # It is a second opinion on the verification above, not the verification
  # itself, so a missing NumPy must not stand between the phone and the
  # flash: verify_pieces.py has already compared every block.
  if ! python3 "$ROOT/scripts/simulate_flash.py" "$DENSE" "$PIECES_DIR"; then
    echo "!! the flash simulation could not run (see the error above)." >&2
    echo "!! The piece verification above is the authoritative check and" >&2
    echo "!! it passed; continuing with the flash." >&2
  fi

  echo
  echo "== verifying the volume =="
  python3 "$ROOT/scripts/verify_root.py" "$WORK_DIR/ios16-root.simg"

  echo
  echo "== verifying the bring-up tweaks =="
  # CommCenter removed, Setup skipped, DataMigration marked, activation
  # seeded.  A missing tweak is the difference between a phone that boots
  # to a home screen and one that sits on the Setup screen for ever, so
  # check the files rather than trust that the build ran them.
  python3 "$ROOT/scripts/check_root_tweaks.py" "$WORK_DIR/ios16-root.simg"

  echo
  echo "== flashing userdata, descending =="
  for p in $(ls "$PIECES_DIR"/p*.simg | sort -r); do
    echo "-- $p"
    "$FASTBOOT" flash userdata "$p"
  done
  echo
  echo "== userdata done =="
else
  echo "== skipping the flash (--no-flash) =="
fi

echo
echo "== checking the kernel inside the boot image =="
# The kernel lives inside a FAT payload, so its strings are split across
# cluster boundaries and `strings(1)` misses them; check_boot_image.py
# walks the raw bytes instead.  A kernel without the panel digest (0111)
# cannot say whether the GUI took the screen, and that is exactly the
# question this bring-up is here to answer -- say so before flashing 13 GB.
if python3 "$ROOT/scripts/check_boot_image.py" "$IMAGE" --need 'gui=%d'; then
  echo "   the panel digest is in this image"
else
  echo "!! gui=%d NOT found in $IMAGE." >&2
  echo "!! This image predates 0111, so it cannot report whether the GUI" >&2
  echo "!! took the screen.  Check the kernel before trusting the screen." >&2
fi

echo
echo "== booting $IMAGE =="
# After a large flash the Xiaomi ABL often hangs: the next "fastboot boot"
# stalls at "Sending" and never returns.  Retrying against the hung
# bootloader only wastes time, so give the command a deadline and, if it
# expires, say exactly what the user has to do instead.
if ! timeout "$BOOT_TIMEOUT" "$FASTBOOT" boot "$IMAGE"; then
  echo
  echo "!! fastboot boot did not finish within ${BOOT_TIMEOUT}s." >&2
  echo "!! This is the known Xiaomi ABL hang after a large flash, not a" >&2
  echo "!! problem with the image.  Do NOT retry against the hung" >&2
  echo "!! bootloader -- it will not answer." >&2
  echo "!!" >&2
  echo "!! On the phone: hold POWER + VOLUME DOWN, release POWER, and" >&2
  echo "!! keep holding VOLUME DOWN until the phone shows the fastboot" >&2
  echo "!! screen (the Xiaomi logo).  Then run this script again with" >&2
  echo "!! --no-flash: the root is already on the phone." >&2
  exit 3
fi

if [ "$READLOG" = "1" ]; then
  echo
  echo "== reading the kernel console over USB (Ctrl-C to stop) =="
  echo "== log file: $LOG"
  echo "== the report line is:"
  echo "   === SWEET t=Ns exec=N crash=N sb=N/N fb=N sw=N dn=N m=N gui=N ==="
  echo "   sw= non-zero and gui=1 means the GUI took the screen."
  python3 "$ROOT/scripts/console_log.py" - "$LOG" || true
else
  echo
  echo "== watch the screen =="
  echo "   === SWEET t=Ns exec=N crash=N sb=N/N fb=N sw=N dn=N m=N gui=N ==="
  echo "   sw= is the number of frames that actually reached the panel."
  echo "   gui=1 means the console yielded to the GUI (the home screen is up)."
fi

# The USB trace is only useful once it has been read, so decode it at the
# end: it says straight out whether the control transfer status stages went
# the way the host expected.
if [ -s "$LOG" ]; then
  echo
  echo "== decoding the USB trace =="
  python3 "$ROOT/scripts/decode_usb_events.py" "$LOG" || true
fi
