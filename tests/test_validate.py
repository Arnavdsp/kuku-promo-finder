"""Validator tests on a tiny synthetic episode (no confidential data)."""
from pathlib import Path

from promo_finder.parse import parse_episode
from promo_finder.validate import dedupe, validate
from promo_finder.candidates import baseline_episode
from promo_finder.rank import apply_scores

SCRIPT = """EPISODE NUMBER: Episode {n}
EPISODE LOGLINE: Test logline.
CONTINUITY NOTES: none

--- SCRIPT START ---

SCENE 01
Scene heading: INT. HALL - DAY
Scene start: 00:00.00
Scene end: 01:10.00

Actions and dialogue:

**ACTION:**
Start: (00:00.50) | End: (00:02.00)
Anna slaps Ben across the face.

**ANNA (Furious):**
Start: (00:02.00) | End: (00:04.00)
"How dare you lie to me!"

**BEN (Cold):**
Start: (00:10.00) | End: (00:14.00)
"You will regret this, Anna."

**ANNA (Shaken):**
Start: (00:30.00) | End: (00:36.00)
"Who are you really?"

**BEN (Quiet):**
Start: (00:50.00) | End: (00:55.00)
"Someone you should fear."

**SOUND FX:**
Start: (01:05.00) | End: (01:10.00)
Ominous drone swells to a cliffhanger peak.

--- SCRIPT END ---
"""


def _ep(tmp_path: Path, n: int = 1):
    p = tmp_path / f"episode-{n:03d}.txt"
    p.write_text(SCRIPT.format(n=n))
    return parse_episode(p)


def _raw(ep, a, b, source="llm"):
    return {"source": source, "id": "X", "segments": [{"episode": ep.number, "start_id": a, "end_id": b}],
            "hook_quote": "", "cliffhanger_quote": "", "scores": {"hook": 5, "cliffhanger": 5, "standalone": 4}}


def test_ids_and_valid_clip(tmp_path):
    ep = _ep(tmp_path)
    assert [e.id for e in ep.events] == ["E001", "E002", "E003", "E004", "E005", "E006"]
    c = validate(_raw(ep, "E001", "E004"), {1: ep}, {"ANNA", "BEN"})
    assert c["status"] == "valid", c["reject_reason"]
    assert c["segments"][0]["start"] == 0.5 and c["segments"][0]["end"] == 36.0
    assert "slow_open" not in c["flags"] and "weak_open" not in c["flags"]
    assert "resolved_ending" not in c["flags"]          # ends on a question


def test_unknown_id_and_reversed_rejected(tmp_path):
    ep = _ep(tmp_path)
    assert validate(_raw(ep, "E001", "E099"), {1: ep}, set())["reject_reason"] == "unknown_event_id"
    assert validate(_raw(ep, "E004", "E001"), {1: ep}, set())["reject_reason"] == "start_after_end"


def test_duration_rules(tmp_path):
    ep = _ep(tmp_path)
    assert validate(_raw(ep, "E001", "E003"), {1: ep}, set())["reject_reason"] == "too_short"     # 13.5s
    c = validate(_raw(ep, "E001", "E006"), {1: ep}, set())                                       # 69.5s -> repaired
    assert c["status"] == "valid" and c["trimmed"] and "auto_trimmed_start" in c["flags"]
    assert 20 <= c["duration"] <= 60 and c["segments"][0]["end_id"] == "E006"                   # cliffhanger kept


def test_quote_mismatch_flag(tmp_path):
    ep = _ep(tmp_path)
    r = _raw(ep, "E002", "E004")
    r["hook_quote"], r["cliffhanger_quote"] = "Totally invented line", "Who are you really?"
    assert "quote_mismatch" in validate(r, {1: ep}, set())["flags"]


def test_dedupe_and_baseline(tmp_path):
    ep = _ep(tmp_path)
    a = validate({**_raw(ep, "E001", "E004"), "id": "A"}, {1: ep}, set())
    b = validate({**_raw(ep, "E002", "E004"), "id": "B"}, {1: ep}, set())
    kept, dropped = dedupe(apply_scores([a, b], 1), {1: ep})
    assert len(kept) == 1 and dropped[0]["dropped_as"] == "overlap"
    base = baseline_episode(ep)
    c = validate({**base[0], "id": "B1"}, {1: ep}, set())
    assert c["status"] == "valid" and 20 <= c["duration"] <= 60


def test_promos_from_accepts_list_shapes():
    from promo_finder.candidates import promos_from
    p = {"segments": [{"start_id": "E001", "end_id": "E004"}]}
    assert promos_from({"promos": [p]}) == [p]
    assert promos_from([{"promos": [p]}]) == [p]
    assert promos_from([p]) == [p]
    assert promos_from("junk") == []


def test_similarity_short_lines():
    from promo_finder.validate import similarity
    assert similarity("No", "I know you") < 0.85
    assert similarity("You will regret this, Anna.", "you will regret this anna") == 1.0
