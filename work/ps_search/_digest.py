"""把 ``sentiment-v2-latest.json`` 压成报告用摘要（只读，供人工核对数字）。

用法::

    <venv-311>\\Scripts\\python.exe work\\ps_search\\_digest.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "sentiment-v2-latest.json"


def fmt(value: object, digits: int = 6) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def main() -> int:
    payload = json.loads(OUT.read_text(encoding="utf-8"))
    print("# meta")
    print("algo_version:", payload["algo_version"])
    print("generated_at:", payload["generated_at"])
    print("db:", json.dumps(payload["db"], ensure_ascii=False))
    print("sentiment:", json.dumps(payload["sentiment"], ensure_ascii=False))
    print("calendar_days_in_window:", payload["calendar_days_in_window"])

    for adjust, by_k in payload["analyses"].items():
        print(f"\n===== adjust={adjust} =====")
        for k, block in by_k.items():
            print(f"\n--- {k}  per_symbol_rows={block['per_symbol_rows']} "
                  f"market_days={block['market_days']} "
                  f"median_universe={block['median_universe_per_day']} ---")
            print("   ret:", json.dumps(block["ret_describe"], ensure_ascii=False))
            for src, s in block["sources"].items():
                print(f"  [{src}] days={s.get('coverage_days')} "
                      f"aligned={s.get('aligned_days_with_return')} "
                      f"lookahead={s.get('lookahead_check')} "
                      f"verdict={s.get('verdict')}")
                if s.get("reason"):
                    print(f"      reason: {s['reason']}")
                for field, v in (s.get("spearman") or {}).items():
                    if "self_value" not in v:
                        print(f"      spearman {field}: {v}")
                        continue
                    print(f"      spearman {field}: self={fmt(v['self_value'])} "
                          f"scipy={fmt(v.get('scipy_value'))} "
                          f"abs_diff={fmt(v.get('abs_diff'), 12)} ok={v.get('ok')} "
                          f"p={fmt(v.get('scipy_pvalue'))}")
                    bb = v.get("block_bootstrap")
                    if bb:
                        print(f"         block-bootstrap rho: robust_excludes_zero="
                              f"{bb['robust_excludes_zero']}")
                        for item in bb["intervals"]:
                            print(f"            b={item['block']:<3} "
                                  f"point={fmt(item['point'])} "
                                  f"CI=[{fmt(item['ci_low'])}, {fmt(item['ci_high'])}] "
                                  f"excl0={item['excludes_zero']} "
                                  f"degenerate={item.get('degenerate')}")
                if s.get("splits"):
                    print("      splits:", json.dumps(s["splits"], ensure_ascii=False))
                    print("      integrity:", json.dumps(s["split_integrity"], ensure_ascii=False))
                    print("      timing_rule:", json.dumps(s["timing_rule"], ensure_ascii=False))
                for seg, data in (s.get("segments") or {}).items():
                    if "sensitivity_spread" not in data:
                        print(f"      seg {seg}: {data}")
                        continue
                    print(f"      seg {seg}: n={data['n']} {data['start']}~{data['end']} "
                          f"mean_mkt={fmt(data['mean_market_ret'])} "
                          f"mean_spread={fmt(data['mean_spread'])} "
                          f"overlay_excess={fmt(data['mean_overlay_excess_vs_buyhold'])} "
                          f"hi={fmt(data['mean_market_ret_when_high'])} "
                          f"lo={fmt(data['mean_market_ret_when_low'])}")
                    for label, key in (("spread", "sensitivity_spread"),
                                       ("overlay", "sensitivity_overlay_excess")):
                        sens = data[key]
                        print(f"         {label} mean={fmt(sens['mean'])} "
                              f"stable_sign={sens['stable_sign']} "
                              f"sig95={sens['significant_at_95']}")
                        for item in sens["hac"]:
                            print(f"            HAC L={item['lags']:<3} se={fmt(item['se'],8)} "
                                  f"CI=[{fmt(item['ci_low'])}, {fmt(item['ci_high'])}] "
                                  f"t={fmt(item['t_stat'],4)}")
                        for item in sens["bootstrap"]:
                            print(f"            BOOT b={item['block']:<3} se={fmt(item['boot_se'],8)} "
                                  f"CI=[{fmt(item['ci_low'])}, {fmt(item['ci_high'])}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
