# -*- coding: utf-8 -*-
"""Enumerate authoritative catalogs from the running backend."""
import json, sys, time, urllib.request, urllib.error

BASE = "http://127.0.0.1:8000"


def get(path, timeout=60):
    url = BASE + path
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return {"path": path, "status": r.status, "ms": round((time.time() - t0) * 1000),
                    "body": body}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return {"path": path, "status": e.code, "ms": round((time.time() - t0) * 1000),
                "body": body}
    except Exception as e:
        return {"path": path, "status": None, "ms": round((time.time() - t0) * 1000),
                "body": "EXC: %r" % (e,)}


if __name__ == "__main__":
    t0 = time.time()
    out = {}
    for p in ["/api/market/datacenter", "/api/market/limit-up", "/api/market/indices"]:
        r = get(p)
        out[p] = r
        print("=== %s -> HTTP %s  %sms  len=%d" % (p, r["status"], r["ms"], len(r["body"])))
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("total %.1fs" % (time.time() - t0))
