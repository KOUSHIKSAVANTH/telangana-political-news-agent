"""Persistence layer (SQLAlchemy Core). SQLite locally, Supabase Postgres in production.

The Postgres DDL in migrations/ is generated from the metadata below
(`python -m app.database --emit-sql`), so both stay in sync.
"""
from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    delete,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex, CreateTable

JSONType = JSON().with_variant(JSONB(), "postgresql")
IdType = BigInteger().with_variant(Integer(), "sqlite")  # sqlite autoincrement needs INTEGER
TS = DateTime(timezone=True)

metadata = MetaData()

sources = Table(
    "sources", metadata,
    Column("id", String(80), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("language", String(8), nullable=False),
    Column("feed_url", Text),
    Column("website", Text),
    Column("type", String(20), nullable=False),
    Column("independence_group", String(80), nullable=False),
    Column("telangana_specific", Boolean, nullable=False, default=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("poll_interval", Integer, nullable=False, default=15),
    Column("etag", Text),
    Column("last_modified", Text),
    Column("last_poll_at", TS),
    Column("last_success_at", TS),
    Column("last_error", Text),
    Column("consecutive_failures", Integer, nullable=False, default=0),
)

articles = Table(
    "articles", metadata,
    Column("id", IdType, primary_key=True, autoincrement=True),
    Column("source_id", String(80), ForeignKey("sources.id"), nullable=False),
    Column("canonical_url", Text, nullable=False, unique=True),
    Column("title", Text, nullable=False),
    Column("title_en", Text),
    Column("excerpt", Text),
    Column("language", String(8), nullable=False),
    Column("published_at", TS),
    Column("discovered_at", TS, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("relevant", Boolean, nullable=False, default=True),
    Column("raw_metadata", JSONType),
)
Index("ix_articles_published_at", articles.c.published_at)
Index("ix_articles_content_hash", articles.c.content_hash)
Index("ix_articles_discovered_at", articles.c.discovered_at)

stories = Table(
    "stories", metadata,
    Column("id", IdType, primary_key=True, autoincrement=True),
    Column("normalized_title", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("summary", Text),
    Column("why_it_matters", Text),
    Column("category", String(40)),
    Column("districts", JSONType),
    Column("topics", JSONType),
    Column("priority", String(12), nullable=False, default="normal"),
    Column("verification_status", String(12), nullable=False, default="unverified"),
    Column("politically_sensitive", Boolean, nullable=False, default=False),
    Column("reason", Text),
    Column("evidence_article_ids", JSONType),
    Column("analysis_source", String(20)),  # gemini | groq | heuristic
    Column("needs_analysis", Boolean, nullable=False, default=True),
    Column("first_published_at", TS),
    Column("last_material_update_at", TS),
    Column("material_update_count", Integer, nullable=False, default=0),
    Column("created_at", TS, nullable=False),
    Column("updated_at", TS, nullable=False),
)
Index("ix_stories_updated_at", stories.c.updated_at)
Index("ix_stories_priority", stories.c.priority)

story_articles = Table(
    "story_articles", metadata,
    Column("story_id", IdType, ForeignKey("stories.id", ondelete="CASCADE"), nullable=False),
    Column("article_id", IdType, ForeignKey("articles.id", ondelete="CASCADE"), nullable=False),
    Column("role", String(20), nullable=False, default="coverage"),
    UniqueConstraint("story_id", "article_id", name="uq_story_article"),
)
Index("ix_story_articles_story_id", story_articles.c.story_id)
Index("ix_story_articles_article_id", story_articles.c.article_id)

runs = Table(
    "runs", metadata,
    Column("id", IdType, primary_key=True, autoincrement=True),
    Column("job_type", String(20), nullable=False),
    Column("started_at", TS, nullable=False),
    Column("ended_at", TS),
    Column("status", String(20), nullable=False),
    Column("items_seen", Integer, nullable=False, default=0),
    Column("items_added", Integer, nullable=False, default=0),
    Column("error_summary", Text),
)

notifications = Table(
    "notifications", metadata,
    Column("id", IdType, primary_key=True, autoincrement=True),
    Column("story_id", IdType, ForeignKey("stories.id", ondelete="SET NULL"), nullable=True),
    Column("type", String(20), nullable=False),  # breaking | review | digest
    Column("recipient_group", String(20), nullable=False),  # team | reviewer
    Column("subject", Text, nullable=False),
    Column("idempotency_key", String(200), nullable=False, unique=True),
    Column("status", String(20), nullable=False),  # pending | sent | failed | dry_run
    Column("created_at", TS, nullable=False),
    Column("sent_at", TS),
    Column("provider_message_id", Text),
    Column("error_summary", Text),
)
Index("ix_notifications_story_id", notifications.c.story_id)

settings_table = Table(
    "settings", metadata,
    Column("key", String(100), primary_key=True),
    Column("value", JSONType),
)

api_usage = Table(
    "api_usage", metadata,
    Column("provider", String(20), nullable=False),
    Column("day", String(10), nullable=False),  # YYYY-MM-DD (UTC)
    Column("units", Integer, nullable=False, default=0),
    UniqueConstraint("provider", "day", name="uq_api_usage"),
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; normalize everything read back to aware UTC."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


class Database:
    def __init__(self, url: str):
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        # prepare_threshold=None: Supabase's transaction pooler (pgbouncer) can't use prepared statements.
        connect_args = {"prepare_threshold": None} if url.startswith("postgresql+psycopg") else {}
        self.engine: Engine = create_engine(url, pool_pre_ping=True, future=True, connect_args=connect_args)
        self.is_sqlite = self.engine.dialect.name == "sqlite"

    def _insert(self, table: Table):
        return (sqlite.insert if self.is_sqlite else postgresql.insert)(table)

    def create_all(self) -> None:
        metadata.create_all(self.engine)

    # ------------------------------------------------------------------ sources
    def sync_sources(self, configured: Iterable[dict[str, Any]]) -> None:
        with self.engine.begin() as conn:
            for s in configured:
                values = dict(
                    id=s["id"], name=s["name"], language=s.get("language", "en"),
                    feed_url=s.get("feed_url"), website=s.get("website"),
                    type=s.get("type", "publisher"),
                    independence_group=s.get("independence_group", s["id"]),
                    telangana_specific=bool(s.get("telangana_specific", False)),
                    enabled=bool(s.get("enabled", False)),
                    poll_interval=int(s.get("poll_interval", 15)),
                )
                stmt = self._insert(sources).values(**values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[sources.c.id],
                    set_={k: v for k, v in values.items() if k != "id"},
                )
                conn.execute(stmt)

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(sources).where(sources.c.id == source_id)).mappings().first()
            return dict(row) if row else None

    def record_poll(self, source_id: str, *, ok: bool, etag: str | None = None,
                    last_modified: str | None = None, error: str | None = None) -> None:
        now = utcnow()
        values: dict[str, Any] = {"last_poll_at": now}
        if ok:
            values.update(last_success_at=now, last_error=None, consecutive_failures=0)
            if etag is not None:
                values["etag"] = etag
            if last_modified is not None:
                values["last_modified"] = last_modified
        else:
            values.update(last_error=(error or "")[:1000],
                          consecutive_failures=sources.c.consecutive_failures + 1)
        with self.engine.begin() as conn:
            conn.execute(update(sources).where(sources.c.id == source_id).values(**values))

    def failing_sources(self, min_failures: int = 4) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(sources.c.id, sources.c.name, sources.c.last_error, sources.c.consecutive_failures)
                .where(sources.c.enabled.is_(True), sources.c.consecutive_failures >= min_failures)
            ).mappings().all()
            return [dict(r) for r in rows]

    # ----------------------------------------------------------------- articles
    def insert_article(self, item, *, content_hash: str, relevant: bool) -> int | None:
        """Insert unless the canonical URL or identical content already exists. Returns new id."""
        with self.engine.begin() as conn:
            dup = conn.execute(
                select(articles.c.id).where(
                    or_(articles.c.canonical_url == item.url, articles.c.content_hash == content_hash)
                ).limit(1)
            ).first()
            if dup:
                return None
            try:
                res = conn.execute(articles.insert().values(
                    source_id=item.source_id, canonical_url=item.url, title=item.title,
                    excerpt=item.excerpt, language=item.language, published_at=item.published_at,
                    discovered_at=utcnow(), content_hash=content_hash, relevant=relevant,
                    raw_metadata=item.raw_metadata,
                ))
            except IntegrityError:  # concurrent insert of the same URL
                return None
            return int(res.inserted_primary_key[0])

    def set_article_title_en(self, article_id: int, title_en: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(articles).where(articles.c.id == article_id).values(title_en=title_en))

    # ------------------------------------------------------------------ stories
    def candidate_stories(self, since: datetime) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(stories.c.id, stories.c.normalized_title, stories.c.first_published_at,
                       stories.c.updated_at)
                .where(stories.c.updated_at >= since)
            ).mappings().all()
            return [dict(r) for r in rows]

    def create_story(self, *, title: str, normalized_title: str, first_published_at: datetime | None) -> int:
        now = utcnow()
        with self.engine.begin() as conn:
            res = conn.execute(stories.insert().values(
                title=title, normalized_title=normalized_title, priority="normal",
                verification_status="unverified", politically_sensitive=False,
                needs_analysis=True, first_published_at=first_published_at,
                material_update_count=0, created_at=now, updated_at=now,
            ))
            return int(res.inserted_primary_key[0])

    def attach_article(self, story_id: int, article_id: int, role: str = "coverage") -> None:
        now = utcnow()
        with self.engine.begin() as conn:
            stmt = self._insert(story_articles).values(story_id=story_id, article_id=article_id, role=role)
            conn.execute(stmt.on_conflict_do_nothing())
            conn.execute(update(stories).where(stories.c.id == story_id)
                         .values(needs_analysis=True, updated_at=now))

    def stories_needing_analysis(self, limit: int) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(stories).where(stories.c.needs_analysis.is_(True))
                .order_by(stories.c.updated_at.desc()).limit(limit)
            ).mappings().all()
            return [dict(r) for r in rows]

    def get_story(self, story_id: int) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(stories).where(stories.c.id == story_id)).mappings().first()
            return dict(row) if row else None

    def story_evidence(self, story_id: int) -> list[dict[str, Any]]:
        """Articles in a story joined with their source metadata, oldest first."""
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(
                    articles.c.id, articles.c.title, articles.c.title_en, articles.c.excerpt,
                    articles.c.canonical_url, articles.c.language, articles.c.published_at,
                    articles.c.discovered_at, articles.c.source_id, articles.c.raw_metadata,
                    sources.c.name.label("publisher"), sources.c.type.label("source_type"),
                    sources.c.independence_group, story_articles.c.role,
                )
                .join(story_articles, story_articles.c.article_id == articles.c.id)
                .join(sources, sources.c.id == articles.c.source_id)
                .where(story_articles.c.story_id == story_id)
                .order_by(articles.c.published_at.asc().nulls_last(), articles.c.id.asc())
            ).mappings().all()
            out = []
            for r in rows:
                d = dict(r)
                d["published_at"] = as_utc(d["published_at"])
                d["discovered_at"] = as_utc(d["discovered_at"])
                out.append(d)
            return out

    def save_analysis(self, story_id: int, **values: Any) -> None:
        # updated_at deliberately untouched: it tracks the latest *coverage*, not re-analysis.
        values.setdefault("needs_analysis", False)
        with self.engine.begin() as conn:
            conn.execute(update(stories).where(stories.c.id == story_id).values(**values))

    def stories_in_window(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        """Stories with coverage published/discovered in [since, until)."""
        with self.engine.connect() as conn:
            ids = select(story_articles.c.story_id).join(
                articles, articles.c.id == story_articles.c.article_id
            ).where(
                func.coalesce(articles.c.published_at, articles.c.discovered_at) >= since,
                func.coalesce(articles.c.published_at, articles.c.discovered_at) < until,
            )
            rows = conn.execute(select(stories).where(stories.c.id.in_(ids))).mappings().all()
            return [dict(r) for r in rows]

    # ------------------------------------------------------------ notifications
    def reserve_notification(self, *, key: str, type_: str, recipient_group: str,
                             subject: str, story_id: int | None) -> bool:
        """Claim an idempotency key before sending. False => already claimed (do not send)."""
        with self.engine.begin() as conn:
            stmt = self._insert(notifications).values(
                idempotency_key=key, type=type_, recipient_group=recipient_group, subject=subject,
                story_id=story_id, status="pending", created_at=utcnow(),
            ).on_conflict_do_nothing(index_elements=[notifications.c.idempotency_key])
            if conn.execute(stmt).rowcount == 1:
                return True
            # A send that definitely failed (e.g. auth error) may be retried; sent/pending never are.
            res = conn.execute(update(notifications).where(
                notifications.c.idempotency_key == key, notifications.c.status == "failed",
            ).values(status="pending", error_summary=None))
            return res.rowcount == 1

    def finish_notification(self, key: str, *, status: str, provider_message_id: str | None = None,
                            error: str | None = None) -> None:
        values: dict[str, Any] = {"status": status, "provider_message_id": provider_message_id,
                                  "error_summary": (error or None) and error[:1000]}
        if status in {"sent", "dry_run"}:
            values["sent_at"] = utcnow()
        with self.engine.begin() as conn:
            conn.execute(update(notifications).where(notifications.c.idempotency_key == key).values(**values))

    def get_notification(self, key: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(select(notifications).where(notifications.c.idempotency_key == key)).mappings().first()
            return dict(row) if row else None

    def last_alert_for_story(self, story_id: int, *, dry_run: bool = False) -> dict[str, Any] | None:
        mode = (notifications.c.idempotency_key.like("dryrun:%") if dry_run
                else ~notifications.c.idempotency_key.like("dryrun:%"))
        with self.engine.connect() as conn:
            row = conn.execute(
                select(notifications)
                .where(notifications.c.story_id == story_id, mode,
                       notifications.c.type.in_(["breaking", "review"]),
                       notifications.c.status.in_(["sent", "dry_run", "pending"]))
                .order_by(notifications.c.created_at.desc()).limit(1)
            ).mappings().first()
            if not row:
                return None
            d = dict(row)
            d["created_at"] = as_utc(d["created_at"])
            return d

    def alerted_story_ids(self, since: datetime) -> set[int]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(notifications.c.story_id).where(
                    notifications.c.type.in_(["breaking", "review"]),
                    notifications.c.status.in_(["sent", "dry_run"]),
                    notifications.c.story_id.is_not(None),
                    notifications.c.created_at >= since,
                )
            ).all()
            return {int(r[0]) for r in rows}

    def count_alerts_since(self, since: datetime) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(
                select(func.count()).select_from(notifications).where(
                    notifications.c.type.in_(["breaking", "review"]),
                    notifications.c.status.in_(["sent", "dry_run", "pending"]),
                    notifications.c.created_at >= since,
                )
            ).scalar_one())

    # --------------------------------------------------------------- api usage
    def get_usage(self, provider: str, day: date | None = None) -> int:
        day_s = (day or utcnow().date()).isoformat()
        with self.engine.connect() as conn:
            v = conn.execute(select(api_usage.c.units).where(
                api_usage.c.provider == provider, api_usage.c.day == day_s)).scalar()
            return int(v or 0)

    def get_usage_since(self, provider: str, since_day: date) -> int:
        with self.engine.connect() as conn:
            v = conn.execute(select(func.coalesce(func.sum(api_usage.c.units), 0)).where(
                api_usage.c.provider == provider, api_usage.c.day >= since_day.isoformat())).scalar()
            return int(v or 0)

    def add_usage(self, provider: str, units: int = 1) -> None:
        day_s = utcnow().date().isoformat()
        with self.engine.begin() as conn:
            stmt = self._insert(api_usage).values(provider=provider, day=day_s, units=units)
            stmt = stmt.on_conflict_do_update(
                index_elements=[api_usage.c.provider, api_usage.c.day],
                set_={"units": api_usage.c.units + units},
            )
            conn.execute(stmt)

    # -------------------------------------------------------------------- runs
    def start_run(self, job_type: str) -> int:
        with self.engine.begin() as conn:
            res = conn.execute(runs.insert().values(job_type=job_type, started_at=utcnow(), status="running",
                                                    items_seen=0, items_added=0))
            return int(res.inserted_primary_key[0])

    def finish_run(self, run_id: int, *, status: str, items_seen: int = 0, items_added: int = 0,
                   error_summary: str | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(runs).where(runs.c.id == run_id).values(
                ended_at=utcnow(), status=status, items_seen=items_seen, items_added=items_added,
                error_summary=(error_summary or None) and error_summary[:2000],
            ))

    # ---------------------------------------------------------------- retention
    def purge(self, *, articles_days: int, runs_days: int) -> None:
        now = utcnow()
        art_cut, run_cut = now - timedelta(days=articles_days), now - timedelta(days=runs_days)
        with self.engine.begin() as conn:
            old_articles = select(articles.c.id).where(articles.c.discovered_at < art_cut)
            conn.execute(delete(story_articles).where(story_articles.c.article_id.in_(old_articles)))
            conn.execute(delete(articles).where(articles.c.discovered_at < art_cut))
            # Stories with no remaining articles (keep those referenced by notifications via SET NULL).
            orphan = select(stories.c.id).where(
                ~stories.c.id.in_(select(story_articles.c.story_id)), stories.c.updated_at < art_cut)
            conn.execute(update(notifications).where(notifications.c.story_id.in_(orphan)).values(story_id=None))
            conn.execute(delete(stories).where(stories.c.id.in_(orphan)))
            conn.execute(delete(runs).where(runs.c.started_at < run_cut))


