# Promo Finder: microdrama screenplay → ranked 20–60s promos

Built for Kuku's AI Generalist assignment (Problem 1). It reads a folder of episode screenplays and returns ranked promo clips.
Each clip comes with an episode, start/end timestamps for every segment, a hook, a cliffhanger, a one-line reason, a priority score and warnings.

**AI proposes → code checks → score ranks → a human approves.**

| Step | Who | What |
|---|---|---|
| Parse | code | screenplay → timed events with IDs (`E001…`) |
| Propose | **LLM**, 1 call per episode | 0–5 promos as **event-ID ranges** (never timestamps, so times can't be invented) |
| Check | code | map IDs to real times · no mid-line cuts · 20–60s · hook within 3s · unresolved ending · unknown names · recap · quote grounding · dedupe |
| Rank | code | `0.35 hook + 0.30 cliffhanger + 0.25 standalone + 0.10 early-episode − flag penalty` (weights are a judgement call; the sensitivity check is in `run_meta.json`) |
| Review | human | Streamlit app: read the excerpt, see why it ranked, accept or reject, export CSV |
| Baseline | code, no AI | each episode's own written ending, for comparison |

## Run

```bash
pip install -r requirements.txt
export GROQ_API_KEY=...            # free key: console.groq.com

python run.py --input path/to/show_folder            # or a .zip of episode .txt files
python run.py --input show --episodes 1-5             # quick test
python run.py --input show --montage                  # + cross-episode montages (1 extra call)
python run.py --input show --provider none            # no AI at all (baseline only)
streamlit run app.py                                  # review UI
python -m pytest -q tests
```

### Swap the model (no code changes)
Any OpenAI-compatible endpoint works:
```bash
python run.py --input show --provider custom --base-url https://YOUR-ENDPOINT/v1 --model YOUR-MODEL   # key: LLM_API_KEY
python run.py --input show --provider openrouter --model <model>      # OPENROUTER_API_KEY
python run.py --input show --provider ollama --model llama3.1:8b      # local
```
Or use env vars: `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`.
Results are cached per (prompt version, model, episode), so an interrupted run resumes for free.
Groq's free tier for `openai/gpt-oss-120b` allows about 200k tokens/day and 8k/min, and the tool backs off on 429.

## Outputs (`outputs/<run>/`)
- `promos_ranked.csv/json`: rank, `segments` (e.g. `Ep3 00:11.30-00:42.10 [E004-E017]`), duration, hook and cliffhanger (description + the actual screenplay line), why, priority and its components, LLM scores, flags
- `montages_ranked.*`: cross-episode promos (experimental)
- `baseline_ranked.*`: the no-AI baseline
- `rejected.json`: every AI proposal the checks rejected or de-duplicated, with the reason
- `raw_llm/`: unedited model responses
- `metrics.json`, `run_meta.json`: validity, grounding, duplicates, coverage, tokens, time, cost, score spread, weight sensitivity

## Human evaluation
```bash
python -m promo_finder.evaluate sheet outputs/<run> path/to/show   # blinded, shuffled review_sheet.csv
# rate each item 1-3 for hook / cliffhanger / standalone, plus post_it Y/N and boundary_ok Y/N
python -m promo_finder.evaluate score outputs/<run>                # precision@10, LLM vs baseline, Spearman
```

## Input format
The parser expects each `.txt` file to have an `EPISODE NUMBER/LOGLINE` header, then blocks like `**SPEAKER (Tone):**` / `**ACTION:**` / `**CAMERA:**` / `**SOUND FX:**`, each followed by `Start: (mm:ss.xx) | End: (mm:ss.xx)`.
If any file yields 0 events, the run stops and names the file.

## Known limits
- It works from screenplay text: a look or a silence that plays well on screen is invisible to it.
- Screenplay timestamps are not the final edit; expect about ±0.5s adjustment by an editor.
- Character aliases ("THE FORMER GROOM" = Gerald) can confuse the standalone check.
- Scores rank candidates; they are not measured engagement. Only real posting data (CTR, watch-through, installs) can validate them.
- Screenplays and run outputs are confidential and git-ignored; share outputs privately.
