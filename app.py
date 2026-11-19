"""Kuku Promo Finder - Streamlit review app.  Run:  streamlit run app.py

Uploads are processed in a temporary folder and deleted. Nothing is stored. The API key lives only in this session.
"""
import json
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from promo_finder.llm import PRESETS, LLMConfig, LLMError
from promo_finder.parse import fmt
from promo_finder.pipeline import run_pipeline, to_rows

st.set_page_config(page_title="Kuku Promo Finder", layout="wide")
st.title("Promo Finder")
st.caption("AI proposes clips · code checks timestamps, length, hook & cliffhanger · score ranks · **you approve**")

with st.sidebar:
    st.header("1 · Screenplays")
    files = st.file_uploader("Episode .txt files or one .zip", type=["txt", "zip"], accept_multiple_files=True)
    st.header("2 · Model")
    provider = st.selectbox("Provider", list(PRESETS), index=0, help="'none' = no AI, rules-only baseline")
    model = st.text_input("Model", value=PRESETS[provider]["model"])
    base_url = st.text_input("Base URL", value=PRESETS[provider]["base_url"], disabled=provider == "none")
    api_key = st.text_input("API key", type="password", disabled=provider in ("none", "ollama"),
                            help="Kept in this session only. Or set GROQ_API_KEY / LLM_API_KEY in the environment.")
    st.header("3 · Options")
    episodes = st.text_input("Episodes (blank = all)", placeholder="e.g. 1-10")
    montage = st.checkbox("Cross-episode montages (experimental, +1 call)")
    go = st.button("Find promos", type="primary", disabled=not files)

if go:
    try:
        cfg = LLMConfig.from_env(provider, model or None, base_url or None, api_key or None)
    except LLMError as ex:
        st.error(str(ex))
        st.stop()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "input"
        src.mkdir()
        zips = [f for f in files if f.name.lower().endswith(".zip")]
        if zips:
            src = tmp / zips[0].name
            src.write_bytes(zips[0].getvalue())
        else:
            for f in files:
                (src / f.name).write_bytes(f.getvalue())
        bar = st.progress(0.0, text="Starting…")
        try:
            res = run_pipeline(src, cfg, out_root=tmp / "out", episodes=episodes or None, montage=montage,
                               cache_dir=Path(tempfile.gettempdir()) / "promo_finder_cache",
                               progress=lambda i, n, ep: bar.progress(i / n, text=f"Episode {ep} ({i}/{n})"))
        except Exception as ex:  # show the real error to the user
            st.error(f"Run failed: {ex}")
            st.stop()
        bar.empty()
        res["files"] = {p.name: p.read_bytes() for p in Path(res["out_dir"]).glob("*.*")}
        st.session_state["res"] = res
        st.session_state["decisions"] = {}

res = st.session_state.get("res")
if not res:
    st.info("Upload episode screenplays, pick a model (or 'none' for the no-AI baseline), then **Find promos**.")
    st.stop()

eps = {e.number: e for e in res["episodes"]}
meta, metrics = res["meta"], res["metrics"]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Episodes", res["report"]["episodes"])
c2.metric("Promos shipped", metrics["shipped_promos"])
c3.metric("Tokens in / out", f"{meta['usage']['prompt_tokens']:,} / {meta['usage']['completion_tokens']:,}")
c4.metric("Run time", f"{meta['elapsed_s']} s")


