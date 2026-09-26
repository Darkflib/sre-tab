"""The language an item is written in, detected once at ingest.

An occasional Portuguese or Thai post in a developer feed is unwanted
rather than untranslated, and a feed's own ``<language>`` cannot say so:
it describes the publication, and dev.to is one publication writing in
dozens of languages. So each item is classified from its own text, and
``feed_items.language`` records the answer.

**fastText's ``lid.176`` model, through fast-langdetect, and only the
bundled one.** The compressed model ships inside the wheel, under 1 MB,
and is what :data:`_DETECTOR` is pinned to. The library's default is
``model="auto"``, which *downloads* the 126 MB full model into a temp
directory on first use — a network fetch from inside the ingest loop, to a
host no operator configured, into a unit capped at ``MemoryMax=768M``.
Pinning ``"lite"`` in the config *and* at the call is what keeps that
branch unreachable; ``tests/ingest/test_language.py`` holds it there.

**The answer is ``None`` unless the model is confident.** Fail open is the
whole design. The feed hides an item only where it has a language and that
language is not one the reader asked for, so an undetermined item is
always shown. A wrong confident answer hides an English post, which is
worse than letting the odd Spanish one through, and the threshold is set
from that asymmetry rather than from overall accuracy.

The two numbers below were measured, not chosen. On 342 live items — the
seed catalogue plus dev.to's ``braziliandevs`` and ``spanish`` tags, 25
September 2026 — title alone called ``2DWillNeverDie`` German at 0.89 and
``Amiga screens: a primer`` Catalan; adding the summary moved both below
0.7. Title plus 300 characters of summary at a floor of 0.8 labelled every
Portuguese and Spanish item bar one (0.78, left undetermined) and no
English one as anything else. The library's own default truncates at 80
characters, which cut most summaries off before their first sentence.
"""

from __future__ import annotations

import structlog
from fast_langdetect import FastLangdetectError, LangDetectConfig, LangDetector

log = structlog.get_logger("app.ingest.language")

#: Characters of title-plus-summary given to the model. See the module
#: docstring for how it was arrived at.
DETECTION_INPUT_LENGTH = 300

#: Below this the item is stored with no language, and is never hidden.
MIN_CONFIDENCE = 0.8

#: Every label ``lid.176`` can emit: ISO 639-1 where one exists, otherwise
#: 639-2/3 (``als``, ``ceb``, ``yue``). The preference API validates
#: against this set, because a code the model never emits is a preference
#: that matches nothing — and one that matches nothing hides every item
#: with a detected language. ``tests/ingest/test_language.py`` asserts it
#: equals the model's own label set, so a model change cannot drift from it.
LANGUAGE_CODES: frozenset[str] = frozenset(
    # A word list, not 176 lines of quoted strings.
    """
    af als am an ar arz as ast av az azb ba bar bcl be bg bh bn bo bpy br bs
    bxr ca cbk ce ceb ckb co cs cv cy da de diq dsb dty dv el eml en eo es
    et eu fa fi fr frr fy ga gd gl gn gom gu gv he hi hif hr hsb ht hu hy ia
    id ie ilo io is it ja jbo jv ka kk km kn ko krc ku kv kw ky la lb lez li
    lmo lo lrc lt lv mai mg mhr min mk ml mn mr mrj ms mt mwl my myv mzn nah
    nap nds ne new nl nn no oc or os pa pam pfl pl pms pnb ps pt qu rm ro ru
    rue sa sah sc scn sco sd sh si sk sl so sq sr su sv sw ta te tg th tk tl
    tr tt tyv ug uk ur uz vec vep vi vls vo wa war wuu xal xmf yi yo yue zh
    """.split()  # noqa: SIM905
)

#: One detector for the process. The model loads on the first call, not at
#: import, so the web process pays nothing until ingest first runs.
_DETECTOR = LangDetector(
    LangDetectConfig(model="lite", max_input_length=DETECTION_INPUT_LENGTH, normalize_input=True)
)


def detect_language(title: str, summary: str | None) -> str | None:
    """The language of an item, or ``None`` where the model is not sure.

    A failure inside the library is logged and answered with ``None``
    rather than raised: an item that cannot be classified is an item that
    is always shown, and that must never cost a source its refresh.
    """
    text = " ".join(f"{title} {summary or ''}".split())
    if not text:
        return None
    try:
        results = _DETECTOR.detect(text, model="lite", k=1)
    except (FastLangdetectError, ValueError) as exc:
        log.warning("language_detection_failed", error=type(exc).__name__)
        return None
    if not results:
        return None
    top = results[0]
    language = str(top["lang"])
    if float(top["score"]) < MIN_CONFIDENCE or language not in LANGUAGE_CODES:
        return None
    return language
