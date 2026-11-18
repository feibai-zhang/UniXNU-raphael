# Flashing the iOS root (HFS+) to raphael

## Why HFS+ and not the SHNGROOT/APFS image

The kernel this port boots is the **SWEET** platform XNU (XNU 8796) built by
`dipa-studio/ios-sweet`. It does **not** use UniXNU's native userdata path:

| | UniXNU native (upstream docs) | This port (SWEET kernel) |
| --- | --- | --- |
| userdata layout | `SHNGROOT` manifest + disk-image windows | nothing; the partition *is* the filesystem |
| root filesystem | APFS volume inside the `OS` window | **HFS+ volume at the start of the partition** |
| how the root is found | `/chosen/root-matching` UUID in the ADT | GPT partition named `userdata` (boot-arg `ufsroot=`) |
| kernel support | native APFS driver | `bsd/dev/sweet_ufs.c` (patch 0028) + built-in Apple OSS `hfs-627` (patch 0026) |

The kernel binary settles it: `SWEET HFS`, `sweet_ufs`, `sweet_ufs_rootdev`
and `ufsroot0` are all present, while **`SHNGROOT` appears zero times**.

`bsd/dev/sweet_ufs.c` finds one GPT partition by name on the internal UFS
(`ufsroot=`, default `userdata`), exposes it as the BSD disk `ufsroot0` with
512-byte logical sectors over 4 KiB device blocks, and the built-in HFS+ driver
mounts it at `/`. The root volume therefore has to be an **HFS+ volume placed
at the very beginning of the `userdata` partition**.

Consequence: an image built as "SHNGROOT manifest + APFS `OS`/`SystemOS`
windows" (what `unixnu build userdata` produces) is **unreadable** by this
kernel. `scripts/../work/mkuserdata.py` on the workstation was a dead end.

The same is true for the official `wochgn/UniXNU-guacamole` device repository
(oneplus 7 Pro, same SM8150 silicon, already reached the iOS userland): its
`board.toml` has **no `[userdata]` section** and its ADT has **no
`root-matching`** node either. It boots the HFS+ iOS root that was already on
the phone's UFS.

## Where the image comes from

The `ios-root-hfs` workflow (`.github/workflows/ios-root-hfs.yml`) builds it
on `ubuntu-24.04`:

1. download the public iPhone X 16.7.16 IPSW,
2. read `OS.dmg`, `Cryptex1,SystemOS` and `Cryptex1,AppOS` with `apfs-fuse`
   (the IPSW images are APFS with LZBITMAP-compressed files),
3. assemble them into **one 16 GiB case-sensitive journaled HFS+ volume**
   (`iOSRoot`) with `mkfs.hfsplus` and `rsync -aH` (owners, modes, symlinks
   and hard links preserved),
4. apply the upstream bring-up tweaks (`tools/sweet-root-tweaks.py`):
   - `launchd.plist`: `_PanicOnCrash` → `_PanicOnCrasX` (a crashing daemon
     must not panic the system) and the CommCenter jobs removed (no baseband:
     it crash-loops and Setup blocks on its CoreTelephony XPC),
   - `/private/var/containers/{Bundle,Data/System,Shared/SystemGroup,Temp}`,
   - `com.apple.migration`: `LastSystemVersion` so DataMigration does not run
     for an hour on every boot,
   - `com.apple.springboard`: `BKNoWatchdogs` (no scene-create watchdog kills),
   - `com.apple.purplebuddy`: Setup skipped (`SetupDone`, `SetupFinishedAllSteps`,
     `SetupVersion` 11),
   - `VirtualAudio.plugin` disabled and `StartRecorderServer=false`
     (mediaserverd crash loop),
5. seed the activation state (`scripts/seed_activation.py`),
6. `fsck.hfsplus`, convert to Android sparse, publish in 1.9 GB parts.

The published release is `iosroot-16.7.16-<timestamp>` with the parts
`root-part.00` … `root-part.06`.

## Flash it

