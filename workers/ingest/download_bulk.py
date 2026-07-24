"""Download the current-cycle Open States bulk zips listed in
data/registry/bulk-urls.json into data/bulkzips/, politely paced.

Idempotent: skips files that already exist and pass a zip integrity check.
Usage: python3 workers/ingest/download_bulk.py [--only ST,ST2]
"""
import json
import pathlib
import re
import sys
import time
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
DEST = ROOT / "data" / "bulkzips"
UA = "BillCommons/0.1 (+https://billcommons.org; contact@billcommons.org)"


def safe_name(state: str, slug: str) -> str:
    slug = urllib.request.unquote(slug)
    return f"{state}_{re.sub(r'[^A-Za-z0-9._-]+', '-', slug)}.zip"


def ok_zip(path: pathlib.Path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            return z.testzip() is None
    except Exception:
        return False


def main() -> int:
    only = None
    if len(sys.argv) > 2 and sys.argv[1] == "--only":
        only = {s.strip().upper() for s in sys.argv[2].split(",")}
    reg = json.loads((ROOT / "data/registry/bulk-urls.json").read_text())
    DEST.mkdir(parents=True, exist_ok=True)
    jobs = []
    for state, info in sorted(reg["jurisdictions"].items()):
        if info.get("optional"):
            continue
        if only and state not in only:
            continue
        for entry in info["current"]:
            m = re.search(r"/latest/[A-Za-z]{2}_(.+?)_csv_", entry["url"])
            slug = m.group(1) if m else entry.get("slug", "session")
            jobs.append((state, slug, entry["url"]))
    done = failed = skipped = 0
    for state, slug, url in jobs:
        path = DEST / safe_name(state, slug)
        if path.exists() and ok_zip(path):
            skipped += 1
            continue
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            if not ok_zip(path):
                path.unlink(missing_ok=True)
                raise ValueError("zip integrity check failed")
            done += 1
            print(f"OK   {path.name} {path.stat().st_size/1e6:.1f}MB", flush=True)
        except Exception as e:  # noqa: BLE001 - log and continue the batch
            failed += 1
            print(f"FAIL {state} {slug}: {e}", flush=True)
        time.sleep(1.0)
    print(f"downloaded={done} skipped={skipped} failed={failed} total={len(jobs)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
