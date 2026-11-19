"""Stage 3: deterministic checks. No AI.

Hard REJECT only what is objectively wrong (bad IDs, wrong order, outside 20-60s).
Soft FLAG what is a judgement call (slow open, resolved ending, unexplained names, recap...).
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from .parse import Episode, Event, normalise_speaker

MIN_LEN, MAX_LEN = 20.0, 60.0
MIN_SEG = 4.0
HOOK_WINDOW = 3.0
PRONOUNS = {"he", "she", "they", "it", "that", "this", "him", "her", "them", "those", "these"}
THREAT_WORDS = re.compile(r"\b(will|won't|never|unless|or else|dare|kill|destroy|ruin|swear|regret|pay for|mark my|until|before)\b", re.I)
HOOK_ACTION = re.compile(r"\b(slap|strike|struck|hit|punch|shove|grab|seize|yank|throw|hurl|slam|kneel|collapse|faint|scream|"
                         r"gasp|tear|rip|blood|knife|dagger|sword|gun|pistol|kiss|reveal|unmask|burst|storm|choke|fall|shatter|crash)\w*", re.I)
HOOK_LINE = re.compile(r"[?!]|\b(how dare|liar|traitor|murder|divorce|pregnant|dead|kill|never|marry|betray|who are you|get out)\b", re.I)
TENSION = re.compile(r"\b(freez|stare|glare|trembl|shock|horror|stun|gasp|tense|ominous|menac|threat|danger|dread|"
                     r"suspense|thunder|slam)\w*", re.I)
STING = re.compile(r"\b(sting|swell|crescendo|drone|cliffhanger|peak|ominous|suspense|tension|thunder|boom|cut to black)\w*", re.I)
CALM = re.compile(r"\b(fad(e|ing) out|gentle|warm|peaceful|soft(ly)?|calm|smiles? softly|content)\b", re.I)
GENERIC = {"MAID", "JUDGE", "LORD", "NOBLEMAN", "NOBLEWOMAN", "PRIEST", "GUARD", "OFFICIAL", "INFORMANT", "HOUSEKEEPER",
           "MOTHER", "FATHER", "MATRIARCH", "STRANGER", "MAN", "WOMAN", "GROOM", "BRIDE", "SERVANT", "BUTLER", "DOCTOR"}


def norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", s.lower()).strip()


def similarity(a: str, b: str) -> float:
    a, b = norm_text(a), norm_text(b)
    if not a or not b:
        return 0.0
    if min(len(a), len(b)) >= 12 and (a in b or b in a):      # substring only counts for real lines, not "no" in "i know"
        return 1.0 if min(len(a), len(b)) >= 0.6 * max(len(a), len(b)) else 0.9
    return SequenceMatcher(None, a, b).ratio()


def known_names(eps: list[Episode]) -> set[str]:
    """Single-word character names that also appear Title-cased in action/camera text (e.g. 'Juliana')."""
    speakers = {normalise_speaker(e.speaker) for ep in eps for e in ep.events if e.kind == "DIALOGUE"}
    single = {s for s in speakers if " " not in s and "'" not in s and s not in GENERIC and len(s) > 2}
    stage_text = " ".join(e.text for ep in eps for e in ep.events if e.kind in ("ACTION", "CAMERA"))
    return {s for s in single if re.search(rf"\b{s.title()}\b", stage_text)}


def _ids(ep: Episode) -> dict[str, Event]:
    return {e.id: e for e in ep.events if e.id}


def snap(ep: Episode, start: float, end: float) -> tuple[float, float]:
    """Never cut mid-line: pull start back / push end out to the edges of any dialogue line crossing the cut."""
    for e in ep.events:
        if e.kind == "DIALOGUE" and e.start < start < e.end:
            start = e.start
        if e.kind == "DIALOGUE" and e.start < end < e.end:
            end = e.end
    return round(max(0.0, start), 2), round(min(ep.duration or end, end), 2)


def _clip_events(ep: Episode, s: dict, kinds=("DIALOGUE", "ACTION", "SOUND")) -> list[Event]:
    return [e for e in ep.events if e.kind in kinds and e.start >= s["start"] - 0.01 and e.end <= s["end"] + 0.01]


def validate(raw: dict, ep_by_num: dict[int, Episode], names: set[str], clip_pool: dict | None = None) -> dict:
    """Return a candidate dict with status 'valid' or 'rejected' (+ reason). Times always come from the screenplay."""
    c = {k: v for k, v in raw.items()}
    c["flags"], c["status"], c["reject_reason"], c["trimmed"] = [], "valid", "", False

    def reject(reason: str) -> dict:
        c["status"], c["reject_reason"] = "rejected", reason
        return c

    segs = []
    if not raw.get("segments"):
        return reject("no_segments")
    if raw["source"] != "montage" and len(raw["segments"]) > 3:
        return reject("too_many_segments")
    for s in raw["segments"]:
        ep = ep_by_num.get(s["episode"])
        if ep is None:
            return reject("unknown_episode_or_clip")
        ids = _ids(ep)
        a, b = ids.get(s["start_id"]), ids.get(s["end_id"])
        if a is None or b is None:
            return reject("unknown_event_id")
        if a.start > b.start:
            return reject("start_after_end")
        st, en = snap(ep, a.start, max(a.end, b.end))
        seg = {"episode": ep.number, "start_id": a.id, "end_id": b.id, "start": st, "end": en}
        if raw["source"] == "montage":
            clip = (clip_pool or {}).get(s.get("clip_id"))
            if not clip or not any(cs["episode"] == ep.number and cs["start"] - 0.01 <= st and en <= cs["end"] + 0.01
                                   for cs in clip["segments"]):
                return reject("montage_segment_not_from_validated_clip")
            if en - st < MIN_SEG:
                return reject("montage_segment_too_short")
        segs.append(seg)

    # story order + no overlap
    for p, q in zip(segs, segs[1:]):
        if q["episode"] < p["episode"] or (q["episode"] == p["episode"] and q["start"] < p["end"]):
            return reject("segments_out_of_order_or_overlap")
    if raw["source"] == "montage" and len({s["episode"] for s in segs}) < 2:
        return reject("montage_single_episode")

    total = sum(s["end"] - s["start"] for s in segs)
    if total > MAX_LEN:
        # Deterministic repair: keep the model's ending (the cliffhanger) and move the first segment's start
        # forward to the earliest strong line/action that makes the total fit. Prefer a hook-like opener.
        ep0 = ep_by_num[segs[0]["episode"]]
        other = total - (segs[0]["end"] - segs[0]["start"])
        options = [e for e in _clip_events(ep0, segs[0], ("DIALOGUE", "ACTION"))
                   if MIN_LEN <= other + segs[0]["end"] - e.start <= MAX_LEN - 2.0 and e.start > segs[0]["start"]]
        strong = [e for e in options if (e.kind == "DIALOGUE" and HOOK_LINE.search(e.text)) or HOOK_ACTION.search(e.text)]
        pick = (strong or options or [None])[0]
        if pick is not None:
            st, _ = snap(ep0, pick.start, segs[0]["end"])
            segs[0]["start"], segs[0]["start_id"], c["trimmed"] = st, pick.id, True
            total = other + segs[0]["end"] - st
    if not (MIN_LEN <= total <= MAX_LEN):
        c["segments"], c["duration"] = segs, round(total, 2)
        return reject("too_long" if total > MAX_LEN else "too_short")
    c["segments"], c["duration"] = segs, round(total, 2)

    # --- soft flags -------------------------------------------------------------------------
    first_ep, last_ep = ep_by_num[segs[0]["episode"]], ep_by_num[segs[-1]["episode"]]
    first_events = _clip_events(first_ep, segs[0])
    all_events = [e for s in segs for e in _clip_events(ep_by_num[s["episode"]], s)]
    last_events = _clip_events(last_ep, segs[-1])
    dialogue = [e for e in all_events if e.kind == "DIALOGUE"]

    # grounding: what the model quoted vs what the screenplay says at those IDs
    start_ev = _ids(first_ep)[segs[0]["start_id"]]
    end_ev = _ids(last_ep)[segs[-1]["end_id"]]
    c["hook_text"], c["cliffhanger_text"] = start_ev.text, end_ev.text
    c["grounding"] = {"hook": round(similarity(raw.get("hook_quote", ""), start_ev.text), 2),
                      "cliffhanger": round(similarity(raw.get("cliffhanger_quote", ""), end_ev.text), 2)}
    if raw["source"] == "llm" and min(c["grounding"].values()) < 0.85:
        c["flags"].append("quote_mismatch")

    opener = next((e for e in first_events if e.kind in ("DIALOGUE", "ACTION")), None)
    if opener is None or opener.start - segs[0]["start"] > HOOK_WINDOW:
        c["flags"].append("slow_open")
    else:  # something happens in 3s - but is it a hook? (shocking line / physical action), not "Here you go."
        early = [e for e in first_events if e.kind in ("DIALOGUE", "ACTION") and e.start - segs[0]["start"] <= HOOK_WINDOW]
        if not any((e.kind == "DIALOGUE" and HOOK_LINE.search(e.text)) or HOOK_ACTION.search(e.text) for e in early):
            c["flags"].append("weak_open")
    first_line = next((e for e in first_events if e.kind == "DIALOGUE"), None)
    early_text = " ".join(e.text for e in first_events if e.start - segs[0]["start"] <= 5.0)
    if first_line and norm_text(first_line.text).split()[:1] and norm_text(first_line.text).split()[0] in PRONOUNS \
            and not any(re.search(rf"\b{n.title()}\b", early_text) for n in names):
        c["flags"].append("opens_on_pronoun")

    # cliffhanger: the last line/action must be unresolved; a closing sound counts only if it is a sting, not a fade-out
    tail = [e for e in last_events if e.start >= segs[-1]["end"] - 6.0] or last_events[-1:] or [end_ev]
    last_beat = next((e for e in reversed(tail) if e.kind in ("DIALOGUE", "ACTION")), tail[-1])
    t = last_beat.text.strip()
    unresolved = (t.endswith(("?", "...", "…", "—", "-", "!")) or bool(THREAT_WORDS.search(t)) or bool(TENSION.search(t))
                  or any(e.kind == "SOUND" and STING.search(e.text) and not CALM.search(e.text) for e in tail))
    if not unresolved or any(CALM.search(e.text) for e in tail if e.kind in ("SOUND", "ACTION")):
        c["flags"].append("resolved_ending")

    present = {normalise_speaker(e.speaker) for e in dialogue}
    stage = " ".join(e.text for s in segs for e in _clip_events(ep_by_num[s["episode"]], s, ("ACTION", "CAMERA")))
    mentioned = {n for n in names for e in dialogue if re.search(rf"\b{n.title()}\b", e.text)}
    absent = sorted(n for n in mentioned if not any(n in p for p in present) and not re.search(rf"\b{n.title()}\b", stage))
    if absent:
        c["flags"].append("unexplained_names")
    c["absent_names"] = absent

    if len(dialogue) < 2:
        c["flags"].append("low_dialogue")

    prev = ep_by_num.get(segs[0]["episode"] - 1)
    if prev and segs[0]["start"] <= 20.0 and dialogue:
        tail = [e.text for e in prev.events if e.kind == "DIALOGUE" and e.start >= prev.duration - 40.0]
        hits = sum(1 for e in dialogue if any(similarity(e.text, t2) >= 0.85 for t2 in tail))
        if hits / len(dialogue) > 0.6:
            c["flags"].append("recap")

    if c["trimmed"]:
        c["flags"].append("auto_trimmed_start")
    c["rule"] = {"hook_ok": not ({"slow_open", "weak_open", "opens_on_pronoun"} & set(c["flags"])),
                 "cliff_ok": "resolved_ending" not in c["flags"],
                 "standalone_ok": "unexplained_names" not in c["flags"]}
    return c


def _event_keys(c: dict, ep_by_num: dict[int, Episode]) -> set:
    return {(s["episode"], e.id) for s in c["segments"] for e in _clip_events(ep_by_num[s["episode"]], s)}


def _dialogue(c: dict, ep_by_num: dict[int, Episode]) -> list[str]:
    """Substantive lines only (>= 4 words): short exclamations repeat across a show and are not evidence of a recap."""
    return [e.text for s in c["segments"] for e in _clip_events(ep_by_num[s["episode"]], s, ("DIALOGUE",))
            if len(e.text.split()) >= 4]


def dedupe(cands: list[dict], ep_by_num: dict[int, Episode]) -> tuple[list[dict], list[dict]]:
    """cands must be sorted best-first. Drops >50% event overlap, and cross-episode recaps (>70% same lines; keep earlier ep)."""
    kept, dropped = [], []
    for c in cands:
        keys, lines = _event_keys(c, ep_by_num), _dialogue(c, ep_by_num)
        dup = None
        for k in kept:
            kk = _event_keys(k, ep_by_num)
            if keys and kk and len(keys & kk) / min(len(keys), len(kk)) > 0.5:
                dup = ("overlap", k["id"])
                break
            kl = _dialogue(k, ep_by_num)
            if lines and kl and k["segments"][0]["episode"] != c["segments"][0]["episode"]:
                hits = sum(1 for a in lines if any(similarity(a, b) >= 0.85 for b in kl))
                if hits / len(lines) > 0.7:
                    dup = ("recap_duplicate", k["id"])
                    break
        if dup:
            c["dropped_as"], c["duplicate_of"] = dup
            dropped.append(c)
        else:
            kept.append(c)
    return kept, dropped
