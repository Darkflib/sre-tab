"""Operator CLI for the source and topic catalogue.

The PRD gives the administrator role no UI: "in v1 this can be an
operator-only CLI or an admin database flag". This is that CLI.

Run it wherever ``DATABASE_URL`` points, which in the deployment means
inside the application container::

    podman exec sre-tab-app sre-tab status
    podman exec sre-tab-app sre-tab sources list

Argparse rather than a CLI framework, deliberately: the dependency set is
Phase 0 property and this needs nothing a stdlib parser cannot do.

Read commands open a session and never write. Write commands open one
session, do the work, and commit once — the transaction boundary belongs
to whoever opened the session (AGENTS.md), and here that is this module.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy.orm import Session

from app.cli import operations as ops
from app.cli.catalogue import InvalidMediumTag
from app.db.engine import create_db_engine
from app.db.session import build_session_factory
from app.settings import get_settings


@contextmanager
def _session(database_url: str | None) -> Iterator[Session]:
    engine = create_db_engine(database_url or get_settings().database_url)
    try:
        with build_session_factory(engine)() as session:
            yield session
    finally:
        engine.dispose()


def _split_topics(value: str | None) -> list[str]:
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _stamp(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%SZ") if value is not None else "—"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [
        max(len(str(headers[index])), *(len(str(row[index])) for row in rows))
        if rows
        else len(str(headers[index]))
        for index in range(len(headers))
    ]
    lines = ["  ".join(str(headers[i]).ljust(widths[i]) for i in range(len(headers))).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(headers))))
    lines.extend(
        "  ".join(str(row[i]).ljust(widths[i]) for i in range(len(headers))).rstrip()
        for row in rows
    )
    return "\n".join(lines)


# --- commands -----------------------------------------------------------


def _cmd_seed(args: argparse.Namespace) -> int:
    with _session(args.database_url) as session:
        report = ops.seed_catalogue(session)
        session.commit()
    if not report.changed:
        print("catalogue already seeded; nothing to do")
        return 0
    print(f"topics added:      {len(report.topics_added)} {' '.join(report.topics_added)}".rstrip())
    print(
        f"sources added:     {len(report.sources_added)} {' '.join(report.sources_added)}".rstrip()
    )
    print(f"topic links added: {report.topic_links_added}")
    return 0


def _cmd_sources_list(args: argparse.Namespace) -> int:
    with _session(args.database_url) as session:
        views = ops.list_sources(session)
    if not views:
        print("no sources configured; run `sre-tab seed`")
        return 0
    print(
        _table(
            ("SLUG", "NAME", "STATE", "EVERY", "TOPICS", "FEED"),
            [
                (
                    view.slug,
                    view.name,
                    "enabled" if view.enabled else "disabled",
                    f"{view.refresh_minutes}m",
                    ",".join(view.topics) or "—",
                    view.feed_url,
                )
                for view in views
            ],
        )
    )
    return 0


def _cmd_sources_add(args: argparse.Namespace) -> int:
    with _session(args.database_url) as session:
        source = ops.add_source(
            session,
            slug=args.slug,
            name=args.name,
            feed_url=args.feed_url,
            website_url=args.website_url,
            refresh_minutes=args.refresh_minutes,
            topics=_split_topics(args.topics),
            icon_url=args.icon_url,
        )
        session.commit()
        print(f"added source {source.slug} -> {source.feed_url}")
    return 0


def _cmd_sources_add_medium_tag(args: argparse.Namespace) -> int:
    with _session(args.database_url) as session:
        source = ops.add_medium_tag(session, args.tag, topics=_split_topics(args.topics))
        session.commit()
        print(f"added source {source.slug} -> {source.feed_url}")
    return 0


def _cmd_sources_enable(args: argparse.Namespace) -> int:
    return _set_source_enabled(args, enabled=True)


def _cmd_sources_disable(args: argparse.Namespace) -> int:
    return _set_source_enabled(args, enabled=False)


def _set_source_enabled(args: argparse.Namespace, *, enabled: bool) -> int:
    with _session(args.database_url) as session:
        ops.set_source_enabled(session, args.slug, enabled=enabled)
        session.commit()
    print(f"source {args.slug} {'enabled' if enabled else 'disabled'}")
    return 0


def _cmd_sources_set_topics(args: argparse.Namespace) -> int:
    with _session(args.database_url) as session:
        ops.set_source_topics(session, args.slug, _split_topics(args.topics))
        session.commit()
    print(f"source {args.slug} topics set to {args.topics or '(none)'}")
    return 0


def _cmd_topics_list(args: argparse.Namespace) -> int:
    with _session(args.database_url) as session:
        topics = ops.list_topics(session)
    if not topics:
        print("no topics configured; run `sre-tab seed`")
        return 0
    print(
        _table(
            ("SLUG", "NAME", "STATE"),
            [
                (topic.slug, topic.name, "enabled" if topic.enabled else "disabled")
                for topic in topics
            ],
        )
    )
    return 0


def _cmd_topics_add(args: argparse.Namespace) -> int:
    with _session(args.database_url) as session:
        ops.add_topic(session, slug=args.slug, name=args.name)
        session.commit()
    print(f"added topic {args.slug}")
    return 0


def _cmd_topics_enable(args: argparse.Namespace) -> int:
    return _set_topic_enabled(args, enabled=True)


def _cmd_topics_disable(args: argparse.Namespace) -> int:
    return _set_topic_enabled(args, enabled=False)


def _set_topic_enabled(args: argparse.Namespace, *, enabled: bool) -> int:
    with _session(args.database_url) as session:
        ops.set_topic_enabled(session, args.slug, enabled=enabled)
        session.commit()
    print(f"topic {args.slug} {'enabled' if enabled else 'disabled'}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    """The operator status view the PRD's non-functional targets require.

    Exits non-zero when an enabled source is failing, so it doubles as a
    check a monitoring job can run.
    """
    with _session(args.database_url) as session:
        views = ops.refresh_status(session)
    if not views:
        print("no sources configured; run `sre-tab seed`")
        return 0

    print(
        _table(
            ("SLUG", "STATE", "LAST FETCH", "LAST SUCCESS", "LAST ERROR"),
            [
                (
                    view.slug,
                    view.state,
                    _stamp(view.last_fetched_at),
                    _stamp(view.last_success_at),
                    view.last_error_class or "—",
                )
                for view in views
            ],
        )
    )

    failing = [view for view in views if view.enabled and view.consecutive_failures]
    if failing:
        print()
        for view in failing:
            print(f"{view.slug}: {view.last_error_class}: {view.last_error_detail}")
    return 1 if failing else 0


# --- parser -------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sre-tab",
        description="Operator CLI: source and topic catalogue, and refresh status.",
    )
    parser.add_argument(
        "--database-url",
        help="Override DATABASE_URL for this invocation.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    seed = commands.add_parser("seed", help="Install the v1 topic taxonomy and source catalogue.")
    seed.set_defaults(handler=_cmd_seed)

    status = commands.add_parser("status", help="Per-source refresh status; non-zero if failing.")
    status.set_defaults(handler=_cmd_status)

    sources = commands.add_parser("sources", help="Manage feed sources.").add_subparsers(
        dest="sources_command", required=True
    )

    sources.add_parser("list", help="List every source.").set_defaults(handler=_cmd_sources_list)

    add = sources.add_parser(
        "add",
        help="Add a source.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "The feed URL is checked here with the same guard the fetcher uses, so a\n"
            "URL that could never be fetched is refused now rather than becoming a\n"
            "source that silently never works.\n"
            "\n"
            "One case the check cannot predict: a URL whose origin answers with a\n"
            "redirect to http://. The guard is https-only on every hop, redirect hops\n"
            "included, so such a source can never be fetched — but nothing about the\n"
            "URL you type says so, and finding out needs a request the add-time check\n"
            "deliberately does not make.\n"
            "\n"
            "A trailing slash is the usual way to land on one. For example\n"
            "https://www.theguardian.com/uk/rss/ answers 301 to\n"
            "http://www.theguardian.com/uk/rss, while the same URL without the slash\n"
            "is fine. If a source you have added never fetches, request the feed URL\n"
            "by hand and look at the Location header before assuming the feed is down."
        ),
    )
    add.add_argument("--slug", required=True)
    add.add_argument("--name", required=True)
    add.add_argument("--feed-url", required=True, help="RSS or Atom URL; https only.")
    add.add_argument("--website-url", required=True)
    add.add_argument("--refresh-minutes", type=int, default=30)
    add.add_argument("--topics", help="Comma-separated topic slugs.")
    add.add_argument("--icon-url")
    add.set_defaults(handler=_cmd_sources_add)

    medium = sources.add_parser(
        "add-medium-tag",
        help="Expand medium.com/feed/tag/<tag> into its own source row.",
    )
    medium.add_argument("tag")
    medium.add_argument("--topics", help="Comma-separated topic slugs.")
    medium.set_defaults(handler=_cmd_sources_add_medium_tag)

    for name, handler, helptext in (
        ("enable", _cmd_sources_enable, "Enable a source."),
        ("disable", _cmd_sources_disable, "Disable a source; its items stay stored."),
    ):
        sub = sources.add_parser(name, help=helptext)
        sub.add_argument("slug")
        sub.set_defaults(handler=handler)

    set_topics = sources.add_parser("set-topics", help="Replace a source's default topics.")
    set_topics.add_argument("slug")
    set_topics.add_argument("--topics", required=True, help="Comma-separated topic slugs.")
    set_topics.set_defaults(handler=_cmd_sources_set_topics)

    topics = commands.add_parser("topics", help="Manage the topic taxonomy.").add_subparsers(
        dest="topics_command", required=True
    )

    topics.add_parser("list", help="List every topic.").set_defaults(handler=_cmd_topics_list)

    topic_add = topics.add_parser("add", help="Add a topic.")
    topic_add.add_argument("--slug", required=True)
    topic_add.add_argument("--name", required=True)
    topic_add.set_defaults(handler=_cmd_topics_add)

    for name, handler, helptext in (
        ("enable", _cmd_topics_enable, "Enable a topic."),
        ("disable", _cmd_topics_disable, "Disable a topic; it leaves the API catalogue."),
    ):
        sub = topics.add_parser(name, help=helptext)
        sub.add_argument("slug")
        sub.set_defaults(handler=handler)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        exit_code: int = args.handler(args)
    except (ops.OperatorError, InvalidMediumTag) as exc:
        # An operator mistake, not a crash: no traceback, just the reason.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return exit_code


if __name__ == "__main__":  # pragma: no cover - exercised via __main__.py
    raise SystemExit(main())
