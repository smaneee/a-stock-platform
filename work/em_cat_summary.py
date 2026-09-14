# -*- coding: utf-8 -*-
"""Print compact catalog structure: dataset keys + declared field names only."""
import json, sys

with open(sys.argv[1], encoding="utf-8") as f:
    cat = json.load(f)

for path, r in cat.items():
    print("=====", path, "HTTP", r["status"], r["ms"], "ms")
    try:
        body = json.loads(r["body"])
    except Exception as e:
        print("  NOJSON", e, r["body"][:200]); continue
    if isinstance(body, list):
        print("  list len=%d" % len(body))
        if body:
            print("  keys[0]:", list(body[0].keys()) if isinstance(body[0], dict) else type(body[0]))
        continue
    print("  topkeys:", list(body.keys()))
    for k, v in body.items():
        if isinstance(v, list):
            print("  - %s: list(%d)" % (k, len(v)))
            for item in v[:80]:
                if isinstance(item, dict):
                    ks = list(item.keys())
                    # prefer a name-like key
                    nm = item.get("key") or item.get("dataset") or item.get("name") or item.get("code") or item.get("id") or item.get("pool")
                    fl = item.get("fields") or item.get("columns") or item.get("field_names")
                    if fl:
                        print("      %-28s fields=%s" % (nm, fl))
                    else:
                        print("      %-28s keys=%s" % (nm, ks))
                else:
                    print("      ", item)
        else:
            print("  - %s = %r" % (k, v))
