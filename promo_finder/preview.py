"""Optional: turn a ranked promo into two short text-to-video shot prompts (hook shot + cliffhanger shot).

Used only for a *visual prototype* of the promo (LTX-2.3 makes ~5-10s clips with synced audio).
Ranking and labels never depend on this. If the LLM is off or fails, a template built from the real
screenplay lines is used instead, so this function always returns prompts.
"""
from __future__ import annotations

from .llm import LLMConfig, LLMError, chat_json
from .parse import Episode

SHOT_SYSTEM = """You write prompts for a text-to-video model that generates ~6 second vertical (9:16) clips WITH synced audio.
You get one promo moment from a drama screenplay. Write TWO shot prompts:
 hook_shot  - the promo's opening moment (the first lines below)
 cliff_shot - the promo's final, unresolved moment (the last lines below)
Each prompt: 45-80 words, present tense, one continuous shot. Describe the setting, the characters' look and positions,
the camera (close-up / push-in), lighting and mood, then the action, then the exact spoken line in double quotes with
who says it and how, plus any sound effect. Use ONLY lines given below; do not invent dialogue. No on-screen text, no logos.
Return JSON only: {"hook_shot": "...", "cliff_shot": "..."}"""


def _lines(ep: Episode, start: float, end: float, limit: int) -> list[str]:
    out = []
    for e in ep.window(start, end):
        if e.kind == "DIALOGUE":
            tone = f" ({e.tone})" if e.tone else ""
            out.append(f'{e.speaker}{tone}: "{e.text}"')
        else:
            out.append(f"[{e.kind.lower()}] {e.text}")
    return out[:limit] if limit > 0 else out[limit:]


def moments(c: dict, ep_by_num: dict[int, Episode]) -> tuple[list[str], list[str], str]:
    """(first ~10s of lines, last ~10s of lines, logline) for a validated candidate."""
    first, last = c["segments"][0], c["segments"][-1]
    ep1, ep2 = ep_by_num[first["episode"]], ep_by_num[last["episode"]]
    head = _lines(ep1, first["start"], min(first["end"], first["start"] + 10), 6)
    tail = _lines(ep2, max(last["start"], last["end"] - 10), last["end"], -6)
    return head, tail, ep1.logline


def _template(head: list[str], tail: list[str], logline: str) -> dict:
    style = ("Vertical 9:16 cinematic drama shot, shallow depth of field, dramatic lighting, "
             "tight close-up with a slow push-in. ")
    ctx = f"Story: {logline.rstrip('. ')}. " if logline else ""
    return {"hook_shot": style + ctx + " ".join(head), "cliff_shot": style + ctx + " ".join(tail) + " Hold on the face; tense silence.",
            "source": "template"}


def shot_prompts(c: dict, ep_by_num: dict[int, Episode], cfg: LLMConfig | None = None) -> dict:
    head, tail, logline = moments(c, ep_by_num)
    if cfg is None or not cfg.enabled:
        return _template(head, tail, logline)
    user = (f"SHOW LOGLINE: {logline}\nHOOK (opening lines):\n" + "\n".join(head) +
            "\n\nCLIFFHANGER (closing lines):\n" + "\n".join(tail))
    try:
        parsed, _ = chat_json(cfg, SHOT_SYSTEM, user, max_tokens=900)
        if isinstance(parsed, dict) and parsed.get("hook_shot") and parsed.get("cliff_shot"):
            return {"hook_shot": str(parsed["hook_shot"]), "cliff_shot": str(parsed["cliff_shot"]), "source": "llm"}
    except LLMError:
        pass
    return _template(head, tail, logline)
