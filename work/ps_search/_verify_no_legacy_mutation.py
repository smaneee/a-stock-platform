"""比对两次 legacy 清单快照，证明 P0-01 没有改动任何旧产物。

用法::

    <venv-311>\\Scripts\\python.exe work\\ps_search\\_verify_no_legacy_mutation.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE / "legacy" / "legacy-preservation-manifest.baseline.json"
CUR = HERE / "legacy" / "legacy-preservation-manifest.json"


def index(payload: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in payload.get("existing_artifacts_snapshot", []):
        if item.get("exists") and not item.get("is_dir"):
            out[item["path"]] = item
    for item in payload.get("named_legacy", []):
        if item.get("exists") and not item.get("is_dir"):
            out[item["path"]] = item
    return out


def main() -> int:
    base = index(json.loads(BASE.read_text(encoding="utf-8")))
    cur = index(json.loads(CUR.read_text(encoding="utf-8")))

    unchanged, changed, missing = [], [], []
    for path, item in base.items():
        now = cur.get(path)
        if now is None:
            missing.append(path)
        elif now.get("sha256") == item.get("sha256"):
            unchanged.append(path)
        else:
            changed.append(
                {
                    "path": path,
                    "sha256_before": item.get("sha256"),
                    "sha256_after": now.get("sha256"),
                }
            )
    added = sorted(set(cur) - set(base))

    print(f"baseline files tracked : {len(base)}")
    print(f"unchanged              : {len(unchanged)}")
    print(f"changed by anyone      : {len(changed)}")
    for item in changed:
        print(f"   ! {item['path']}")
    print(f"missing                : {len(missing)}")
    for path in missing:
        print(f"   - {path}")
    print(f"new files (not in base): {len(added)}")
    for path in added:
        print(f"   + {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