```bash
# 1. download the parts
gh release download iosroot-16.7.16-20261101-093843 \
    -R JohnLeeeeeee678/UniXNU-raphael -p 'root-part.*' -D hfsroot/

# 2. concatenate and verify
python3 scripts/assemble_hfsroot.py hfsroot/ -o ios16-root.simg

# 3. make it DENSE -- see "Why the image has to be dense" below
python3 scripts/make_dense.py ios16-root.simg \
    -o ios16-root-dense.simg --max-chunk-mib 128 --verify

# 4. split it: the Google fastboot.exe for Windows is 32-bit and mmaps the
#    whole file, so anything above ~2 GB dies with std::bad_alloc
python3 scripts/split_for_abl.py ios16-root-dense.simg -o pieces/ --limit-gib 1.30

# 4b. prove the pieces reproduce the image byte for byte before flashing
python3 scripts/verify_pieces.py ios16-root-dense.simg pieces/

# 4c. and simulate what the phone would end up holding
python3 scripts/simulate_flash.py ios16-root-dense.simg pieces/

# 5. phone into the bootloader, then flash in DESCENDING order
for p in $(ls pieces/p*.simg | sort -r); do fastboot flash userdata "$p"; done
```

Descending order is deliberate: each piece carries a leading `DONT_CARE`
chunk that covers every block before its own range, and the SM8150 ABL
**seeks** over `DONT_CARE` instead of writing zeros. If a bootloader ever
materialised `DONT_CARE` as zeros, descending order would only damage blocks
that have not been written yet.

**This overwrites `userdata`.** Everything on that partition is lost.

## Why the image has to be dense

The ABL treats a `DONT_CARE` chunk as a **seek**, not as a write of zeros.
That is what makes the split flash work -- each piece declares its absolute
start block with a leading `DONT_CARE` chunk -- but it has a consequence: the
blocks a piece skips keep whatever the partition held before.

The partition is not empty. It still holds the previous root build, and the
old image differed from the new one in 310 blocks. Every one of those 310 is
an **allocated** block of the new HFS+ volume, and 47 of them sit inside the
catalog B-tree (extents `(183682, 16384)`, `(219273, 8192)`, `(1137808, 8192)`,
`(1756736, 8192)`). Flashing the sparse image would therefore have left the
phone with a volume that is *not* the volume the build server verified.

`scripts/make_dense.py` removes the question: every block is stored, so the
partition ends up holding exactly the bytes of the image, whatever was there
before. It costs 4.7 GiB of extra transfer and nothing else. The output is
still a normal sparse image in the layout this bootloader expects, so
`split_for_abl.py`, `verify_pieces.py` and `simulate_flash.py` take it
unchanged.

The tell is in `simulate_flash.py`'s output: with the sparse image it reports
`source raw blocks: 2966058, device written blocks: 4194304` (1.2 M blocks
would keep their old bytes); with the dense image both numbers are 4194304.

One trap worth knowing: the byte count in a sparse chunk header is a 32-bit
field, so one chunk can cover at most 1048575 blocks (4 GiB). `split_for_abl.py`
can only cut *between* chunks, so a single oversized chunk produces one
oversized piece -- and a piece above ~2 GiB is exactly what the 32-bit client
cannot send. `make_dense.py --max-chunk-mib` caps the chunk size for that
reason (128 MiB by default).

## Boot

```bash
fastboot boot boot-raphael.img
```

Watch the screen. The kernel panel prints a digest every 20 s:

```
=== SWEET t=60s exec=42 crash=3 sb=1/1 fb=9 sw=16 dn=0 m=1 gui=1 ===
```

- `sw=` — number of framebuffer presents that actually reached the panel. If
  it is non-zero the GUI owns the screen.
- `gui=` — 1 once the panel has yielded to the GUI. This is the honest
  counterpart of `m=`: patch 0109 read `sweet_console_muted` *after* clearing
  it, so `m=` was always 0 and could never say anything. Patch 0111 reads the
  state before overriding it.
- `sb=x/y` — SpringBoard execs / SpringBoard crashes.
- `crash=` — crashing processes, listed underneath.

If `sw=` stays 0 the userland never presented a frame; the panel keeps
painting the digest every 20 s so the numbers stay readable.

Patch 0111 also keeps the digest coming **after** the panel has yielded to
the GUI. Before it, the panel went permanently silent once a real frame
arrived, so a later crash left a frozen picture and no diagnostics at all --
exactly the "the screen stopped moving" symptom.

## Reading the log instead of photographing the screen

The kernel exposes its console over USB as a CDC-ACM serial port
(`USB\Class_02&SubClass_02&Prot_01`, VID `0x1209`, PID `0x0001`). Windows
matches it with its own `usbser.inf`, so **no third-party driver is needed**:

```bash
python3 scripts/console_log.py - ios-console.log
```

The script finds the port by itself, reconnects if the phone reboots, and
writes everything the kernel prints. `scripts/bringup.sh` runs it right after
`fastboot boot`, so the bring-up is one command:

```bash
bash scripts/bringup.sh          # verify -> flash -> boot -> read the log
```

### macOS

No driver and no `usbser`: `AppleUSBACMData` binds the interface and exports
it as `/dev/cu.usbmodem*`, so the same script works unchanged:

```bash
python3 scripts/console_log.py --list        # what is on the bus, and where it would read
python3 scripts/console_log.py - ios-console.log
```

It opens the tty `O_RDWR|O_NONBLOCK`, sets it raw with `CLOCAL|CREAD`, **no
`CRTSCTS`** and **`HUPCL` off** (closing the reader must not drop DTR and make
the phone think the host left), then asserts DTR/RTS explicitly because the
kernel only starts pushing the ring once the host is there. None of the
Windows control-transfer trouble below can happen on a tty -- there is no
`SET_LINE_CODING` to stall.

`--list` deliberately also prints the raw USB devices. A phone that is
plugged in but not in the kernel -- still in Android, still in fastboot, or
blocked by macOS -- shows up as a USB device with **no serial port at all**,
and that is the difference between "wrong cable" and "wrong mode".

### macOS blocks the phone before any of this runs

On Apple Silicon, macOS gates new USB devices behind
**System Settings > Privacy & Security > "Allow accessories to connect"**.
While a device is not approved, it enumerates and is then dropped, and
`log show` says so:

```
AppleUSBHostPort::enumerateDeviceComplete: device functionality blocked by transport restrictions
IOUSBHostDevice::start_block_invoke: device will not be registered for matching
AppleUSBIORequest::complete: ... endpoint 0x00: status 0xe0005000 (pipe stalled)
AppleUSBHostPort::terminateDevice: destroying 0x18d1/4ee7/0414 (...): hardware connection lost
```

`adb devices` and `fastboot devices` stay empty while this repeats, because
the device node is never published. Set the policy to **Always**, re-plug,
and approve the prompt if one appears. The tell that it is a policy problem
and not a cable: the same VID/PID keeps coming back every couple of seconds
without anyone touching the phone.

Chrome and Edge can also seize the interface for WebUSB, which is visible in
`ioreg -r -n <device> -d 3` as `Microsoft Edge` / `Google Chrome` user
clients next to the `ADB Interface`. Quit the browser, then
`adb kill-server && adb devices`.

A phone that answers `unauthorized` is in Android with USB debugging on but
has not been trusted yet: unlock it and accept the "Allow USB debugging"
prompt (tick *always*), then `adb reboot bootloader` for fastboot.

## If the COM port will not open

Windows creates the COM port as soon as it has enumerated the device, but
opening it can still fail with:

```
OSError(22, 'the semaphore timeout period has expired', None, 121)
```

That is the host waiting for the **status stage** of a control transfer.
A control transfer is SETUP, an optional data stage and a status stage, and
the status stage always runs in the direction opposite to the data stage
(USB 2.0 8.5.3).  Windows sends `SET_LINE_CODING` with seven payload bytes
when it opens the port, so the device has to answer with an IN status.

Patch 0112 derives that direction from the data stage.  Its fix leaves no
trace in the binary, so the image cannot be checked for it directly; what
*can* be checked is that the image carries a kernel new enough to report
the panel state at all.  `gui=%d` is the marker for patch 0111:

```bash
python3 scripts/check_boot_image.py boot.img --need 'gui=%d'
```

Note that `strings(1)` does **not** work for this: the kernel sits inside a
FAT payload, so its strings are split across cluster boundaries.  The
script walks the raw bytes instead.

### If it still does not open

The kernel prints every DWC3 event it sees:

```
SWEET USB: event 0x%08x
```

and every control request it receives:

```
SWEET USB: setup %02x %02x %04x %04x len %u
```

`setup 21 20 0000 0000 len 7` is the `SET_LINE_CODING` that starts the
trouble.  The event words right after it show which endpoint the
controller reported for the status phase, which is what patch 0112
changed the code to ignore.

## Kernel boot arguments

From `board.toml` `[payload] kernel_args`:

| Argument | Meaning |
| --- | --- |
| `rd=ufsroot0` | mount the root from `ufsroot0` (the `userdata` GPT partition) |
| `ufsroot=userdata` | optional: which GPT partition to use (default `userdata`) |
| `sweetfb_noconsole=1` | mute the console once the GUI presents (see the note below) |
| `sweetfb_hz=30` | framebuffer present rate |
| `sweetusb=2` | expose the kernel console over USB CDC-ACM |
| `sweetusblog=1` | stream the console to the USB serial port (patch 0101) |
| `ignition_level=1` | look for the shared cache in the cryptexes |