def emit_postgres_sql() -> str:
    dialect = postgresql.dialect()
    parts = ["-- Generated by `python -m app.database --emit-sql`. Do not hand-edit; change app/database.py.",
             "BEGIN;"]
    for table in metadata.sorted_tables:
        parts.append(str(CreateTable(table, if_not_exists=True).compile(dialect=dialect)).strip() + ";")
        for idx in sorted(table.indexes, key=lambda i: i.name):
            parts.append(str(CreateIndex(idx, if_not_exists=True).compile(dialect=dialect)).strip() + ";")
    parts.append("-- Server-side access only: enable RLS with no policies so anon/auth keys cannot read.")
    for table in metadata.sorted_tables:
        parts.append(f"ALTER TABLE {table.name} ENABLE ROW LEVEL SECURITY;")
    parts.append("COMMIT;")
    return "\n\n".join(parts) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Database utilities")
    parser.add_argument("--init", action="store_true", help="create tables in DATABASE_URL")
    parser.add_argument("--emit-sql", action="store_true", help="print Postgres DDL")
    args = parser.parse_args()
    if args.emit_sql:
        print(emit_postgres_sql())
    if args.init:
        from app.settings import get_settings

        db = Database(get_settings().database_url)
        db.create_all()
        print("tables created")


if __name__ == "__main__":
    main()
