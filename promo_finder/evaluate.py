"""Evaluation. Automatic metrics come from the run itself; human metrics come from a blinded review sheet.

  python -m promo_finder.evaluate sheet  <run_dir> <pack_dir>   -> review_sheet.csv (+ hidden key)
  python -m promo_finder.evaluate score  <run_dir>              -> human_metrics.json (after you fill the sheet)
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

from .parse import fmt, load_show


def auto_metrics(out: Path, *, llm_raw, llm_checked, llm_kept, llm_dups, ranked, chosen, statuses,
                 llm_enabled, ep_by_num) -> dict:
    out = Path(out)
    m: dict = {"note": "All numbers computed from this run's files; nothing hand-entered."}
    # Hard invariants on what we ship (always checked, also in no-LLM mode)
    bad = []
    for c in ranked:
        tot = sum(s["end"] - s["start"] for s in c["segments"])
        for s in c["segments"]:
            ep = ep_by_num[s["episode"]]
            if not (0 <= s["start"] < s["end"] <= ep.duration + 0.01):
                bad.append((c["id"], "segment_outside_episode"))
        if not (20 <= tot <= 60):
            bad.append((c["id"], "duration_outside_20_60"))
    m["shipped_invariant_violations"] = bad
    m["shipped_promos"] = len(ranked)
    cov = {s["episode"] for c in ranked for s in c["segments"]}
    m["episode_coverage"] = {"episodes_processed": len(chosen), "with_valid_promo": len(cov),
                             "share": round(len(cov) / len(chosen), 3) if chosen else None,
                             "no_promo": [n for n in chosen if n not in cov]}
    if llm_enabled:
        n = len(llm_raw)
        valid = [c for c in llm_checked if c["status"] == "valid"]
        rej = Counter(c["reject_reason"] for c in llm_checked if c["status"] == "rejected")
        m["llm"] = {
            "episodes_failed": [s["episode"] for s in statuses if s.get("error")],
            "proposed": n,
            "validity_rate": round(len(valid) / n, 3) if n else None,
            "passed_without_trim": round(sum(1 for c in valid if not c.get("trimmed")) / n, 3) if n else None,
            "trimmed": sum(1 for c in valid if c.get("trimmed")),
            "rejected_by_reason": dict(rej),
            "id_validity": round(1 - rej.get("unknown_event_id", 0) / n, 3) if n else None,
            "quote_accuracy": round(sum(1 for c in valid if min(c["grounding"].values()) >= 0.85) / len(valid), 3) if valid else None,
            "duplicate_rate": round(len(llm_dups) / len(valid), 3) if valid else None,
            "duplicates_by_type": dict(Counter(c.get("dropped_as") for c in llm_dups)),
            "flag_counts_all_valid": dict(Counter(f for c in llm_kept for f in c["flags"])),
            "top15_flagged_for_review": sum(1 for c in ranked[:15] if c["flags"]),
            "avg_promos_per_episode": round(n / len(chosen), 2) if chosen else None,
        }
    (out / "metrics.json").write_text(json.dumps(m, indent=2))
    return m


def _excerpt(c: dict, eps_by_num) -> str:
    lines = []
    for s in c["segments"]:
        ep = eps_by_num[s["episode"]]
        lines.append(f"--- Episode {s['episode']} {fmt(s['start'])}-{fmt(s['end'])} ---")
        for e in ep.events:
            if e.kind in ("DIALOGUE", "ACTION", "SOUND") and e.start >= s["start"] - 0.01 and e.end <= s["end"] + 0.01:
                t = f"+{e.start - s['start']:.1f}s"
                lines.append(f"{t} {e.speaker}: \"{e.text}\"" if e.kind == "DIALOGUE" else f"{t} [{e.kind}] {e.text}")
    return "\n".join(lines)


def make_review_sheet(run_dir: str | Path, pack_dir: str | Path, seed: int = 7) -> Path:
    """Blinded, shuffled: LLM top 15 + 5 random LLM ranked >=30 + baseline top 10. Rater sees only excerpts."""
    run_dir = Path(run_dir)
    eps = {e.number: e for e in load_show(pack_dir)}
    llm = json.loads((run_dir / "promos_ranked.json").read_text())
    base = json.loads((run_dir / "baseline_ranked.json").read_text())
    rng = random.Random(seed)
    picks = [("llm_top", c) for c in llm[:15] if c["source"] == "llm"]
    low = [c for c in llm[29:] if c["source"] == "llm"]
    picks += [("llm_low", c) for c in rng.sample(low, min(5, len(low)))]
    picks += [("baseline", c) for c in base[:10]]
    rng.shuffle(picks)
    rows, key = [], []
    for i, (group, c) in enumerate(picks, 1):
        rows.append({"item": i, "excerpt": _excerpt(c, eps), "hook_1to3": "", "cliffhanger_1to3": "",
                     "standalone_1to3": "", "post_it_YN": "", "boundary_ok_YN": "", "notes": ""})
        key.append({"item": i, "group": group, "id": c["id"], "priority": c.get("priority"),
                    "rank": c.get("rank")})
    sheet = run_dir / "review_sheet.csv"
    pd.DataFrame(rows).to_csv(sheet, index=False)
    pd.DataFrame(key).to_csv(run_dir / "review_key_DO_NOT_OPEN_BEFORE_RATING.csv", index=False)
    return sheet


def _spearman(x: list[float], y: list[float]) -> float | None:
    if len(x) < 3:
        return None
    rx = pd.Series(x).rank().tolist()
    ry = pd.Series(y).rank().tolist()
    return round(pd.Series(rx).corr(pd.Series(ry)), 3)


def score_review(run_dir: str | Path) -> dict:
    run_dir = Path(run_dir)
    s = pd.read_csv(run_dir / "review_sheet.csv")
    k = pd.read_csv(run_dir / "review_key_DO_NOT_OPEN_BEFORE_RATING.csv")
    d = s.merge(k, on="item")
    for col in ("hook_1to3", "cliffhanger_1to3", "standalone_1to3"):
        d[col] = pd.to_numeric(d[col], errors="coerce")
    d["total"] = d[["hook_1to3", "cliffhanger_1to3", "standalone_1to3"]].sum(axis=1, min_count=3)
    yes = lambda col: d[col].astype(str).str.strip().str.upper().str.startswith("Y")
    d["post"], d["boundary"] = yes("post_it_YN"), yes("boundary_ok_YN")
    res = {}
    for g, grp in d.groupby("group"):
        res[g] = {"n": int(len(grp)), "mean_hook": round(grp["hook_1to3"].mean(), 2),
                  "mean_cliffhanger": round(grp["cliffhanger_1to3"].mean(), 2),
                  "mean_standalone": round(grp["standalone_1to3"].mean(), 2),
                  "mean_total": round(grp["total"].mean(), 2), "post_rate": round(grp["post"].mean(), 3),
                  "boundary_ok_rate": round(grp["boundary"].mean(), 3)}
    top10 = d[(d["group"] == "llm_top")].sort_values("rank").head(10)
    res["precision_at_10"] = round(top10["post"].mean(), 3) if len(top10) else None
    llm = d[d["group"].isin(["llm_top", "llm_low"])].dropna(subset=["total"])
    res["spearman_priority_vs_human"] = _spearman(llm["priority"].tolist(), llm["total"].tolist())
    (run_dir / "human_metrics.json").write_text(json.dumps(res, indent=2))
    return res


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "sheet":
        print(make_review_sheet(sys.argv[2], sys.argv[3]))
    elif cmd == "score":
        print(json.dumps(score_review(sys.argv[2]), indent=2))
    else:
        print(__doc__)
