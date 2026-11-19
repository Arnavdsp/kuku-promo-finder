"""Stage 2: propose candidates.

- propose_episode: AI. One call per episode; the model answers with event-ID ranges, never timestamps.
- baseline_episode: no AI. Each episode's own written-in ending (microdrama episodes end on a cliffhanger).
- propose_montages: AI, optional. Joins already-validated clips across episodes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .llm import DailyLimitError, LLMConfig, LLMError, chat_json
from .parse import Episode, compact_transcript, fmt
from .prompts import (EPISODE_SYSTEM, EPISODE_USER, MONTAGE_SYSTEM, MONTAGE_USER, PROMPT_VERSION)


def show_context(eps: list[Episode], number: int, last_k: int = 10) -> str:
    """Episode 1 logline + the last k loglines before this episode. Bounded size for long shows."""
    prior = [e for e in eps if e.number < number]
    keep = ([prior[0]] if prior else []) + [e for e in prior[-last_k:] if prior and e is not prior[0]]
    if not keep:
        return "(this is the first episode)"
    return "\n".join(f"Ep{e.number}: {e.logline}" for e in keep)


def promos_from(parsed, key: str = "promos") -> list:
    """Models sometimes return a list ([{"promos": [...]}] or [promo, ...]) instead of {"promos": [...]}. Accept all."""
    if isinstance(parsed, dict):
        v = parsed.get(key, [])
        return v if isinstance(v, list) else []
    if isinstance(parsed, list):
        out = []
        for item in parsed:
            if isinstance(item, dict) and key in item:
                out += promos_from(item, key)
            elif isinstance(item, dict):
                out.append(item)
        return out
    return []


def _cache_key(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def _norm_promo(p: dict, episode: int, source: str) -> dict:
    segs = p.get("segments")
    if not segs and p.get("start_id"):                      # tolerate the flat {start_id, end_id} shape
        segs = [{"start_id": p.get("start_id"), "end_id": p.get("end_id")}]
    return {
        "source": source,
        "segments": [{"episode": episode, "start_id": str(s.get("start_id", "")).strip().upper(),
                      "end_id": str(s.get("end_id", "")).strip().upper()} for s in (segs or []) if isinstance(s, dict)],
        "hook": str(p.get("hook", "")), "hook_quote": str(p.get("hook_quote", "")),
        "cliffhanger": str(p.get("cliffhanger", "")), "cliffhanger_quote": str(p.get("cliffhanger_quote", "")),
        "why": str(p.get("why", "")), "context_needed": str(p.get("context_needed", "")),
        "scores": p.get("scores") if isinstance(p.get("scores"), dict) else {},
    }


def propose_episode(ep: Episode, eps: list[Episode], cfg: LLMConfig, cache_dir: Path, raw_dir: Path | None = None) -> tuple[list[dict], dict]:
    """Return (raw candidates, status). Cached per (prompt version, model, episode text, context)."""
    user = EPISODE_USER.format(show_context=show_context(eps, ep.number), number=ep.number,
                               duration=f"{ep.duration:.1f}s", logline=ep.logline or "(none)",
                               transcript=compact_transcript(ep, seconds=True))
    key = _cache_key(PROMPT_VERSION, cfg.base_url, cfg.model, EPISODE_SYSTEM, user)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cpath = cache_dir / f"ep{ep.number:03d}_{key}.json"
    status = {"episode": ep.number, "cached": False, "error": ""}
    if cpath.exists():
        rec = json.loads(cpath.read_text())
        status["cached"] = True
    else:
        try:
            parsed, raw = chat_json(cfg, EPISODE_SYSTEM, user)
        except DailyLimitError as ex:
            status["error"], status["daily_limit"] = str(ex)[:300], True
            return [], status
        except LLMError as ex:
            status["error"] = str(ex)[:300]
            return [], status
        rec = {"episode": ep.number, "model": cfg.model, "prompt_version": PROMPT_VERSION, "raw": raw, "parsed": parsed}
        cpath.write_text(json.dumps(rec, indent=2, ensure_ascii=False))
    if raw_dir:
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"ep{ep.number:03d}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False))
    promos = promos_from(rec["parsed"])
    out = [_norm_promo(p, ep.number, "llm") for p in promos if isinstance(p, dict)]
    status["proposed"] = len(out)
    return out, status


def baseline_episode(ep: Episode, target: float = 35.0) -> list[dict]:
    """No-AI baseline: the episode's own ending (written as a cliffhanger), starting on a dialogue line ~25-45s earlier."""
    ev = [e for e in ep.events if e.kind != "CAMERA"]
    if not ev:
        return []
    last = ev[-1]
    end_t = last.end
    starts = [e for e in ev if e.kind == "DIALOGUE" and 25.0 <= end_t - e.start <= 45.0]
    if not starts:
        starts = [e for e in ev if 20.0 <= end_t - e.start <= 60.0]
    if not starts:
        return []
    first = min(starts, key=lambda e: abs((end_t - e.start) - target))
    return [{
        "source": "baseline",
        "segments": [{"episode": ep.number, "start_id": first.id, "end_id": last.id}],
        "hook": "Episode-ending stretch (no AI)", "hook_quote": first.text,
        "cliffhanger": "The episode's own written ending", "cliffhanger_quote": last.text,
        "why": "Microdrama episodes are written to end on a cliffhanger, so the ending is a free promo candidate.",
        "context_needed": "", "scores": {},
    }]


