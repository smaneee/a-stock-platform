"""基线备份工具：SQLite 在线备份 API + SHA-256 清单 + 只读恢复验证。"""
from __future__ import annotations
import hashlib, json, os, sqlite3, sys, time
from datetime import datetime
from pathlib import Path

PROJECT = Path(r"E:\workbuddy work\2026-09-09-15-34-54\a-stock-platform")
DB = PROJECT / "backend" / "a_stock.db"
STATE = PROJECT / "outputs" / "handoff" / ".last_backup_dest.txt"


def ro_uri(path: Path) -> str:
    return "file:" + str(path).replace("\\", "/").replace(" ", "%20") + "?mode=ro"


def connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(ro_uri(path), uri=True, timeout=60)
    if con.execute("pragma page_count").fetchone()[0] == 0:
        con.close()
        raise RuntimeError("page_count==0, URI parse error, refuse: " + str(path))
    return con


def sha256_file(path, chunk=4 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def table_row_counts(con) -> dict:
    names = [r[0] for r in con.execute(
        "select name from sqlite_master where type='table' and name not like 'sqlite_%' order by name")]
    return {n: con.execute('select count(*) from "' + n + '"').fetchone()[0] for n in names}


def cmd_backup(dest_root: Path) -> int:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = dest_root / ts
    dest.mkdir(parents=True, exist_ok=True)
    print("[1/5] dest " + str(dest), flush=True)
    src_size = DB.stat().st_size
    src_mtime = datetime.fromtimestamp(DB.stat().st_mtime).isoformat()
    print("[2/5] source " + str(src_size) + " bytes mtime=" + src_mtime, flush=True)
    t0 = time.time()
    src = connect_ro(DB)
    pages = src.execute("pragma page_count").fetchone()[0]
    print("      page_count=" + str(pages) + " journal_mode=" + src.execute("pragma journal_mode").fetchone()[0], flush=True)
    target_path = dest / "a_stock.db"
    tgt = sqlite3.connect(str(target_path))
    try:
        src.backup(tgt)
    finally:
        tgt.close()
    print("[3/5] backup done " + str(target_path.stat().st_size) + " bytes in " + str(round(time.time() - t0, 1)) + "s", flush=True)
    t1 = time.time()
    bck = connect_ro(target_path)
    bp = bck.execute("pragma page_count").fetchone()[0]
    integ = bck.execute("pragma integrity_check").fetchall()
    fk = bck.execute("pragma foreign_key_check").fetchall()
    bck_counts = table_row_counts(bck)
    bck.close()
    src_counts = table_row_counts(src)
    src.close()
    print("[4/5] verify integrity=" + str(integ[0][0]) + " pages=" + str(bp) + " fk=" + str(len(fk)) + " in " + str(round(time.time() - t1, 1)) + "s", flush=True)
    mismatch = {k: [src_counts.get(k), bck_counts.get(k)] for k in set(src_counts) | set(bck_counts)
                if src_counts.get(k) != bck_counts.get(k)}
    t2 = time.time()
    bck_sha = sha256_file(target_path)
    print("[5/5] sha256=" + bck_sha + " in " + str(round(time.time() - t2, 1)) + "s", flush=True)
    info = {"created_at": datetime.now().isoformat(), "dest_dir": str(dest), "source_db": str(DB),
            "source_size_bytes": src_size, "source_mtime": src_mtime, "source_page_count": pages,
            "journal_mode": "delete", "backup_file": str(target_path),
            "backup_size_bytes": target_path.stat().st_size, "backup_page_count": bp,
            "integrity_check": integ[0][0], "integrity_statements": len(integ),
            "foreign_key_violations": len(fk), "table_count": len(bck_counts),
            "row_counts_backup": bck_counts, "row_count_mismatch": mismatch,
            "row_counts_match": not mismatch, "backup_sha256": bck_sha,
            "restore_verification": "PASS" if (integ[0][0] == "ok" and not mismatch) else "FAIL"}
    (dest / "backup-info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    STATE.write_text(str(dest), encoding="utf-8")
    print("RESULT_JSON " + json.dumps({k: info[k] for k in ("dest_dir", "backup_size_bytes", "backup_sha256",
          "integrity_check", "foreign_key_violations", "table_count", "row_counts_match", "restore_verification")},
          ensure_ascii=False), flush=True)
    return 0


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += (Path(root) / n).stat().st_size
            except OSError:
                pass
    return total


def _is_complete(dest: Path) -> bool:
    info = dest / "backup-info.json"
    if not (info.is_file() and (dest / "a_stock.db").is_file()):
        return False
    try:
        data = json.loads(info.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("restore_verification") == "PASS"


def _recorded_sha(dest: Path) -> str:
    try:
        return json.loads((dest / "backup-info.json").read_text(encoding="utf-8")).get("backup_sha256") or ""
    except (OSError, ValueError):
        return ""


def cmd_prune(dest_root: Path, keep: int = 3, dry_run: bool = False) -> int:
    """保留策略：按内容去重，最多保留最新 keep 份“内容互不相同且已验证”的备份。

    - 只处理形如 YYYYmmdd_HHMMSS 的时间戳目录；
    - 最新一个完整备份永不删除；
    - 判定重复时重新计算实际 SHA-256，不盲信 backup-info.json 里的记录值；
    - 其余目录（更旧的、未完成的残留）全部删除。
    """
    import re
    import shutil
    pattern = re.compile(r"^\d{8}_\d{6}$")
    if not dest_root.is_dir():
        print("prune: dest root not found " + str(dest_root))
        return 2
    dirs = sorted([d for d in dest_root.iterdir() if d.is_dir() and pattern.match(d.name)],
                  key=lambda d: d.name, reverse=True)
    complete = [d for d in dirs if _is_complete(d)]
    incomplete = [d for d in dirs if d not in complete]
    keep = max(keep, 1)
    kept: list = []          # [(dir, sha)] —— 内容互不相同的幸存者
    duplicates: list = []    # 仅因内容与幸存者相同而删除
    evicted: list = []       # 因超出保留份数而删除
    for d in complete:
        sha = _recorded_sha(d)
        match = next((k for k in kept if sha and k[1] == sha), None)
        if match is not None:
            if dry_run:
                duplicates.append(d)
                continue
            actual = sha256_file(d / "a_stock.db")
            if actual == match[1]:
                duplicates.append(d)
                print("  duplicate of " + match[0].name + " (sha verified) -> " + d.name)
                continue
            print("  !! " + d.name + " 记录 sha 与实际不符，作为独立备份保留")
        if len(kept) < keep:
            kept.append((d, sha))
        else:
            evicted.append(d)
    survivors = set(d.name for d, _ in kept)
    print("prune: total=" + str(len(dirs)) + " complete=" + str(len(complete))
          + " distinct_kept=" + str(sorted(survivors)))
    freed = 0
    deleted = []
    for d in incomplete + duplicates + evicted:
        if d.name in survivors:
            continue
        size = _dir_size(d)
        reason = "incomplete" if d in incomplete else ("duplicate" if d in duplicates else "older")
        if dry_run:
            print("  would delete " + d.name + "  " + str(round(size / (1 << 30), 2)) + " GB  (" + reason + ")")
            continue
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            freed += size
            deleted.append(d.name)
            print("  deleted " + d.name + "  " + str(round(size / (1 << 30), 2)) + " GB  (" + reason + ")")
        else:
            print("  delete FAILED " + d.name)
    if not dry_run:
        print("prune: deleted=" + str(len(deleted)) + " freed_gb=" + str(round(freed / (1 << 30), 2)))
    return 0


def cmd_manifest(dest: Path) -> int:
    files, total = [], 0
    for root, dirs, names in os.walk(dest):
        dirs[:] = [d for d in dirs if d not in {"node_modules", ".venv", ".venv-311", "__pycache__"}]
        for n in sorted(names):
            p = Path(root) / n
            if p.name in {"manifest.json", "manifest.sha256"}:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            files.append({"path": str(p.relative_to(dest)).replace("\\", "/"), "size": size, "sha256": sha256_file(p)})
            total += size
    (dest / "manifest.sha256").write_text("\n".join(f["sha256"] + "  " + f["path"] for f in files) + "\n", encoding="utf-8")
    (dest / "manifest.json").write_text(json.dumps({"generated_at": datetime.now().isoformat(),
        "file_count": len(files), "total_bytes": total, "files": files}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("manifest files=" + str(len(files)) + " total_bytes=" + str(total), flush=True)
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: backup [keep] [dest_root] | prune [keep] [dest_root] [--dry-run] | manifest [dest]")
        return 2
    keep = int(os.environ.get("BASELINE_BACKUP_KEEP", "3"))
    if sys.argv[1] == "backup":
        root = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(r"D:\A股量化平台备份")
        rc = cmd_backup(root)
        if rc == 0:
            print("[prune] 保留最新 " + str(keep) + " 个已验证备份", flush=True)
            cmd_prune(root, keep=keep)
        return rc
    if sys.argv[1] == "prune":
        args = [a for a in sys.argv[2:] if not a.startswith("--")]
        root = Path(args[1]) if len(args) > 1 else Path(r"D:\A股量化平台备份")
        k = int(args[0]) if args else keep
        return cmd_prune(root, keep=k, dry_run="--dry-run" in sys.argv)
    if sys.argv[1] == "manifest":
        return cmd_manifest(Path(sys.argv[3]))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
