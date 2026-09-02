#!/usr/bin/env python
"""Download or verify the exact optional Kronos model revisions."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from huggingface_hub import snapshot_download


MODEL_ID = "NeoQuasar/Kronos-base"
MODEL_REVISION = "2b554741eca47781b64468546e77fef3e85130e6"
TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"
TOKENIZER_REVISION = "0e0117387f39004a9016484a186a908917e22426"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-root", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    cache_root = args.cache_root.expanduser().resolve()
    cache_root.mkdir(parents=True, exist_ok=True)
    resolved = {}
    for label, repo_id, revision in (
        ("model", MODEL_ID, MODEL_REVISION),
        ("tokenizer", TOKENIZER_ID, TOKENIZER_REVISION),
    ):
        resolved[label] = snapshot_download(
            repo_id=repo_id,
            revision=revision,
            cache_dir=cache_root,
            local_files_only=args.offline,
        )
    receipt = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "offline": args.offline,
        "cache_root": str(cache_root),
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "tokenizer_id": TOKENIZER_ID,
        "tokenizer_revision": TOKENIZER_REVISION,
        "resolved": resolved,
    }
    receipt_path = cache_root / "kronos_cache_manifest.json"
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(receipt_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
