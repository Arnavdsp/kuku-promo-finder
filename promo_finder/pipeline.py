"""Orchestrates the stages. Shared by run.py (CLI) and app.py (Streamlit)."""
from __future__ import annotations

import json
import re
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd

from . import evaluate
from .candidates import baseline_episode, propose_episode, propose_montages
from .llm import LLMConfig
from .parse import fmt, load_show, parse_report
from .prompts import PROMPT_VERSION
from .rank import apply_scores, score_spread, select, sensitivity
from .validate import dedupe, known_names, validate


def _one_sentence(s: str) -> str:
    s = s.strip()
    m = re.match(r"(.+?[.!?])(\s|$)", s)
    return m.group(1) if m else s


def parse_range(spec: str | None, available: list[int]) -> list[int]:
    if not spec:
        return available
    out = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out |= set(range(int(a), int(b) + 1))
        elif part.strip():
            out.add(int(part))
    return [n for n in available if n in out]


def load_input(path: str | Path):
    """Folder of .txt files, or a .zip of them. Returns (episodes, tempdir-or-None)."""
    path = Path(path)
    tmp = None
    if path.suffix.lower() == ".zip":
        tmp = tempfile.TemporaryDirectory()
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.lower().endswith(".txt") and not name.startswith("__MACOSX"):
                    target = Path(tmp.name) / Path(name).name
                    target.write_bytes(z.read(name))
        path = Path(tmp.name)
    eps = load_show(path)
    return eps, tmp


def seg_str(c: dict) -> str:
    return "; ".join(f"Ep{s['episode']} {fmt(s['start'])}-{fmt(s['end'])} [{s['start_id']}-{s['end_id']}]" for s in c["segments"])


def to_rows(cands: list[dict]) -> list[dict]:
    rows = []
    for c in cands:
        comp = c.get("components", {})
        sc = c.get("scores", {}) or {}
        rows.append({
            "rank": c.get("rank"), "id": c["id"], "source": c["source"],
            "episodes": ",".join(str(s["episode"]) for s in c["segments"]),
            "segments": seg_str(c), "duration_s": c.get("duration"),
            "hook": c.get("hook", ""), "hook_line": c.get("hook_text", ""),
            "cliffhanger": c.get("cliffhanger", ""), "cliffhanger_line": c.get("cliffhanger_text", ""),
            "why": c.get("why", ""), "context_needed": c.get("context_needed", ""),
            "priority": c.get("priority"), "post_reason": c.get("post_reason", ""),
            "c_hook": comp.get("hook"), "c_cliffhanger": comp.get("cliffhanger"),
            "c_standalone": comp.get("standalone"), "c_funnel": comp.get("funnel"),
            "llm_hook": sc.get("hook"), "llm_cliffhanger": sc.get("cliffhanger"), "llm_standalone": sc.get("standalone"),
            "flags": ",".join(c.get("flags", [])),
        })
    return rows