def propose_montages(cands: list[dict], eps: list[Episode], cfg: LLMConfig, cache_dir: Path,
                     raw_dir: Path | None = None, top_n: int = 15) -> tuple[list[dict], dict]:
    """Optional: let the model join validated single-episode clips across episodes. Returns raw montages."""
    by_num = {e.number: e for e in eps}
    pool = [c for c in cands if len({s["episode"] for s in c["segments"]}) == 1][:top_n]
    pool.sort(key=lambda c: (c["segments"][0]["episode"], c["segments"][0]["start"]))   # story order helps the model
    blocks = []
    for c in pool:
        ep = by_num[c["segments"][0]["episode"]]
        lines = []
        for s in c["segments"]:
            for e in ep.events:   # only events fully inside the validated clip, so any pick stays inside it
                if e.kind not in ("DIALOGUE", "ACTION", "SOUND") or e.start < s["start"] - 0.01 or e.end > s["end"] + 0.01:
                    continue
                who = f'{e.speaker}: "{e.text}"' if e.kind == "DIALOGUE" else f"{e.kind}: {e.text}"
                lines.append(f"  {e.id} [{fmt(e.start)}-{fmt(e.end)}] {who}")
        blocks.append(f"CLIP {c['id']} (episode {ep.number}, {c['duration']:.0f}s)\n" + "\n".join(lines))
    premise = eps[0].logline if eps else ""
    user = MONTAGE_USER.format(premise=premise, clips="\n\n".join(blocks))
    key = _cache_key(PROMPT_VERSION, cfg.base_url, cfg.model, MONTAGE_SYSTEM, user)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cpath = cache_dir / f"montage_{key}.json"
    status = {"cached": cpath.exists(), "error": "", "pool": len(pool)}
    if cpath.exists():
        rec = json.loads(cpath.read_text())
    else:
        try:
            parsed, raw = chat_json(cfg, MONTAGE_SYSTEM, user, max_tokens=1500)
        except LLMError as ex:
            status["error"] = str(ex)[:300]
            return [], status
        rec = {"model": cfg.model, "prompt_version": PROMPT_VERSION, "raw": raw, "parsed": parsed}
        cpath.write_text(json.dumps(rec, indent=2, ensure_ascii=False))
    if raw_dir:
        (raw_dir / "montage.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False))
    clip_ep = {c["id"]: c["segments"][0]["episode"] for c in pool}
    out = []
    for m in promos_from(rec["parsed"], "montages"):
        if not isinstance(m, dict):
            continue
        segs = []
        for s in m.get("segments", []) or []:
            cid = str(s.get("clip_id", ""))
            segs.append({"episode": clip_ep.get(cid, -1), "clip_id": cid,
                         "start_id": str(s.get("start_id", "")).upper(), "end_id": str(s.get("end_id", "")).upper()})
        r = _norm_promo({**m, "segments": []}, -1, "montage")
        r["segments"] = segs
        out.append(r)
    status["proposed"] = len(out)
    return out, status
