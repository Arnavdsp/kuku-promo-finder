#!/usr/bin/env python3
"""Kuku Promo Finder - find and rank 20-60s promos in a microdrama screenplay pack.

Examples
  python run.py --input path/to/show_folder                      # Groq (needs GROQ_API_KEY)
  python run.py --input show.zip --episodes 1-5
  python run.py --input show --provider none                     # no AI: rules-only baseline
  python run.py --input show --provider custom --base-url https://their.endpoint/v1 --model their-model
  python run.py --input show --montage --price-in 0.59 --price-out 0.79
"""
import argparse
import json
import sys

from promo_finder.llm import LLMConfig, LLMError, PRESETS
from promo_finder.pipeline import run_pipeline


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="folder of episode .txt files, or a .zip of them")
    ap.add_argument("--provider", choices=list(PRESETS), help="default: $LLM_PROVIDER or groq")
    ap.add_argument("--model", help="default: preset model or $LLM_MODEL")
    ap.add_argument("--base-url", help="OpenAI-compatible base URL (for custom endpoints)")
    ap.add_argument("--episodes", help="e.g. 1-5 or 1,3,7 (default: all)")
    ap.add_argument("--montage", action="store_true", help="also build cross-episode montages (1 extra LLM call)")
    ap.add_argument("--out", default="outputs", help="output root folder")
    ap.add_argument("--name", help="run folder name (default: run_<timestamp>_<model>)")
    ap.add_argument("--price-in", type=float, help="USD per 1M input tokens (for the cost line)")
    ap.add_argument("--price-out", type=float, help="USD per 1M output tokens")
    a = ap.parse_args()

    try:
        cfg = LLMConfig.from_env(a.provider, a.model, a.base_url)
    except LLMError as ex:
        print(f"Config error: {ex}", file=sys.stderr)
        return 2

    def progress(i, n, ep):
        print(f"\r  episode {ep} ({i}/{n})", end="", flush=True)

    print(f"Model: {cfg.provider} / {cfg.model}")
    res = run_pipeline(a.input, cfg, a.out, a.episodes, a.montage, price_in=a.price_in,
                       price_out=a.price_out, progress=progress, run_name=a.name)
    print()
    m, meta = res["metrics"], res["meta"]
    print(f"Parsed {res['report']['episodes']} episodes, {res['report']['events']} events, {res['report']['minutes']} min")
    if "llm" in m:
        l = m["llm"]
        print(f"LLM proposed {l['proposed']} | valid {l['validity_rate']} | rejected {l['rejected_by_reason']} "
              f"| duplicates {l['duplicate_rate']} | failed episodes {l['episodes_failed']}")
    print(f"Shipped {m['shipped_promos']} promos; coverage {m['episode_coverage']['share']}; "
          f"invariant violations: {len(m['shipped_invariant_violations'])}")
    print(f"Tokens in/out: {meta['usage']['prompt_tokens']}/{meta['usage']['completion_tokens']} "
          f"in {meta['usage']['calls']} calls, {meta['elapsed_s']}s, cost: {meta['cost_usd']}")
    print("\nTop 5:")
    for c in res["ranked"][:5]:
        segs = "; ".join(f"Ep{s['episode']} {s['start']:.1f}-{s['end']:.1f}s" for s in c["segments"])
        print(f"  #{c['rank']} [{c['priority']}] {segs} ({c['duration']}s) | {c.get('post_reason','')}")
        print(f"      hook: {c.get('hook_text','')[:90]}")
        print(f"      cliff: {c.get('cliffhanger_text','')[:90]}")
    print(f"\nOutputs: {res['out_dir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
