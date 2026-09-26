"""Language detection — what it answers, when it declines, what it never does.

Titles are real ones from the feeds this was measured against on
25 September 2026 (see ``app.ingest.language``), not invented examples:
a detector is only as good as the text it is asked about, and developer
headlines are short, jargon-heavy, and full of English loanwords.
"""

from __future__ import annotations

from datetime import UTC, datetime

import fasttext
import pytest
from fast_langdetect import FastLangdetectError
from fast_langdetect.infer import _LOCAL_SMALL_MODEL_PATH, ModelLoader
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FeedItem, Source
from app.ingest import language
from app.ingest.language import LANGUAGE_CODES, detect_language
from app.ingest.normalise import NormalisedItem
from app.ingest.store import upsert_items

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("title", "summary", "expected"),
    [
        (
            "Análise das Respostas de LLMs em Relação ao Conteúdo Introdutório de "
            "Programação: um Comparativo entre o ChatGPT e o Gemini",
            "Dados da obra: Análise das Respostas de LLMs em Relação ao Conteúdo",
            "pt",
        ),
        (
            "Claude Opus 5.5 ถูกลง 40 เปอร์เซ็นต์แต่เก่งขึ้น: สงครามโมเดลยุคใหม่เริ่มแล้ว",
            None,
            "th",
        ),
        ("Epílogo: haviam muitas possibilidades para dar errado", None, "pt"),
        ("Closures a fondo en JavaScript", "Una guía de closures con ejemplos prácticos", "es"),
        ("Why your Kubernetes pods keep getting OOMKilled", None, "en"),
    ],
)
def test_it_names_the_language_of_real_headlines(
    title: str, summary: str | None, expected: str
) -> None:
    assert detect_language(title, summary) == expected


@pytest.mark.parametrize(
    ("title", "summary"),
    [
        # 0.56 English on its own: three tokens, two of them names.
        ("Rust vs Go", None),
        # Catalan at 0.48, from Lobsters. Wrong, and not confident, which is
        # the combination the floor exists for.
        ("Amiga screens: a primer", None),
        # Title alone, this was German at 0.89 — the reason the summary is
        # part of the input and the reason for the floor.
        ("2DWillNeverDie", "Comments"),
        ("", None),
        ("   ", "  \n "),
    ],
)
def test_it_declines_rather_than_guesses(title: str, summary: str | None) -> None:
    """``None`` is the answer the feed never hides, so declining is the safe
    failure and a confident wrong answer is the unsafe one."""
    assert detect_language(title, summary) is None


def test_newlines_in_a_summary_are_not_an_error() -> None:
    """fastText refuses a newline in its input; summaries have plenty."""
    assert (
        detect_language(
            "Como uma gestão despreparada pode custar a sua carreira",
            "Uma gestão ruim\n\ncusta caro.\nMuito caro.",
        )
        == "pt"
    )


def test_a_library_failure_is_an_undetermined_item(monkeypatch: pytest.MonkeyPatch) -> None:
    """A source's refresh must not depend on the classifier working."""

    def _broken(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise FastLangdetectError("model exploded")

    monkeypatch.setattr(language._DETECTOR, "detect", _broken)

    assert detect_language("Why your Kubernetes pods keep getting OOMKilled", None) is None


def test_the_full_model_is_never_downloaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The library's default is ``model="auto"``, which fetches 126 MB from
    Facebook's CDN on first use. The loader is replaced with one that fails
    the test, and the detector's model cache is emptied so the next call
    has to choose a model afresh rather than reuse one already loaded."""

    def _forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("language detection tried to download the full model")

    monkeypatch.setattr(ModelLoader, "load_with_download", _forbidden)
    monkeypatch.setattr(language._DETECTOR, "_models", {})

    assert detect_language("Why your Kubernetes pods keep getting OOMKilled", None) == "en"


def test_the_code_list_is_exactly_what_the_model_emits() -> None:
    """The preference API validates against :data:`LANGUAGE_CODES`, so a
    code missing here is a language nobody can choose and a code extra
    here is a choice that matches nothing. ``k=-1`` with a negative
    threshold asks for every label; one input is not always enough to get
    all of them back, so a few are pooled."""
    model = fasttext.load_model(str(_LOCAL_SMALL_MODEL_PATH))
    emitted: set[str] = set()
    for text in ("hello world", "ถูกลง", "日本語", "Привет", "مرحبا", "Análise", ""):
        labels, _ = model.predict(text, k=-1, threshold=-1.0)
        emitted.update(label.removeprefix("__label__") for label in labels)

    assert emitted == LANGUAGE_CODES


# --- at ingest ----------------------------------------------------------


def _item(url: str, title: str) -> NormalisedItem:
    return NormalisedItem(
        canonical_url=url, title=title, summary=None, published_at=NOW, image_url=None
    )


def test_a_new_item_is_stored_with_its_language(db_session: Session, source: Source) -> None:
    upsert_items(
        db_session,
        source_id=source.id,
        items=[
            _item("https://dev.to/a/pt", "Como uma gestão despreparada pode custar a sua carreira"),
            _item("https://dev.to/a/en", "Why your Kubernetes pods keep getting OOMKilled"),
            _item("https://dev.to/a/short", "Rust vs Go"),
        ],
        topic_ids=[],
        fetched_at=NOW,
    )

    stored = dict(
        db_session.execute(select(FeedItem.canonical_url, FeedItem.language)).tuples().all()
    )
    assert stored == {
        "https://dev.to/a/pt": "pt",
        "https://dev.to/a/en": "en",
        "https://dev.to/a/short": None,
    }


def test_an_existing_item_is_not_redetected(db_session: Session, source: Source) -> None:
    """Ingest never rewrites a stored row; the CLI pass is what does."""
    db_session.add(
        FeedItem(
            source_id=source.id,
            canonical_url="https://dev.to/a/pt",
            title="Como uma gestão despreparada pode custar a sua carreira",
            published_at=NOW,
        )
    )
    db_session.commit()

    upsert_items(
        db_session,
        source_id=source.id,
        items=[
            _item("https://dev.to/a/pt", "Como uma gestão despreparada pode custar a sua carreira")
        ],
        topic_ids=[],
        fetched_at=NOW,
    )

    assert db_session.scalars(select(FeedItem.language)).one() is None
