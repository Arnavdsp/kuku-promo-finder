"""Deterministic screenplay parser. No AI here: the format is regular, so code is cheaper and exact."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

TS = r"(\d{2}):(\d{2}\.\d{2})"
BLOCK_RE = re.compile(r"^\*\*(?P<head>[^*]+?):?\*\*\s*$")
SE_RE = re.compile(rf"^Start:\s*\({TS}\)\s*\|\s*End:\s*\({TS}\)")
SCENE_RE = re.compile(r"^SCENE\s+(\d+)")


def to_sec(m: str, s: str) -> float:
    return int(m) * 60 + float(s)


def fmt(t: float) -> str:
    m, s = divmod(max(t, 0.0), 60)
    return f"{int(m):02d}:{s:05.2f}"


@dataclass
class Event:
    kind: str            # DIALOGUE | ACTION | CAMERA | SOUND
    start: float
    end: float
    text: str
    speaker: str = ""    # dialogue only
    tone: str = ""       # e.g. "Cold, Commanding"
    scene: int = 0
    id: str = ""         # E001.. over non-camera events, in time order (what the LLM refers to)


@dataclass
class Episode:
    number: int
    file: str
    logline: str
    continuity: str
    duration: float
    events: list[Event] = field(default_factory=list)

    def window(self, start: float, end: float, kinds=("DIALOGUE", "ACTION", "SOUND")) -> list[Event]:
        return [e for e in self.events if e.kind in kinds and e.end > start and e.start < end]

    def to_dict(self):
        return asdict(self)


def _head(head: str) -> tuple[str, str, str]:
    """Return (kind, speaker, tone) for a block header like 'LADY JULIANA (Confused, Breathless)'."""
    h = head.strip().rstrip(":")
    if h == "CAMERA":
        return "CAMERA", "", ""
    if h == "ACTION":
        return "ACTION", "", ""
    if h.startswith("SOUND"):
        return "SOUND", "", ""
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", h)
    if m:
        return "DIALOGUE", m.group(1).strip(), m.group(2).strip()
    return "DIALOGUE", h, ""


def parse_episode(path: str | Path) -> Episode:
    path = Path(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    meta = {"EPISODE NUMBER": "", "EPISODE LOGLINE": "", "CONTINUITY NOTES": ""}
    for ln in lines[:10]:
        for k in meta:
            if ln.startswith(k + ":"):
                meta[k] = ln.split(":", 1)[1].strip()
    num_m = re.search(r"(\d+)", meta["EPISODE NUMBER"]) or re.search(r"episode-(\d+)", path.name)
    ep = Episode(number=int(num_m.group(1)) if num_m else 0, file=path.name,
                 logline=meta["EPISODE LOGLINE"], continuity=meta["CONTINUITY NOTES"], duration=0.0)

    scene, i, n = 0, 0, len(lines)
    while i < n:
        ln = lines[i].strip()
        if (sm := SCENE_RE.match(ln)):
            scene = int(sm.group(1))
        elif ln.startswith("Scene end:"):
            mm = re.search(TS, ln)
            if mm:
                ep.duration = max(ep.duration, to_sec(*mm.groups()))
        elif (bm := BLOCK_RE.match(ln)) and i + 1 < n and (tm := SE_RE.match(lines[i + 1].strip())):
            kind, speaker, tone = _head(bm.group("head"))
            g = tm.groups()
            body, j = [], i + 2
            while j < n and lines[j].strip() and not BLOCK_RE.match(lines[j].strip()):
                body.append(lines[j].strip())
                j += 1
            text = " ".join(body).strip().strip('"')
            ep.events.append(Event(kind, to_sec(g[0], g[1]), to_sec(g[2], g[3]), text, speaker, tone, scene))
            i = j
            continue
        i += 1
    ep.events.sort(key=lambda e: (e.start, e.end))
    k = 0
    for e in ep.events:
        if e.kind != "CAMERA":
            k += 1
            e.id = f"E{k:03d}"
    if not ep.duration and ep.events:
        ep.duration = max(e.end for e in ep.events)
    return ep


def load_show(folder: str | Path) -> list[Episode]:
    eps = [parse_episode(p) for p in sorted(Path(folder).glob("*.txt"))]
    return sorted(eps, key=lambda e: e.number)


def compact_transcript(ep: Episode, with_camera: bool = False, seconds: bool = False) -> str:
    """Token-cheap view of an episode for the LLM: one timestamped line per event."""
    kinds = {"DIALOGUE", "ACTION", "SOUND"} | ({"CAMERA"} if with_camera else set())
    out = []
    for e in ep.events:
        if e.kind not in kinds:
            continue
        stamp = f"{e.id} [{e.start:.1f}s-{e.end:.1f}s]" if seconds else f"{e.id} [{fmt(e.start)}-{fmt(e.end)}]"
        if e.kind == "DIALOGUE":
            tone = f" ({e.tone})" if e.tone else ""
            out.append(f'{stamp} {e.speaker}{tone}: "{e.text}"')
        else:
            out.append(f"{stamp} {e.kind}: {e.text}")
    return "\n".join(out)


HONORIFICS = {"LADY", "LORD", "SIR", "THE", "MR", "MRS", "MISS", "DAME", "DUKE", "DUCHESS", "YOUNG", "OLD"}


def normalise_speaker(name: str) -> str:
    """'LADY JULIANA' -> 'JULIANA'. Role names like 'THE FORMER GROOM' stay unresolved (known limitation)."""
    words = [w for w in re.split(r"\s+", name.upper().strip()) if w]
    core = [w for w in words if w not in HONORIFICS]
    return " ".join(core or words)


def parse_report(eps: list[Episode]) -> dict:
    """Sanity report; callers should stop if any episode has zero events (format changed)."""
    rows = []
    for e in eps:
        kinds = {}
        for ev in e.events:
            kinds[ev.kind] = kinds.get(ev.kind, 0) + 1
        rows.append({"episode": e.number, "file": e.file, "duration": round(e.duration, 2),
                     "events": len(e.events), **kinds})
    empty = [r["file"] for r in rows if r["events"] == 0]
    return {"episodes": len(eps), "events": sum(r["events"] for r in rows),
            "minutes": round(sum(e.duration for e in eps) / 60, 1),
            "empty_files": empty, "per_episode": rows}
