import json,sys
p=r"E:\workbuddy work\2026-09-09-15-34-54\a-stock-platform\outputs\handoff\eastmoney-matrix-raw.json"
d=json.load(open(p,encoding="utf-8"))
print("top keys:", list(d.keys()))
print("datasets:", len(d["datasets"]), "pools:", len(d["limit_up_pools"]))
print("frontend_assets:", json.dumps(d["other_endpoints"]["frontend_assets"], ensure_ascii=False)[:400])
print("session:", json.dumps(d["meta"]["市场会话"], ensure_ascii=False)[:200])
print("issues:", len(d["issues"]), "unverified:", len(d["unverified"]))
print("stack_leak:", d["error_behavior"]["stack_leak_detected"], "500:", d["error_behavior"]["http_500_observed"])
print("sample dc dragon-tiger markets:", d["datasets"]["dragon-tiger"]["markets_limit500"])
print("nulls northbound:", d["datasets"]["northbound"]["always_null_fields_limit200"])
