#!/usr/bin/env python3
"""Verify a set of ABL-split sparse pieces without the dense image they came from.

    check_pieces.py PIECES_DIR

verify_pieces.py compares every block against the dense image, which is the
strongest check but needs that 5.4 GiB file on disk.  This script answers the
question you can still ask when only the pieces were downloaded:

  * does each piece declare the absolute block range it belongs to
    (a leading DONT_CARE that ends exactly where the previous piece ended)?
  * is the union of the pieces a *dense* image -- every block from 0 to the
    last block stored as RAW/FILL, no gaps, no overlaps?

The second point is the one that matters.  The bootloader treats DONT_CARE as
a seek, not as a write of zeros, so any block no piece stores keeps whatever
the partition held before: with the iOS root that means stale blocks inside
the HFS+ catalog B-tree, and a volume that is not the one CI verified.

It also sanity-checks the filesystem: block 0 must carry an HFS+ volume
header, so a piece set that starts with something else is caught before the
phone is wiped.

The sparse chunk header is the SM8150 ABL's, i.e. "u16 type, u16 reserved,
u32 blocks, u32 bytes" -- the opposite field order from AOSP libsparse, which
is why the pieces cannot be fed to simg2img.
"""
import argparse
import os
import struct
import sys

SPARSE_MAGIC = 0xED26FF3A
CHUNK_RAW, CHUNK_FILL, CHUNK_DONT_CARE, CHUNK_CRC32 = 0xCAC1, 0xCAC2, 0xCAC3, 0xCAC4
NAMES = {CHUNK_RAW: "RAW", CHUNK_FILL: "FILL", CHUNK_DONT_CARE: "DONT_CARE",
         CHUNK_CRC32: "CRC32"}


def walk(path, verbose=False):
    """Yield (type, at_block, blocks) and return the header information."""
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        head = fh.read(28)
        magic, major, minor, file_hdr, chunk_hdr = struct.unpack_from("<IHHHH", head, 0)
        if magic != SPARSE_MAGIC:
            raise SystemExit(f"{path}: not a sparse image (magic {magic:#x})")
        block_size, total_blocks, total_chunks = struct.unpack_from("<III", head, 12)
        if block_size != 4096:
            raise SystemExit(f"{path}: block size {block_size}, expected 4096")

        at = 0
        chunks = []
        consumed = file_hdr
        for _ in range(total_chunks):
            fh.seek(consumed)
            ctype, _res, blocks, nbytes = struct.unpack("<HHII", fh.read(chunk_hdr))
            chunks.append((ctype, at, blocks, nbytes))
            # This bootloader's chunk header counts its own 12 bytes in nbytes
            # (389132 = 95 * 4096 + 12 for the first chunk of p00), unlike
            # AOSP libsparse where chunk_sz is the payload alone.
            payload = nbytes - chunk_hdr
            consumed += chunk_hdr
            if ctype in (CHUNK_RAW, CHUNK_FILL):
                data = fh.read(payload)
                if len(data) != payload:
                    raise SystemExit(f"{path}: chunk data truncated at block {at}")
                consumed += payload
            at += blocks
        if at != total_blocks:
            raise SystemExit(f"{path}: chunks cover {at} blocks, header says {total_blocks}")
    return block_size, total_blocks, chunks, consumed, size


def hfs_signature(path):
    """Signatures at volume offset 1024: 'H+'/'HX' for HFS+/HFSX."""
    with open(path, "rb") as fh:
        fh.seek(28)                                  # file header -> first chunk header
        _ctype, _res, _blocks, _n = struct.unpack("<HHII", fh.read(12))
        data = fh.read(2048)
    if len(data) < 2048:
        return None
    return data[1024:1026]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pieces_dir")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    names = sorted(n for n in os.listdir(args.pieces_dir) if n.endswith(".simg"))
    if not names:
        raise SystemExit(f"no *.simg in {args.pieces_dir}")

    coverage = {}
    total_blocks = 0
    ok = True
    for name in names:
        path = os.path.join(args.pieces_dir, name)
        block_size, blocks, chunks, consumed, size = walk(path)
        raw = sum(b for t, _, b, _ in chunks if t == CHUNK_RAW)
        fill = sum(b for t, _, b, _ in chunks if t == CHUNK_FILL)
        skip = sum(b for t, _, b, _ in chunks if t == CHUNK_DONT_CARE)
        first = chunks[0] if chunks else (0, 0, 0, 0)
        lead = first[2] if first[0] == CHUNK_DONT_CARE else 0
        print("%-12s %8.1f MiB  blocks=%-7d (%.2f GiB)  chunks=%-6d RAW+FILL=%-7d "
              "DONT_CARE=%-7d lead_skip=%d"
              % (name, size / 2 ** 20, blocks, blocks * block_size / 2 ** 30,
                 len(chunks), raw + fill, skip, lead))
        if consumed != size:
            print("   !! file length %d != chunks %d" % (size, consumed))
            ok = False
        for ctype, at, nblocks, _ in chunks:
            if ctype in (CHUNK_RAW, CHUNK_FILL):
                for b in range(at, at + nblocks):
                    if b in coverage:
                        print("   !! block %d stored twice (%s and %s)"
                              % (b, coverage[b], name))
                        ok = False
                        break
                    coverage[b] = name
        total_blocks = max(total_blocks, blocks)

    missing = [b for b in range(total_blocks) if b not in coverage]
    print("\nvolume: %d blocks (%.2f GiB)" % (total_blocks, total_blocks * 4096 / 2 ** 30))
    print("stored: %d blocks (%.2f GiB)  missing: %d"
          % (len(coverage), len(coverage) * 4096 / 2 ** 30, len(missing)))
    if missing:
        print("!! NOT dense -- %d block(s) would keep whatever the partition held:"
              % len(missing))
        for b in missing[:10]:
            print("     block %d" % b)
        ok = False
    else:
        print("dense: every block is stored, the flash reproduces the image exactly")

    # Ordering: the pieces must tile the volume, piece i starting where i-1 ended.
    expected_start = 0
    for name in names:
        path = os.path.join(args.pieces_dir, name)
        _bs, blocks, chunks, _c, _s = walk(path)
        first_data = next(((t, at) for t, at, b, _ in chunks
                           if t in (CHUNK_RAW, CHUNK_FILL)), None)
        if first_data and first_data[1] != expected_start:
            print("!! %s first stored block is %d, expected %d"
                  % (name, first_data[1], expected_start))
            ok = False
        expected_start = blocks

    sig = hfs_signature(os.path.join(args.pieces_dir, names[0]))
    if sig in (b"H+", b"HX"):
        print("filesystem: %s at volume offset 1024 -- HFS%s"
              % (sig.decode(), "+" if sig == b"H+" else "X"))
    else:
        print("!! volume offset 1024 is %r, not an HFS+ signature" % (sig,))
        ok = False

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
