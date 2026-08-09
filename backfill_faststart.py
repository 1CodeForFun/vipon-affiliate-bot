#!/usr/bin/env python3
"""
backfill_faststart.py — Rewrite existing reels with +faststart so Buffer can
extract a cover frame from them.

Reels built before the faststart fix have the MP4 moov atom AFTER mdat, so
Buffer's thumbnail proxy (images.buffer.com/thumbnail/...?url=<mp4>) returns
422 "No frame data extracted". Buffer hands that proxy URL to Pinterest as the
pin cover, Pinterest fetches it, gets the 422, and fails the pin with
"Sorry we could not fetch the image". TikTok is unaffected because it downloads
the whole file rather than using the proxy.

For each row with a video URL this downloads the file, checks the atom order,
and — only if needed — stream-copies it with +faststart (no re-encode, no
quality loss, ~1 byte size change), re-uploads to Cloudinary and updates the
sheet cell.

Safe to re-run: rows that are already faststart are skipped, and a row is only
rewritten after its replacement has uploaded successfully.

Usage:
  python backfill_faststart.py --dry-run     # report only, change nothing
  python backfill_faststart.py               # do it
  python backfill_faststart.py --limit 5     # first 5 needing work
"""

import argparse
import os
import struct
import subprocess
import sys
import tempfile
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reel_concept_test import cloud_upload, _which_ffmpeg   # noqa: E402

COL_N_REEL_URL = 14          # 1-based
SHEETS = [("Sheet1", "US"), ("Sheet2", "CA")]


def log(m):
    print(m, flush=True)


def _open_sheet(tab):
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
    for p in ("../vipon_google_creds.json", "vipon_google_creds.json",
              os.path.join(os.environ.get("SECRETS_DIR", "."), "vipon_google_creds.json")):
        if os.path.exists(p):
            creds = ServiceAccountCredentials.from_json_keyfile_name(
                p, ["https://spreadsheets.google.com/feeds",
                    "https://www.googleapis.com/auth/drive"])
            ss = gspread.authorize(creds).open("vipon")
            return ss.worksheet(tab) if tab != "Sheet1" else ss.sheet1
    sys.exit("vipon_google_creds.json not found")


def is_faststart(path) -> bool:
    """True when moov comes before mdat (streamable)."""
    order = []
    with open(path, "rb") as f:
        pos = 0
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            size = struct.unpack(">I", hdr[:4])[0]
            typ = hdr[4:8].decode("latin1", "replace")
            if size == 1:
                size = struct.unpack(">Q", f.read(8))[0]
            if size == 0:
                order.append(typ)
                break
            f.seek(pos + size)
            order.append(typ)
            pos = f.tell()
    return "moov" in order and "mdat" in order and \
           order.index("moov") < order.index("mdat")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    ffmpeg = _which_ffmpeg()
    fixed = skipped = failed = 0

    for tab, label in SHEETS:
        try:
            ws = _open_sheet(tab)
        except Exception as e:
            log(f"{label}: cannot open {tab} ({e}) — skipping")
            continue
        rows = ws.get_all_values()
        log(f"\n=== {label} ({tab}): {len(rows)-1} data row(s) ===")

        for idx, row in enumerate(rows[1:], start=2):
            if args.limit and fixed >= args.limit:
                log(f"{label}: hit --limit {args.limit}")
                break
            while len(row) < COL_N_REEL_URL:
                row.append("")
            url = row[COL_N_REEL_URL - 1].strip()
            if not url.startswith("http"):
                continue

            with tempfile.TemporaryDirectory(prefix="fs_") as td:
                src = os.path.join(td, "in.mp4")
                try:
                    r = requests.get(url, timeout=180)
                    r.raise_for_status()
                    open(src, "wb").write(r.content)
                except Exception as e:
                    log(f"  row {idx}: download failed ({str(e)[:60]}) — skipping")
                    failed += 1
                    continue

                if is_faststart(src):
                    skipped += 1
                    continue

                if args.dry_run:
                    log(f"  row {idx}: NOT faststart — would rewrite "
                        f"({os.path.getsize(src):,} bytes)")
                    fixed += 1
                    continue

                dst = os.path.join(td, "out.mp4")
                p = subprocess.run(
                    [ffmpeg, "-y", "-loglevel", "error", "-i", src,
                     "-c", "copy", "-movflags", "+faststart", dst],
                    capture_output=True, timeout=300)
                if p.returncode != 0 or not os.path.exists(dst):
                    log(f"  row {idx}: remux failed — leaving original")
                    failed += 1
                    continue
                if not is_faststart(dst):
                    log(f"  row {idx}: remux did not move moov — leaving original")
                    failed += 1
                    continue

                base = os.path.splitext(os.path.basename(url.split("?")[0]))[0]
                new_url = cloud_upload(dst, f"vipon_reels/{base}_fs")
                if not new_url:
                    log(f"  row {idx}: upload failed — leaving original")
                    failed += 1
                    continue

                # Only touch the sheet once the replacement is live.
                try:
                    ws.update_acell(f"N{idx}", new_url)
                    fixed += 1
                    log(f"  row {idx}: ✓ faststart -> {new_url[-58:]}")
                except Exception as e:
                    log(f"  row {idx}: sheet write failed ({str(e)[:60]}) "
                        f"— new file is at {new_url}")
                    failed += 1
                time.sleep(1.2)      # sheet write quota

    log(f"\n=== {'DRY RUN — ' if args.dry_run else ''}"
        f"rewritten {fixed} | already faststart {skipped} | failed {failed} ===")


if __name__ == "__main__":
    main()
