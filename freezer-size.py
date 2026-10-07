#!/usr/bin/env python3
"""Estimate how much of a geth/BSC freezer (ancient/chain) the most recent N blocks occupy.

Usage: freezer-size.py <ancient/chain dir> <head block> <blocks to keep>
"""
import os, re, struct, sys

if len(sys.argv) != 4 or not (sys.argv[2].isdigit() and sys.argv[3].isdigit()) or int(sys.argv[3]) < 1:
    sys.exit(__doc__.strip().splitlines()[-1])
d, head, keep = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
cutoff = max(head - keep + 1, 0)  # first block that would be kept

def entry(f, i):
    # Index entries are 6 bytes: filenum (uint16 BE) + offset (uint32 BE)
    f.seek(i * 6)
    return struct.unpack(">HI", f.read(6))

def size(b):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1000 or unit == "TB":
            return f"{b:.0f}{unit}" if unit == "B" else f"{b:.2f}{unit}"
        b /= 1000

tot_all = tot_keep = 0
print(f"{'table':9} {'items':>12} {'total':>10} {'keep':>10} {'prune':>10}")
for idx in sorted(x for x in os.listdir(d) if x.endswith((".cidx", ".ridx"))):
    name, ext = idx.rsplit(".", 1)
    dext = "cdat" if ext == "cidx" else "rdat"
    files = {int(m.group(1)): os.path.getsize(os.path.join(d, x))
             for x in os.listdir(d)
             if (m := re.fullmatch(rf"{name}\.(\d{{4}})\.{dext}", x))}
    total = sum(files.values())
    with open(os.path.join(d, idx), "rb") as f:
        n = os.path.getsize(f.name) // 6 - 1        # entries after the header entry
        _, first = entry(f, 0)                       # header: first item number stored
        # Entry k+1 holds where item k ends; so item k starts where entry k says
        k = cutoff - first
        if k <= 0:
            keep_b = total                            # table starts after cutoff (e.g. blobs)
        elif k >= n:
            keep_b = 0
        else:
            fn, off = entry(f, k)
            keep_b = (files[fn] - off) + sum(s for num, s in files.items() if num > fn)
    tot_all += total; tot_keep += keep_b
    print(f"{name:9} {first + n:>12,} {size(total):>10} {size(keep_b):>10} {size(total - keep_b):>10}")
print(f"{'TOTAL':9} {'':>12} {size(tot_all):>10} {size(tot_keep):>10} {size(tot_all - tot_keep):>10}")
print(f"cutoff block = {cutoff:,} (keeping blocks {cutoff:,}..{head:,})")
