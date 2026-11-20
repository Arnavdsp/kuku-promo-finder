# 3-minute walkthrough: script outline (record in your own voice)

**0:00–0:20 · Problem.** "Today an editor watches every episode to pick promos. That's about 100 minutes of footage for this show alone. I built a tool that reads the screenplays and hands the editor a ranked shortlist with the reasons attached."

**0:20–0:50 · Run it.** Terminal: `python run.py --input <pack> --episodes 1-3`. Point out:
- it's one LLM call per episode;
- the model answers with event IDs, never timestamps, so it can't invent times;
- the summary shows validity, the reasons for rejections, and the token cost.

**0:50–1:50 · The app.** Open the private Hugging Face Space (runs in the browser, nothing stored), paste a Groq key and drop the episodes.
- The ranked table shows segments with timestamps.
- Open #1 and show the script excerpt, with the hook and cliffhanger lines marked.
- Show the score breakdown bar chart and the "post first because…" line.
- Open a flagged clip (e.g. a `resolved_ending` flag on a reveal line) and explain that this is where the rule and the model disagree, so the editor decides.
- Accept or reject, then export the CSV.
- Show the Rejected tab: the code throws out what the model gets wrong.
- Click "Preview promo": the storyboard of the real lines always shows; the LTX video clip appears if free GPU time is left.

**1:50–2:20 · Swap the model.** Show `--provider custom --base-url … --model …`. No code changes are needed, and the cache makes reruns free.

**2:20–2:50 · Quality and failures.** Cover:
- metrics: 100% valid IDs, 86.8% matching quotes, coverage 50 of 50, 0 clips outside 20–60s;
- the comparison with the no-AI baseline (fewer weak openings: 35% vs 48%);
- your human-review numbers;
- the 3 failures: reveals flagged as resolved, inflated self-scores, and text can't see the picture.

**2:50–3:00 · Cost.** About $0.001 per episode at Groq's paid price, or about $7 a month at 150 shows a month. The real saving is the editor's time.
