"""Run the k6 spike test with the webhook secret from `.env` (never printed or put on the command line).

    python scripts/run_spike.py [--peak 500] [--products 1000] [--summary load/results/spike-summary.json]

Needs cdms-api (+ cdms-worker, emulator for background polling) running and k6 on PATH or in its default
folder.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from cdms.config import get_settings

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_K6 = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "k6" / "k6.exe"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--peak", type=int, default=500, help="requests / s during the spike")
    parser.add_argument("--products", type=int, default=1000, help="load-test products to spread events over")
    parser.add_argument("--cdms-url", default="http://localhost:8100")
    parser.add_argument("--summary", default="load/results/spike-summary.json")
    args = parser.parse_args()

    secret = get_settings().webhook_secret
    if secret is None:
        print("WEBHOOK_SECRET is not set in .env", file=sys.stderr)
        return 2
    k6 = shutil.which("k6") or (str(DEFAULT_K6) if DEFAULT_K6.exists() else None)
    if k6 is None:
        print("k6 not found (winget install --id GrafanaLabs.k6 -e)", file=sys.stderr)
        return 2

    env = os.environ | {
        "WEBHOOK_SECRET": secret.get_secret_value(),
        "CDMS_URL": args.cdms_url,
        "PEAK": str(args.peak),
        "PRODUCTS": str(args.products),
        "SUMMARY": args.summary,
    }
    (ROOT / args.summary).parent.mkdir(parents=True, exist_ok=True)
    return subprocess.call([k6, "run", "--quiet", "load/k6/spike.js"], cwd=ROOT, env=env)


if __name__ == "__main__":
    sys.exit(main())