def run_pipeline(input_path: str | Path, cfg: LLMConfig, out_root: str | Path = "outputs",
                 episodes: str | None = None, montage: bool = False, cache_dir: str | Path | None = None,
                 price_in: float | None = None, price_out: float | None = None, progress=None,
                 run_name: str | None = None) -> dict:
    t0 = time.time()
    eps, tmp = load_input(input_path)
    try:
        report = parse_report(eps)
        if not eps:
            raise ValueError("No .txt screenplay files found in the input.")
        if report["empty_files"]:
            raise ValueError(f"Parser found 0 events in: {report['empty_files']}. Screenplay format differs from expected.")
        ep_by_num = {e.number: e for e in eps}
        chosen = parse_range(episodes, [e.number for e in eps])
        names = known_names(eps)
        out_root = Path(out_root)
        cache_dir = Path(cache_dir) if cache_dir else out_root / "cache"
        stamp = run_name or f"run_{datetime.now():%Y%m%d_%H%M%S}_{re.sub(r'[^A-Za-z0-9.-]+', '-', cfg.model)}"
        out = out_root / stamp
        raw_dir = out / "raw_llm"
        out.mkdir(parents=True, exist_ok=True)

        llm_raw, statuses, base_raw = [], [], []
        for i, n in enumerate(chosen, 1):
            ep = ep_by_num[n]
            if cfg.enabled and any(x.get("daily_limit") for x in statuses):
                statuses.append({"episode": n, "error": "skipped: daily quota reached - rerun later, cache resumes"})
            elif cfg.enabled:
                props, st = propose_episode(ep, eps, cfg, cache_dir, raw_dir)
                for j, p in enumerate(props, 1):
                    p["id"] = f"L{n:03d}-{j}"
                llm_raw += props
                statuses.append(st)
            for p in baseline_episode(ep):
                p["id"] = f"B{n:03d}"
                base_raw.append(p)
            if progress:
                progress(i, len(chosen), n)

        n_eps = len(eps)
        llm_checked = [validate(p, ep_by_num, names) for p in llm_raw]
        base_checked = [validate(p, ep_by_num, names) for p in base_raw]
        llm_valid = apply_scores([c for c in llm_checked if c["status"] == "valid"], n_eps)
        base_valid = apply_scores([c for c in base_checked if c["status"] == "valid"], n_eps)
        llm_kept, llm_dups = dedupe(llm_valid, ep_by_num)
        base_kept, _ = dedupe(base_valid, ep_by_num)
        llm_ranked = select(llm_kept)
        base_ranked = select(base_kept)
        for c in llm_ranked + base_ranked:
            c["why"] = _one_sentence(c.get("why", ""))
        sens = sensitivity(llm_kept, n_eps) if len(llm_kept) >= 10 else None
        llm_ranked = sorted(llm_kept, key=lambda c: c["rank"])

        mont_ranked, mont_rejected, mont_status = [], [], None
        if montage and cfg.enabled and llm_ranked:
            pool = {c["id"]: c for c in llm_ranked[:15]}
            raw_m, mont_status = propose_montages(llm_ranked, eps, cfg, cache_dir, raw_dir)
            for j, m in enumerate(raw_m, 1):
                m["id"] = f"M{j}"
            checked = [validate(m, ep_by_num, names, clip_pool=pool) for m in raw_m]
            mont_rejected = [c for c in checked if c["status"] == "rejected"]
            mont_ranked = select(apply_scores([c for c in checked if c["status"] == "valid"], n_eps), top_k=10, max_per_ep=99)
            for c in mont_ranked:
                c["why"] = _one_sentence(c.get("why", ""))

        primary = llm_ranked if cfg.enabled else base_ranked
        rejected = [c for c in llm_checked if c["status"] == "rejected"] + llm_dups + mont_rejected

        # ---- write outputs -----------------------------------------------------------------
        pd.DataFrame(to_rows(primary)).to_csv(out / "promos_ranked.csv", index=False)
        (out / "promos_ranked.json").write_text(json.dumps(primary, indent=2, ensure_ascii=False))
        pd.DataFrame(to_rows(base_ranked)).to_csv(out / "baseline_ranked.csv", index=False)
        (out / "baseline_ranked.json").write_text(json.dumps(base_ranked, indent=2, ensure_ascii=False))
        if mont_ranked or mont_rejected:
            pd.DataFrame(to_rows(mont_ranked)).to_csv(out / "montages_ranked.csv", index=False)
            (out / "montages_ranked.json").write_text(json.dumps(mont_ranked, indent=2, ensure_ascii=False))
        (out / "rejected.json").write_text(json.dumps(rejected, indent=2, ensure_ascii=False))
        (out / "parse_report.json").write_text(json.dumps(report, indent=2))

        elapsed = round(time.time() - t0, 1)
        usage = dict(cfg.usage)
        cost = None
        if price_in is not None and price_out is not None:
            cost = round(usage["prompt_tokens"] / 1e6 * price_in + usage["completion_tokens"] / 1e6 * price_out, 4)
        meta = {
            "run": stamp, "created": datetime.now().isoformat(timespec="seconds"), "input": Path(input_path).name,
            "llm": cfg.describe(), "prompt_version": PROMPT_VERSION, "episodes_processed": chosen,
            "usage": usage, "elapsed_s": elapsed, "price_per_1M": {"in": price_in, "out": price_out}, "cost_usd": cost,
            "episode_status": statuses, "montage_status": mont_status,
            "llm_score_spread": score_spread(llm_valid), "weight_sensitivity": sens,
        }
        (out / "run_meta.json").write_text(json.dumps(meta, indent=2))
        metrics = evaluate.auto_metrics(out, llm_raw=llm_raw, llm_checked=llm_checked, llm_kept=llm_kept,
                                        llm_dups=llm_dups, ranked=primary, chosen=chosen, statuses=statuses,
                                        llm_enabled=cfg.enabled, ep_by_num=ep_by_num)
        return {"out_dir": str(out), "ranked": primary, "baseline": base_ranked, "montages": mont_ranked,
                "rejected": rejected, "meta": meta, "metrics": metrics, "report": report, "episodes": eps}
    finally:
        if tmp is not None:
            tmp.cleanup()
