"""Stage 4: transparent priority score + diverse selection. No AI.

The weights are product judgement, not science. They are configurable, every component is shown,
and sensitivity() reports how much the top 10 moves when weights shift.
"""
from __future__ import annotations

import statistics

WEIGHTS = {"hook": 0.35, "cliffhanger": 0.30, "standalone": 0.25, "funnel": 0.10}
FLAG_PENALTY, FLAG_PENALTY_CAP = 0.05, 0.15
LLM_SHARE = 0.7          # share of each sub-score from the LLM's anchored 1-5 rating; the rest from rule checks


def _llm(c: dict, key: str) -> float | None:
    try:
        v = float(c.get("scores", {}).get(key))
        return min(max((v - 1) / 4, 0.0), 1.0)
    except (TypeError, ValueError):
        return None


def components(c: dict, n_eps: int) -> dict:
    rule = c.get("rule", {})
    out = {}
    for key, rk in (("hook", "hook_ok"), ("cliffhanger", "cliff_ok"), ("standalone", "standalone_ok")):
        r = 1.0 if rule.get(rk) else 0.0
        l = _llm(c, key)
        out[key] = r if l is None else LLM_SHARE * l + (1 - LLM_SHARE) * r   # baseline has no LLM score -> rules only
    ep = min(s["episode"] for s in c["segments"])
    out["funnel"] = 1.0 if n_eps <= 1 else 1 - (ep - 1) / (n_eps - 1)
    return {k: round(v, 3) for k, v in out.items()}


def score(c: dict, n_eps: int, weights: dict | None = None) -> float:
    w = weights or WEIGHTS
    comp = c["components"] if "components" in c else components(c, n_eps)
    penalty = min(FLAG_PENALTY * len([f for f in c.get("flags", []) if f != "low_dialogue"]), FLAG_PENALTY_CAP)
    return round(sum(w[k] * comp[k] for k in w) - penalty, 4)


def apply_scores(cands: list[dict], n_eps: int, weights: dict | None = None) -> list[dict]:
    for c in cands:
        c["components"] = components(c, n_eps)
        c["priority"] = score(c, n_eps, weights)
    return sorted(cands, key=lambda c: -c["priority"])


def select(cands: list[dict], top_k: int = 10, max_per_ep: int = 2) -> list[dict]:
    """Greedy by priority; at most max_per_ep per episode and no repeated hook event in the top_k. Rest follow in order."""
    top, rest, per_ep, hooks = [], [], {}, set()
    for c in cands:
        ep = c["segments"][0]["episode"]
        hk = (ep, c["segments"][0]["start_id"])
        if len(top) < top_k and per_ep.get(ep, 0) < max_per_ep and hk not in hooks:
            top.append(c)
            per_ep[ep] = per_ep.get(ep, 0) + 1
            hooks.add(hk)
        else:
            rest.append(c)
    ranked = top + rest
    for i, c in enumerate(ranked, 1):
        c["rank"] = i
        c["post_reason"] = explain(c)
    return ranked


def explain(c: dict) -> str:
    comp = c["components"]
    labels = {"hook": "strong opening hook", "cliffhanger": "unresolved ending", "standalone": "easy for a stranger to follow",
              "funnel": "early episode (no spoilers, viewer lands near Ep 1)"}
    best = sorted(("hook", "cliffhanger", "standalone", "funnel"), key=lambda k: -comp[k] * WEIGHTS[k])[:2]
    reason = " + ".join(labels[k] for k in best)
    if c.get("flags"):
        reason += f"; check: {', '.join(c['flags'])}"
    return reason


def sensitivity(cands: list[dict], n_eps: int, top_k: int = 10, delta: float = 0.1) -> dict:
    """For each weight +/- delta (renormalised), how many of the top_k stay in the top_k?"""
    base = [c["id"] for c in select(apply_scores([dict(c) for c in cands], n_eps), top_k)[:top_k]]
    res = {}
    for k in WEIGHTS:
        for sign in (+1, -1):
            w = dict(WEIGHTS)
            w[k] = max(0.0, w[k] + sign * delta)
            tot = sum(w.values())
            w = {kk: v / tot for kk, v in w.items()}
            alt = [c["id"] for c in select(apply_scores([dict(c) for c in cands], n_eps, w), top_k)[:top_k]]
            res[f"{k}{'+' if sign > 0 else '-'}{delta}"] = len(set(base) & set(alt))
    # restore ranking/fields for the real weights
    select(apply_scores(cands, n_eps), top_k)
    return {"top_k": top_k, "overlap_with_base": res, "min_overlap": min(res.values()) if res else None}


def score_spread(cands: list[dict]) -> dict:
    out = {}
    for k in ("hook", "cliffhanger", "standalone"):
        vals = [float(c["scores"][k]) for c in cands if isinstance(c.get("scores"), dict) and str(c["scores"].get(k, "")).replace(".", "", 1).isdigit()]
        out[k] = {"n": len(vals), "mean": round(statistics.mean(vals), 2) if vals else None,
                  "stdev": round(statistics.pstdev(vals), 2) if len(vals) > 1 else None}
    return out
