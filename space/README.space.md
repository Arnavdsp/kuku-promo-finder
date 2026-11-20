---
title: Promo Finder
emoji: 🎬
colorFrom: red
colorTo: yellow
sdk: static
app_file: index.html
short_description: Find and rank hook-to-cliffhanger microdrama promos
pinned: false
---

# Promo Finder

Upload a show's episode screenplays (.txt per episode, or one .zip). The page finds 20–60s promo moments that open on a hook and end on a cliffhanger, checks every timestamp and quote in code, ranks them, and lets you accept or reject each one.

- Everything runs in your browser (Python via Pyodide). Nothing is uploaded to this Space or stored.
- AI: paste your own Groq API key (kept in the tab only). With no key, it runs the rules-only baseline.
- "Preview promo" always shows a storyboard of the real lines. It also tries a short AI video prototype on the free LTX-2.3 GPU Space; if free GPU time is used up, only the storyboard shows.