def detail(c: dict, key: str):
    comp = c.get("components", {})
    left, right = st.columns([3, 2])
    with left:
        for s in c["segments"]:
            ep = eps[s["episode"]]
            st.markdown(f"**Episode {s['episode']} · {fmt(s['start'])} → {fmt(s['end'])}** "
                        f"({s['end'] - s['start']:.1f}s, events {s['start_id']}–{s['end_id']})")
            lines = []
            for e in ep.events:
                if e.kind == "CAMERA" or e.end < s["start"] - 5 or e.start > s["end"] + 5:
                    continue
                inside = e.start >= s["start"] - 0.01 and e.end <= s["end"] + 0.01
                who = f"**{e.speaker}**: “{e.text}”" if e.kind == "DIALOGUE" else f"*{e.kind.lower()}: {e.text}*"
                mark = "🎣 " if e.id == c["segments"][0]["start_id"] and s is c["segments"][0] else (
                    "🪝 " if e.id == c["segments"][-1]["end_id"] and s is c["segments"][-1] else "")
                txt = f"`{fmt(e.start)}` {mark}{who}"
                lines.append(txt if inside else f"<span style='opacity:.4'>{txt} (outside clip)</span>")
            st.markdown("  \n".join(lines), unsafe_allow_html=True)
    with right:
        st.markdown(f"**Priority score {c.get('priority')}** — {c.get('post_reason', '')}")
        st.bar_chart(pd.DataFrame({"component": ["hook", "cliffhanger", "standalone", "funnel"],
                                   "value": [comp.get(k, 0) for k in ("hook", "cliffhanger", "standalone", "funnel")]}),
                     x="component", y="value", height=180)
        st.markdown(f"**Hook:** {c.get('hook', '')}  \n**Cliffhanger:** {c.get('cliffhanger', '')}  \n"
                    f"**Why:** {c.get('why', '')}")
        if c.get("context_needed"):
            st.markdown(f"**A stranger may miss:** {c['context_needed']}")
        if c.get("flags"):
            st.warning("Check: " + ", ".join(c["flags"]) + (f" (names: {', '.join(c['absent_names'])})" if c.get("absent_names") else ""))
        d = st.session_state["decisions"].get(c["id"], {})
        choice = st.radio("Decision", ["–", "Accept", "Reject"], key=f"d_{key}",
                          index=["–", "Accept", "Reject"].index(d.get("decision", "–")), horizontal=True)
        note = st.text_input("Note", value=d.get("note", ""), key=f"n_{key}")
        st.session_state["decisions"][c["id"]] = {"decision": choice, "note": note}


tabs = st.tabs(["Ranked promos", "Montages (experimental)", "Baseline (no AI)", "Rejected", "Run metrics"])
for tab, items, tag in ((tabs[0], res["ranked"], "r"), (tabs[1], res["montages"], "m"), (tabs[2], res["baseline"], "b")):
    with tab:
        if not items:
            st.write("Nothing here for this run.")
            continue
        df = pd.DataFrame(to_rows(items))[["rank", "id", "segments", "duration_s", "priority", "hook_line", "cliffhanger_line", "flags"]]
        st.dataframe(df, hide_index=True, width="stretch")
        top_n = st.slider("Show details for top", 1, len(items), min(10, len(items)), key=f"s_{tag}")
        for c in items[:top_n]:
            with st.expander(f"#{c['rank']} · {c['id']} · {c['duration']}s · score {c['priority']}"
                             + (" · ⚠ " + ", ".join(c["flags"]) if c.get("flags") else "")):
                detail(c, f"{tag}_{c['id']}")
with tabs[3]:
    rej = res["rejected"]
    st.write(f"{len(rej)} candidates rejected or de-duplicated by the code checks.")
    if rej:
        st.dataframe(pd.DataFrame([{"id": c["id"], "source": c["source"],
                                    "reason": c.get("reject_reason") or f"{c.get('dropped_as')} of {c.get('duplicate_of')}",
                                    "segments": json.dumps(c.get("segments"))} for c in rej]), hide_index=True,
                     width="stretch")
with tabs[4]:
    st.json(metrics)
    st.json({k: meta[k] for k in ("llm", "prompt_version", "usage", "elapsed_s", "llm_score_spread", "weight_sensitivity")})

st.divider()
dec = pd.DataFrame(to_rows(res["ranked"]))
dec["decision"] = dec["id"].map(lambda i: st.session_state["decisions"].get(i, {}).get("decision", "–"))
dec["note"] = dec["id"].map(lambda i: st.session_state["decisions"].get(i, {}).get("note", ""))
a, b = st.columns(2)
a.download_button("⬇ Ranked promos + your decisions (CSV)", dec.to_csv(index=False), "promos_with_decisions.csv")
b.download_button("⬇ Full run (JSON)", json.dumps({"meta": meta, "metrics": metrics, "ranked": res["ranked"],
                                                    "montages": res["montages"]}, ensure_ascii=False, default=str),
                  "promo_run.json")
