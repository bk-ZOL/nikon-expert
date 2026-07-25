#!/usr/bin/env python3
"""Cloud-side WPS sync — reuses sync_kwiki.py's mirror/incremental/ingest logic
but swaps its kwiki-cli calls for direct HTTP (wps_api), so it runs headless on
the server with no kwiki-cli binary. Auth via X_KWIKI_AUTH; KB via KWIKI_KB_KUID.

Usage (same flags as sync_kwiki):
  python scripts/sync_wps.py --dry-run     # list what would sync, no ingest
  python scripts/sync_wps.py               # sync + incremental ingest
  python scripts/sync_wps.py --no-ingest   # mirror only
  python scripts/sync_wps.py --force       # re-download all
"""
import os
import sys

_ROOT = "/opt/nikon-expert"
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
os.chdir(_ROOT)

from dotenv import load_dotenv
load_dotenv(os.path.join(_ROOT, ".env"))
# optional dedicated secrets file (chmod 600) for the WPS token
_kw = os.path.join(_ROOT, ".kwiki_env")
if os.path.exists(_kw):
    load_dotenv(_kw, override=True)

import wps_api
import sync_kwiki

# swap the CLI transport for direct HTTP (both file-list & file-download)
sync_kwiki.run_cli = wps_api.run_cli

if __name__ == "__main__":
    sync_kwiki.main()
