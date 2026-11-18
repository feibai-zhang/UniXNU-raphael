#!/usr/bin/env python3
"""Build the Aloha NOSB firmware volume with the UniXNU/DarwinBoot INFs for raphael.

The Project Aloha tree already ships a first-class ``xiaomi-raphael`` device
directory (Device/xiaomi-raphael with its own DeviceTreeBlob), so unlike
guacamole we do NOT have to clone another vendor's device dir. The DSC selects
it purely via ``TARGET_DEVICE=xiaomi-raphael`` and PlatformBuildNoSb derives
``FDT=raphael.dtb`` from it, both of which board.toml already sets.

The aloha-guacamole.patch creates ``Resources/Configs/guacamole.toml``; we
mirror it to ``raphael.toml`` because board.toml sets ``[firmware] config``
to "raphael".
"""
import os
import shutil
import sys
import tomllib
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "unixnu" / "scripts"))
sys.path.insert(0, str(ROOT / "scripts"))

from unixnu import firmware  # noqa: E402
from tree import assemble  # noqa: E402

UEFI = ROOT / "uefi"
EDK2 = UEFI / "MU_BASECORE"
WORK = ROOT / "out" / "raphael"
TREE = assemble(WORK, ROOT / "unixnu", ROOT)
LOGS = WORK / "logs"
LOGS.mkdir(parents=True, exist_ok=True)

DEVICE_ROOT = UEFI / "Platforms" / "SurfaceDuo1Pkg" / "Device"
DST = DEVICE_ROOT / "xiaomi-raphael"
SRC = DEVICE_ROOT / "oneplus-guacamole"

# xiaomi-raphael is shipped by the Aloha tree; only synthesise it as a fallback
# (same SM8150 silicon) if a future revision drops it.
if not DST.is_dir():
    if not SRC.is_dir():
        raise SystemExit(
            f"firmware: neither {DST} nor {SRC} exists in the Aloha tree; "
            "cannot build the raphael device."
        )
    print(f"firmware: {DST.name} missing; cloning {SRC.name} (same SM8150 silicon)")
    shutil.copytree(SRC, DST)
    for old, new in (("guacamole.dtb", "raphael.dtb"), ("guacamole.toml", "raphael.toml")):
        p = DST / old
        if p.is_file():
            p.rename(DST / new)
else:
    print(f"firmware: using native Aloha device dir {DST.name}")

# The UniXNU firmware driver reads Resources/Configs/<config>.toml; the SM8150
# patch only provides the guacamole one.
cfg_src = UEFI / "Resources" / "Configs" / "guacamole.toml"
cfg_dst = UEFI / "Resources" / "Configs" / "raphael.toml"
if cfg_src.is_file() and not cfg_dst.is_file():
    shutil.copy(cfg_src, cfg_dst)
    print(f"firmware: created {cfg_dst.name}")

# aloha-guacamole.patch injects the UniXNU boot manager into the DXE of the
# *guacamole* device directory only:
#
#     Device/oneplus-guacamole/DXE.inc        (the FV contents)
#     Device/oneplus-guacamole/DXE.dsc.inc    (the module list)
#
# raphael builds with TARGET_DEVICE=xiaomi-raphael, and Aloha ships a native
# device directory for it, so those edits never landed.  The firmware was a
# stock Aloha environment with no payload driver at all: BDS had nothing to
# boot and fell through to the Mu "Device Recovery" screen (the wrench + USB
# + QR for aka.ms/devicerecovery).  add_unixnu_dxe() applies the same four
# lines to whatever device this board actually targets.
UNIXNU_DXE_MODULES = (
    "AndromedaPkg/Driver/ShengBootMemoryDxe/ShengBootMemoryDxe.inf",
    "UniXNUPkg/Application/UniXNU/UniXNU.inf",
    "UniXNUPkg/Driver/UniXNUDxe/UniXNUDxe.inf",
    "DarwinBootPkg/Driver/DarwinBoot.inf",
)
# Same form firmware-only.yml appends by hand: the per-module FdtLib override
# is what makes these drivers link (the global [LibraryClasses] entry alone is
# not enough for the FV to come out with DarwinBoot in it).
UNIXNU_DXE_DSC = [
    "%s {\n  <LibraryClasses>\n    FdtLib|MdePkg/Library/BaseFdtLib/BaseFdtLib.inf\n}"
    % inf for inf in UNIXNU_DXE_MODULES
]
UNIXNU_DXE_INC = [f"INF {inf}" for inf in UNIXNU_DXE_MODULES]


def add_unixnu_dxe(device_dir):
    """Make the device's DXE build the UniXNU modules; idempotent."""
    for name, lines, probe in (
            ("DXE.dsc.inc", UNIXNU_DXE_DSC, lambda line, text: line.split(" {")[0] in text),
            ("DXE.inc", UNIXNU_DXE_INC, lambda line, text: line in text)):
        path = device_dir / name
        if not path.is_file():
            raise SystemExit(
                f"firmware: {path} is missing; cannot add the UniXNU DXE "
                f"(dir has: {sorted(p.name for p in device_dir.iterdir())})")
        text = path.read_text()
        missing = [line for line in lines if not probe(line, text)]
        if not missing:
            print(f"firmware: {device_dir.name}/{name} already builds UniXNU")
            continue
        with path.open("a") as fh:
            if not text.endswith("\n"):
                fh.write("\n")
            fh.write("\n" + "\n".join(missing) + "\n")
        print(f"firmware: {device_dir.name}/{name} += {len(missing)} UniXNU module(s)")


add_unixnu_dxe(DST)

board = tomllib.loads((ROOT / "board.toml").read_text())
ctx = types.SimpleNamespace(
    board=board,
    tree=TREE,
    work=WORK,
    logs=LOGS,
    jobs=8,
    args=types.SimpleNamespace(skip_preflight=False),
    uefi=UEFI,
)
# DEBUG builds enable EDK II ASSERTs; a driver that trips one dead-loops
# (black screen until the bootloader watchdog resets the phone).
TARGET = os.environ.get("FW_TARGET", "DEBUG")
firmware.build(ctx, None, UEFI, EDK2, TARGET)
