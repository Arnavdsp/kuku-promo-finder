"""Where does the tool pick badly?  Code gathers the evidence from a run; any LLM writes the analysis.

failure_evidence(res)      -> dict of counts + concrete examples (deterministic, no AI)
failure_report(res, cfg)   -> {"evidence": ..., "analysis": {...} | None, "source": "llm" | "evidence-only"}

The LLM sees ONLY this evidence (numbers + a few short real lines) and must cite promo IDs from it,
so the write-up stays grounded in what actually happened in this run.
"""
from __future__ import annotations

from collections import Counter

from .llm import LLMConfig, LLMError, chat_json

FLAG_MEANING = {
    "resolved_ending": "the last line may answer the tension instead of leaving it open",
    "weak_open": "something happens in the first 3s, but it isn't a shock line or a physical action",
    "slow_open": "nothing happens in the first 3s",
    "opens_on_pronoun": "first line starts with he/she/they and no name is shown",
    "unexplained_names": "a character is named but never appears in the clip",
    "quote_mismatch": "the model's quote didn't match the real screenplay line",
    "recap": "mostly repeats the previous episode's ending",
    "low_dialogue": "fewer than 2 spoken lines",
    "auto_trimmed_start": "the model's clip was too long and code moved the start",
}


def _short(s, n: int = 110) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def failure_evidence(res: dict, top_k: int = 15) -> dict:
    ranked, baseline, rejected = res.get("ranked", []), res.get("baseline", []), res.get("rejected", [])
    metrics, meta = res.get("metrics", {}), res.get("meta", {})
    llm = metrics.get("llm", {})
    top = ranked[:top_k]
    flags_top = Counter(f for c in top for f in c.get("flags", []))
    flags_all = Counter(f for c in ranked for f in c.get("flags", []))
    examples = {}
    for c in ranked:
        for f in c.get("flags", []):
            ex = examples.setdefault(f, [])
            if len(ex) < 2:
                item = {"id": c["id"], "rank": c.get("rank"), "episode": c["segments"][0]["episode"],
                        "hook_line": _short(c.get("hook_text")), "last_line": _short(c.get("cliffhanger_text"))}
                if f == "quote_mismatch":     # show both ends: the real line at that ID vs what the model quoted
                    g = c.get("grounding", {})
                    item["mismatch"] = [{"which": w, "real_line": _short(c.get(real)), "model_quoted": _short(c.get(q)), "match": g.get(w)}
                                        for w, real, q in (("hook", "hook_text", "hook_quote"), ("cliffhanger", "cliffhanger_text", "cliffhanger_quote"))
                                        if g.get(w) is not None and g[w] < 0.85]
                if f == "unexplained_names":
                    item["names"] = c.get("absent_names", [])
                ex.append(item)
    rej_reasons = Counter(c.get("reject_reason") or c.get("dropped_as") or c.get("status") for c in rejected)
    rej_examples = [{"id": c.get("id"), "reason": c.get("reject_reason") or c.get("dropped_as"),
                     "duplicate_of": c.get("duplicate_of"), "hook": _short(c.get("hook"), 80)} for c in rejected[:4]]

    def share(cands, flagset):
        return round(sum(1 for c in cands if set(c.get("flags", [])) & flagset) / len(cands), 2) if cands else None

    spread = meta.get("llm_score_spread") or {}
    cov = metrics.get("episode_coverage", {})
    return {
        "promos_ranked": len(ranked), "top_k": len(top),
        "flag_counts_top_k": dict(flags_top), "flag_counts_all": dict(flags_all),
        "flag_meaning": {f: FLAG_MEANING.get(f, f) for f in flags_all},
        "flag_examples": examples,
        "rejected_or_dropped": dict(rej_reasons), "rejected_examples": rej_examples,
        "episodes_without_promo": cov.get("no_promo", []), "episodes_failed": llm.get("episodes_failed", []),
        "quote_accuracy": llm.get("quote_accuracy"), "validity_rate": llm.get("validity_rate"),
        "duplicate_rate": llm.get("duplicate_rate"),
        "weak_or_slow_open_share": {"ai_top": share(top, {"weak_open", "slow_open"}),
                                    "baseline_top": share(baseline[:top_k], {"weak_open", "slow_open"})},
        "resolved_ending_share": {"ai_top": share(top, {"resolved_ending"}), "baseline_top": share(baseline[:top_k], {"resolved_ending"})},
        "llm_self_score_spread": spread,
        "always_true_limits": ["works from screenplay text only: looks, silences and visual beats are invisible",
                               "screenplay timestamps are not the final edit (about ±0.5s)",
                               "character aliases (e.g. a role name for a named character) confuse the name checks"],
    }


SYSTEM = """You are a candid QA reviewer of an AI tool that picks 20-60s promo clips (hook -> cliffhanger) from microdrama screenplays.
You get EVIDENCE from one run: counts of warning flags, rejections, comparisons with a no-AI baseline, and a few real examples.
What the tool ALREADY does (do not suggest these as fixes): the model answers with line IDs and code looks up real times;
code rejects unknown IDs and clips outside 20-60s; it compares the model's quote with the real line at that ID (quote_mismatch)
and always shows/ships the REAL line, never the model's quote; it flags the warnings listed in flag_meaning; it merges duplicates.
Write where the tool picks badly. Rules:
- Use ONLY the evidence. Every number you state must appear in it. Cite promo IDs from the examples.
- Rank patterns by how much they would mislead an editor (most harmful first). 3-5 patterns.
- Be specific and plain; no hype, no generic AI caveats beyond what the evidence shows.
Return JSON only:
{"summary": "<2 sentences: overall, where it is weakest>",
 "patterns": [{"title": "...", "how_often": "<from the numbers>", "example": "<promo id + the real line>",
               "why": "<likely cause in the tool>", "editor_check": "<what a human should check>", "fix": "<one concrete improvement>"}],
 "trust": "<1 sentence: what the editor can trust as-is>"}"""


def failure_report(res: dict, cfg: LLMConfig | None = None) -> dict:
    ev = failure_evidence(res)
    if cfg is None or not cfg.enabled or not res.get("ranked"):
        return {"evidence": ev, "analysis": None, "source": "evidence-only"}
    import json
    try:
        parsed, _ = chat_json(cfg, SYSTEM, "EVIDENCE:\n" + json.dumps(ev, ensure_ascii=False, indent=1), max_tokens=1800)
        if isinstance(parsed, dict) and parsed.get("patterns"):
            return {"evidence": ev, "analysis": parsed, "source": "llm", "model": cfg.model}
        return {"evidence": ev, "analysis": None, "source": "evidence-only", "error": "model returned no patterns"}
    except LLMError as ex:
        return {"evidence": ev, "analysis": None, "source": "evidence-only", "error": str(ex)[:200]}
