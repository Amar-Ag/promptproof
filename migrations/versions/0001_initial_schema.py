"""Initial schema: target families, targets, scans, findings

Revision ID: 0001
Revises:
Create Date: 2026-11-09

Migrations are forward-only in production (AGENTS.md). `downgrade` exists for
local development convenience and is never run against a deployed database.

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "target_families",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_target_families"),
    )

    op.create_table(
        "targets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("family_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("endpoint_url", sa.Text(), nullable=True),
        sa.Column("endpoint_auth_header", sa.Text(), nullable=True),
        sa.Column(
            "ownership_confirmed",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["family_id"],
            ["target_families.id"],
            name="fk_targets_family_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_targets"),
        # A target version, once written, is never updated (ADR-0004). The unique
        # constraint is what makes two concurrent version inserts safe: the loser
        # gets an integrity error rather than a duplicate version number.
        sa.UniqueConstraint("family_id", "version", name="uq_targets_family_version"),
        sa.CheckConstraint("kind IN ('prompt', 'endpoint')", name="ck_targets_kind"),
        sa.CheckConstraint("version >= 1", name="ck_targets_version_positive"),
        # A half-specified target should be impossible, not merely unlikely.
        sa.CheckConstraint(
            "(kind = 'prompt' AND system_prompt IS NOT NULL AND model IS NOT NULL) "
            "OR (kind = 'endpoint' AND endpoint_url IS NOT NULL AND ownership_confirmed)",
            name="ck_targets_kind_fields",
        ),
    )
    op.create_index("ix_targets_family_id", "targets", ["family_id"])

    op.create_table(
        "scans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("suites", postgresql.ARRAY(sa.String(length=64)), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("probe_corpus_version", sa.String(length=64), nullable=True),
        sa.Column("judge_model", sa.String(length=128), nullable=True),
        sa.Column("probe_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        # W3C trace context, so one trace spans submission and execution.
        sa.Column("traceparent", sa.String(length=64), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=64), nullable=True),
        sa.Column(
            "queued_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grade", sa.String(length=1), nullable=True),
        sa.Column("counts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["target_id"], ["targets.id"], name="fk_scans_target_id", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_scans"),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_scans_status",
        ),
        sa.CheckConstraint(
            "grade IS NULL OR grade IN ('A', 'B', 'C', 'D', 'F')", name="ck_scans_grade"
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_scans_attempts"),
        sa.CheckConstraint("array_length(suites, 1) >= 1", name="ck_scans_suites_nonempty"),
    )
    op.create_index("ix_scans_target_id", "scans", ["target_id"])
    # The claim statement only ever looks at pending rows, so the index only
    # contains them. Keeps it small regardless of how many scans accumulate.
    op.create_index(
        "ix_scans_pending_queued_at",
        "scans",
        ["queued_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # Lease-expiry reclaim scans running rows by lock age.
    op.create_index(
        "ix_scans_running_locked_at",
        "scans",
        ["locked_at"],
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "findings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("probe_id", sa.String(length=64), nullable=False),
        sa.Column("suite", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("response_excerpt", sa.Text(), nullable=True),
        sa.Column("judge_rationale", sa.Text(), nullable=True),
        sa.Column("judge_confidence", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("error_reason", sa.String(length=256), nullable=True),
        sa.Column("target_latency_ms", sa.Integer(), nullable=True),
        sa.Column("judge_latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["scan_id"], ["scans.id"], name="fk_findings_scan_id", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_findings"),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')", name="ck_findings_severity"
        ),
        sa.CheckConstraint(
            "outcome IN ('exploited', 'resisted', 'errored')", name="ck_findings_outcome"
        ),
        # One finding per probe per scan. This is what makes re-running a
        # reclaimed scan idempotent instead of duplicating its findings.
        sa.UniqueConstraint("scan_id", "probe_id", name="uq_findings_scan_probe"),
    )
    op.create_index("ix_findings_scan_id", "findings", ["scan_id"])


def downgrade() -> None:
    op.drop_table("findings")
    op.drop_table("scans")
    op.drop_table("targets")
    op.drop_table("target_families")
