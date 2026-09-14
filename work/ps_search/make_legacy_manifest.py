"""记录「旧结论 / 旧产物原样保留」的证据清单。

本脚本**只读**，不复制、不移动、不删除任何文件。它做两件事：

1. 扫描研发计划里点名的旧脚本 / 旧产物（``work/ps_search/sent_factor.py``、
   ``wf_summary.json``）以及同一批输出目录里的既有文件，记录 SHA-256 与大小；
2. 把结果写成 ``work/ps_search/legacy/legacy-preservation-manifest.json``。

如果旧文件在会话开始前就已经不存在，清单会明确记录 ``exists=False`` 以及
搜索过的位置，避免「悄悄覆盖了旧结论」这种无法证伪的说法。

用法::

    <venv-311>\\Scripts\\python.exe work\\ps_search\\make_legacy_manifest.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]  # .../a-stock-platform
PARENT = PROJECT.parent  # .../2026-09-09-15-34-54

# 研发计划 1.2 / 11.2 / 13 节点名的旧脚本与旧产物，以及同批研究输出
NAMED_LEGACY = [
    PROJECT / "work" / "ps_search" / "sent_factor.py",
    PROJECT / "work" / "ps_search" / "wf_summary.json",
    PROJECT / "backend" / "wf_summary.json",
    PROJECT / "outputs" / "wf_summary.json",
    PROJECT / "outputs" / "param_search_20260912-233431.json",
]

EXISTING_DIRS = [
    PROJECT / "outputs" / "handoff",
    PROJECT / "docs",
]

# 搜索过但未命中的位置（用来证明「不是没找，而是真的不在」）
SEARCHED_ROOTS = [
    str(PROJECT),
    str(PARENT),
    r"D:\A股量化平台备份\20260913_193836\source",
]


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            data = handle.read(chunk)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def describe(path: Path, *, deep: bool = True) -> dict[str, object]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    info: dict[str, object] = {
        "path": str(path),
        "exists": True,
        "is_dir": path.is_dir(),
        "size_bytes": None if path.is_dir() else path.stat().st_size,
        "mtime": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
    }
    if deep and path.is_file():
        info["sha256"] = sha256(path)
    return info


def main() -> int:
    out_dir = HERE / "legacy"
    out_dir.mkdir(parents=True, exist_ok=True)

    named = [describe(p) for p in NAMED_LEGACY]

    existing: list[dict[str, object]] = []
    for directory in EXISTING_DIRS:
        if not directory.exists():
            existing.append({"path": str(directory), "exists": False})
            continue
        for child in sorted(directory.iterdir()):
            if child.is_dir():
                existing.append(
                    {
                        "path": str(child),
                        "exists": True,
                        "is_dir": True,
                        "note": "目录已列出内容清单，未改动",
                        "children": sorted(c.name for c in child.iterdir()),
                    }
                )
            else:
                existing.append(describe(child))

    ps_search_dir = HERE
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "policy": (
            "旧脚本与旧产物一律不覆盖、不删除、不重命名；新结果只写到 "
            "work/ps_search/out/ 与 outputs/handoff/ 下的 **-v2 / p0-01-** 新文件。"
        ),
        "session_start_state": {
            "work/ps_search 目录": "会话开始时不存在（glob **/ps_search/** 无命中）",
            "work/ 目录内容": "仅 tmp/（node-compile-cache、pytest-of-ASUS）",
            "说明": (
                "研发计划 1.2/11.2/13 节点名的 work/ps_search/sent_factor.py "
                "与 wf_summary.json 在本工作区、上级目录与基线备份中均不存在，"
                "因此不存在被本任务覆盖的旧文件；旧结论以 docs/研发计划-2026Q4.md "
                "1.2 节原文为准保留。"
            ),
        },
        "named_legacy": named,
        "searched_roots": SEARCHED_ROOTS,
        "existing_artifacts_snapshot": existing,
        "new_files_created_by_p0_01": sorted(
            str(p.relative_to(PROJECT))
            for p in ps_search_dir.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts
        ),
        "db": describe(PROJECT / "backend" / "a_stock.db", deep=False),
    }
    out = out_dir / "legacy-preservation-manifest.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] wrote {out}")
    missing = [item["path"] for item in named if not item["exists"]]
    print(f"[info] named legacy files missing: {len(missing)}/{len(named)}")
    for path in missing:
        print(f"   - not found: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