The upstream configuration that reached the iOS 16.7 home screen was
`cpus=6, sweetfb_noconsole=1, sweetfb_hz=30`. This port's `board.toml` keeps
`sweetfb_noconsole=1` and `sweetfb_hz=30` but uses `cpus=8`; the images
booted during bring-up use the local `cpus=6` variant, which is the one
upstream verified.

## Note on `sweetfb_noconsole`

`sweetfb_noconsole=1` makes the kernel mute the console on the **first** swap
(patch 0067). That is what upstream uses, and it hides the kernel text once
the GUI takes the screen.

It also means that if the userland never gets far enough to present a frame
while the console was already muted, the screen stays blank with no
diagnostics on it -- and this port cannot pull the persistent log, because
that needs a recovery, which is out of scope here.

The kernel's panel thread (patch 0103 onwards, enabled by default) solves this
better: it keeps painting a digest every 20 s, and as soon as it sees a
**real** framebuffer present it draws one last digest and then stops painting
so the GUI shows through. So for bring-up, boot **without** `sweetfb_noconsole`:

```
python3 scripts/bootimg_args.py patch boot-raphael.img -sweetfb_noconsole
```

(`bootimg_args.py` / `fat_args.py` edit the `args =` line in
`EFI/UniXNU/unixnu.toml` inside the payload; the kernel, the device tree and
the firmware are carried over byte for byte, so no rebuild is needed.)

`TEST_Y.img` in this repository is exactly that: the same kernel, the same
ADT and the same UEFI as `TEST_X.img`, with that one boot argument removed.

## If the screen shows a wrench, a USB cable and a QR code

That is not a Xiaomi screen. It is the **Mu / Project Aloha `Device Recovery`
UI**: the QR decodes to `aka.ms/devicerecovery`, and on the bus the phone
presents a Microsoft VID (`045e:066b`) with a garbage product string. It means
the UEFI came up but had **nothing to boot**, so BDS fell through to recovery.

Check the firmware volume, not the boot-image plumbing:

```bash
# the FV is LZMA-compressed (GUID EE4E5898-3914-4259-9D6E-DC7BD79403CF)
python3 - SM8150_EFI.fd <<'EOF'
import lzma, struct, sys
d = open(sys.argv[1], 'rb').read()
i = d.find(b'_FVH') - 40
hdr = struct.unpack_from('<H', d, i + 48)[0]
fsize = struct.unpack_from('<Q', d, i + 32)[0]
p = i + hdr
while p < i + fsize - 24:
    size = int.from_bytes(d[p + 20:p + 23], 'little')
    if size == 0:
        break
    s = p + 24
    while s < p + size:
        n = int.from_bytes(d[s:s + 3], 'little')
        if n == 0:
            break
        if d[s + 3] == 2:                       # GUID_DEFINED -> nested FV
            off = struct.unpack_from('<H', d, s + 20)[0]
            fv = lzma.decompress(d[s + off:s + n], format=lzma.FORMAT_ALONE)
            for m in (b'GUACESP', b'DarwinBoot', b'unixnu.toml'):
                print(m.decode(), fv.count(m))
            raise SystemExit(0)
        s += (n + 3) // 4 * 4
    p += (size + 7) // 8 * 8
print('no GUID_DEFINED section found')
EOF
```

All three must be non-zero. If they are all zero the firmware volume has no
UniXNU boot manager in it at all -- which is exactly what happened here: the
vendored `aloha-guacamole.patch` injects the four UniXNU modules into
`Device/oneplus-guacamole/DXE.{inc,dsc.inc}` only, while this board builds
`TARGET_DEVICE = xiaomi-raphael` from Aloha's native device directory.
`scripts/build-firmware.py` now appends them to whichever device is targeted,
so both `build-raphael` and `firmware-only` produce firmware that can boot the
payload.

## Still open

- **Touch**: not wired up yet on raphael.
- **SEP**: absent, so no secure enclave operations.
- **Apple ID**: the root is seeded as Activated
  (`scripts/seed_activation.py` creates the mobileactivationd container the
  upstream bring-up image used and writes `data_ark.plist` into it), but
  without a SEP the device still cannot sign in to an Apple ID.
