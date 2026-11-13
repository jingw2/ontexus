"""Task 20: refresh freshness propagation into `SemanticSnapshot` and the
Runtime policy layer.

Every successful `RefreshRun` freezes its cursor/lag onto the new
`SemanticSnapshot` it backs (`materialize_refresh_snapshot`); Runtime
decisions read that frozen state through `compute_snapshot_freshness`/
`evaluate_snapshot_freshness` without ever rewriting it. A read
(`RuntimeService.investigate`) denies outright on anything but fresh
evidence; a write (`RuntimeService.create_action_plan`) may still be
proposed for soft-stale data, but the resulting plan carries an explicit
HITL requirement rather than executing automatically, and hard-stale or
unknown freshness denies plan creation outright. A later successful refresh
always materializes a brand-new snapshot; it never rewrites an earlier
snapshot, plan, or (pre-Task-22, before a dedicated approval-receipt model
exists) the plan's own record.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.models.action import Action
from app.models.oauth import OAuthClient
from app.models.ontology import OntologyProject
from app.models.ontology_data_grant import OntologyDataGrant
from app.models.ontology_release import OntologyRelease
from app.models.runtime_plan import RuntimePlan
from app.models.user import User
from app.models.v2.dataset import Dataset, DatasetVersion
from app.models.v2.pipeline import Pipeline, PipelineRun, PipelineRunInput
from app.models.v2.refresh import RefreshRun, RefreshSourceState
from app.schemas.runtime import InvestigationRequest
from app.services.runtime.credentials import RuntimeAccessError, RuntimeContext, RuntimePrincipal
from app.services.runtime.policy import DEFAULT_FRESHNESS_POLICY, FreshnessPolicy, compute_snapshot_freshness
from app.services.runtime.service import ActionPlanRequest, RuntimeService
from app.services.runtime.snapshots import get_snapshot, materialize_refresh_snapshot
from app.services.v2.incremental.operations import get_last_successful_refresh_run

def _now() -> datetime:
    """Capture wall-clock time per fixture/test, not once at module import."""
    return datetime.now(timezone.utc)

DOMAIN = "00000000-0000-0000-0000-0000000000f0"
ONTOLOGY_ID = "ontology-freshness-001"
RELEASE_ID = "release-freshness-001"
AGENT_ID = "agent-freshness-001"
USER_ID = "user-freshness-001"
ACTION_ID = "action-freshness-001"
CAPABILITIES = ["investigate", "propose_action"]

# Lag (seconds) baked into each case_id's RefreshRun cursor, chosen against
# `DEFAULT_FRESHNESS_POLICY` (max_lag_seconds / hard_deny_after_seconds).
_CASE_LAG_SECONDS = {
    "refresh-success-001": 120,
    "refresh-success-002": 90,
    "soft-stale": DEFAULT_FRESHNESS_POLICY.max_lag_seconds + 3600,
    "hard-stale": DEFAULT_FRESHNESS_POLICY.hard_deny_after_seconds + 3600,
}


def _ensure_release(db) -> OntologyRelease:
    """Get-or-create the one published release every fixture in this module
    shares, so repeated `materialize_refresh_fixture` calls within a test
    stay governed by the same ontology/agent/grant."""
    existing = db.get(OntologyRelease, RELEASE_ID)
    if existing is not None:
        return existing
    user = db.get(User, USER_ID)
    if user is None:
        user = User(
            id=USER_ID, username=USER_ID, email=f"{USER_ID}@example.invalid",
            password_hash="not-a-real-password-hash", role="editor", security_domain_id=DOMAIN,
        )
        db.add(user)
        db.flush()
    project = db.get(OntologyProject, ONTOLOGY_ID)
    if project is None:
        project = OntologyProject(
            id=ONTOLOGY_ID, name="freshness ontology", domain="test",
            created_by=USER_ID, security_domain_id=DOMAIN,
        )
        db.add(project)
        db.flush()
    # manifest_projection uses Task 11's PostgreSQL-only CanonicalJSONB type;
    # seed with a plain SQL insert like tests/runtime/conftest.py's `_release`.
    db.execute(
        text(
            "INSERT INTO ontology_releases "
            "(id, ontology_id, version_no, version, manifest_bytes, "
            "manifest_projection, schema_hash, status, created_by, created_at) "
            "VALUES (:id, :ontology_id, :version_no, :version, :manifest, "
            ":projection, :schema_hash, :status, :created_by, CURRENT_TIMESTAMP)"
        ),
        {
            "id": RELEASE_ID, "ontology_id": ONTOLOGY_ID, "version_no": 1, "version": "v1",
            "manifest": b"freshness-manifest", "projection": "{}",
            "schema_hash": b"freshness-schema-hash-00000000000",
            "status": "published", "created_by": USER_ID,
        },
    )
    project.latest_published_release_id = RELEASE_ID
    db.commit()
    return db.get(OntologyRelease, RELEASE_ID)


def _ensure_action(db) -> Action:
    existing = db.get(Action, ACTION_ID)
    if existing is not None:
        return existing
    action = Action(id=ACTION_ID, ontology_id=ONTOLOGY_ID, name_cn="确认", enabled=True)
    db.add(action)
    db.commit()
    return action


def _seed_completed_run(db, *, dataset_version_id: str, pipeline_run_id: str, refresh_run_id: str) -> None:
    """One governed Pipeline/DatasetVersion/PipelineRunInput triple —
    mirrors `tests/runtime/conftest.py`'s `_run` helper, kept local so this
    file stays self-contained (matching `test_runtime_service.py`'s own
    convention of not depending on that conftest's fixtures). Get-or-create
    by `pipeline_run_id`: `materialize_refresh_fixture` is called more than
    once for the same `refresh_run_id` within a single test (once directly,
    once inside `evaluate_fixture_runtime_freshness`), and each call must
    still always insert a brand-new `SemanticSnapshot` over the *same*
    reused governed lineage — exactly how `materialize_snapshot` itself is
    already exercised twice over the same lineage in
    `test_snapshot_materialization.py`.

    `source_id`/`source_type`/`locator`/`content_hash` only — no
    `security_domain_id` key — because `RuntimeService`'s citation
    construction (`EvidenceCitation(**citation)`) only declares those four
    fields; `security_domain_id` is a pre-existing, out-of-scope latent
    schema gap in that path (see the Task 20 report), not something this
    freshness fixture needs to exercise.
    """
    if db.get(PipelineRun, pipeline_run_id) is not None:
        return
    pipeline = Pipeline(id=f"pipeline-{pipeline_run_id}", name=pipeline_run_id, spec={})
    dataset = Dataset(id=f"dataset-{pipeline_run_id}", name=pipeline_run_id, kind="structured")
    version = DatasetVersion(
        id=dataset_version_id, dataset_id=dataset.id, version_no=1, rowcount=2,
        checksum="a" * 64, refresh_run_id=refresh_run_id,
    )
    pipeline_run = PipelineRun(
        id=pipeline_run_id, pipeline_id=pipeline.id, status="success",
        finished_at=datetime.now(timezone.utc), dataset_version_id=version.id,
        stats={"row_count": 2},
    )
    input_row = PipelineRunInput(
        id=f"input-{pipeline_run_id}", pipeline_run_id=pipeline_run.id,
        dataset_version_id=version.id, input_ordinal=0,
        provenance={
            "source_id": "source-freshness-001", "source_type": "test_fixture",
            "locator": "fixture://freshness", "content_hash": "b" * 64,
        },
    )
    db.add_all([pipeline, dataset, version, pipeline_run, input_row])
    db.commit()


def materialize_refresh_fixture(db, *, refresh_run_id: str, now: datetime | None = None):
    """Seed one governed dataset/pipeline pair plus the named `RefreshRun`
    and materialize the snapshot it backs.

    `refresh_run_id` selects the case shape: a plain success id gets a
    fresh cursor/lag anchored to the supplied/current test time; "soft-stale"/"hard-stale" get
    a cursor whose lag already exceeds the shared default policy's
    soft/hard thresholds; "unknown-cursor" gets a successful run with no
    recorded cursor at all.
    """
    release = _ensure_release(db)
    now = now or _now()
    dataset_version_id = f"dv-{refresh_run_id}"
    # "refresh-success-001" is this module's canonical fresh case, matching
    # the exact `pipeline_run_ids == ["pipeline-run-001"]` assertion below.
    pipeline_run_id = "pipeline-run-001" if refresh_run_id == "refresh-success-001" else f"pr-{refresh_run_id}"
    _seed_completed_run(
        db, dataset_version_id=dataset_version_id, pipeline_run_id=pipeline_run_id,
        refresh_run_id=refresh_run_id,
    )
    run = db.get(RefreshRun, refresh_run_id)
    if run is None:
        if refresh_run_id == "unknown-cursor":
            run = RefreshRun(
                id=refresh_run_id, source_id="source-freshness-001", resource="orders",
                policy="batch", config_version=1, cursor_contract="watermark_primary_key",
                status="succeeded", dispatch_state="dispatched",
                idempotency_key=f"idem-{refresh_run_id}", cursor_after_json=None,
                fencing_token=1, pipeline_run_id=pipeline_run_id,
            )
        else:
            lag = _CASE_LAG_SECONDS[refresh_run_id]
            observed_at = now - timedelta(seconds=lag)
            run = RefreshRun(
                id=refresh_run_id, source_id="source-freshness-001", resource="orders",
                policy="batch", config_version=1, cursor_contract="watermark_primary_key",
                status="succeeded", dispatch_state="dispatched",
                idempotency_key=f"idem-{refresh_run_id}",
                cursor_after_json={
                    "watermark": None, "primary_key": "100", "opaque_value": None,
                    "observed_at": observed_at.isoformat(),
                },
                fencing_token=1, lag_seconds=lag, pipeline_run_id=pipeline_run_id,
            )
        db.add(run)
        db.commit()
    return materialize_refresh_snapshot(
        db, refresh_run_id=run.id, ontology_release_id=release.id,
        dataset_version_ids=[dataset_version_id], created_by=USER_ID,
    )


@pytest.fixture
def runtime_context(db):
    """A fully governed principal: active Agent with both capabilities, an
    active entitlement grant, and the one eligible Action, all scoped to
    this module's shared release/ontology."""
    release = _ensure_release(db)
    if db.get(OAuthClient, AGENT_ID) is None:
        db.add(OAuthClient(
            id=AGENT_ID, client_name="Freshness Agent", redirect_uris=[], allowed_scopes=[],
            is_active=True, created_by=USER_ID, security_domain_id=DOMAIN,
            allowed_audiences=[], capability_names=list(CAPABILITIES),
        ))
    has_grant = db.query(OntologyDataGrant).filter(
        OntologyDataGrant.ontology_id == release.ontology_id, OntologyDataGrant.user_id == USER_ID,
    ).first()
    if has_grant is None:
        db.add(OntologyDataGrant(
            id=str(uuid.uuid4()), ontology_id=release.ontology_id, user_id=USER_ID,
            capabilities=list(CAPABILITIES), status="active", created_by=USER_ID,
        ))
    _ensure_action(db)
    db.commit()
    principal = RuntimePrincipal(
        agent_id=AGENT_ID, user_id=USER_ID, security_domain_id=DOMAIN,
        audience="ontexus-runtime", scope=frozenset({"ontology:read"}), token_id="tok-freshness-001",
    )
    return RuntimeContext(principal=principal, correlation_id="corr-freshness-001")


def evaluate_fixture_runtime_freshness(case_id, db, runtime_context):
    """Propose a write against `case_id`'s snapshot and normalize the
    outcome to a `.reason_code`: an outright DENY raises `RuntimeAccessError`
    (its `reason_code` is used directly); a created plan (ALLOW or
    soft-stale HITL) carries its own `freshness_reason_code` in
    `policy_decision`."""
    snapshot = materialize_refresh_fixture(db, refresh_run_id=case_id)
    try:
        plan = RuntimeService().create_action_plan(
            ActionPlanRequest(
                semantic_snapshot_id=snapshot.id, action_id=ACTION_ID,
                parameters={"status": "confirmed"}, idempotency_key=f"idem-plan-{case_id}",
            ),
            runtime_context, db,
        )
    except RuntimeAccessError as exc:
        return types.SimpleNamespace(reason_code=exc.reason_code)
    return types.SimpleNamespace(reason_code=plan.policy_decision["freshness_reason_code"])


def create_approved_fixture(db, runtime_context, refresh_run_id):
    """Build one governed snapshot and its action plan.

    Task 22 (Milestone 3) introduces a dedicated exact-plan approval
    receipt; that model does not exist yet, and this task's Files list adds
    no new one. Until Task 22 lands, the immutable `RuntimePlan` row
    created here IS the plan's own record — `get_approval` below reads the
    same row `get_action_plan` does. These fixtures always target a fresh
    snapshot, so no HITL branch is involved.
    """
    snapshot = materialize_refresh_fixture(db, refresh_run_id=refresh_run_id)
    plan = RuntimeService().create_action_plan(
        ActionPlanRequest(
            semantic_snapshot_id=snapshot.id, action_id=ACTION_ID,
            parameters={"status": "confirmed"}, idempotency_key=f"idem-approved-{refresh_run_id}",
        ),
        runtime_context, db,
    )
    return snapshot, plan, plan


def get_action_plan(db, plan_id):
    return db.get(RuntimePlan, plan_id)


get_approval = get_action_plan


def test_fresh_snapshot_exposes_cursor_lag_and_lineage(db):
    now = _now()
    snapshot = materialize_refresh_fixture(db, refresh_run_id="refresh-success-001", now=now)
    view = compute_snapshot_freshness(
        snapshot, now=now, policy=FreshnessPolicy(max_lag_seconds=300, stale_action="deny"),
    )
    assert view.state == "fresh"
    assert view.lag_seconds == 120
    assert view.cursor.primary_key == "100"
    assert view.pipeline_run_ids == ["pipeline-run-001"]


def test_unrelated_refresh_run_cannot_stamp_freshness_onto_unconnected_dataset_versions(db):
    """`materialize_refresh_snapshot` must not let an unrelated, successful,
    cursor-bearing `RefreshRun` stamp its freshness/cursor onto dataset
    versions it never produced -- it must degrade to the same "unknown"
    freshness shape the function already uses for missing/unsuccessful/
    cursor-less runs, rather than trusting the two independent
    `refresh_run_id`/`dataset_version_ids` parameters as related on faith."""
    release = _ensure_release(db)
    dataset_version_id = "dv-refresh-success-001"
    # Governs dataset_version_id via the real, related "refresh-success-001".
    materialize_refresh_fixture(db, refresh_run_id="refresh-success-001")

    # A real, successful, cursor-bearing RefreshRun for a completely
    # different source/pipeline -- never associated with dataset_version_id
    # via DatasetVersion.refresh_run_id or via PipelineRunInput lineage.
    unrelated = RefreshRun(
        id="refresh-unrelated-001", source_id="source-unrelated-001", resource="invoices",
        policy="batch", config_version=1, cursor_contract="watermark_primary_key",
        status="succeeded", dispatch_state="dispatched",
        idempotency_key="idem-refresh-unrelated-001",
        cursor_after_json={
            "watermark": None, "primary_key": "999", "opaque_value": None,
            "observed_at": _now().isoformat(),
        },
        fencing_token=1, lag_seconds=5, pipeline_run_id="pr-unrelated-001",
    )
    db.add(unrelated)
    db.commit()

    snapshot = materialize_refresh_snapshot(
        db, refresh_run_id=unrelated.id, ontology_release_id=release.id,
        dataset_version_ids=[dataset_version_id], created_by=USER_ID,
    )

    assert snapshot.freshness_state == "unknown"
    assert snapshot.freshness_lag_seconds is None
    assert snapshot.source_cursor is None
    assert snapshot.lineage_summary == {}


def test_partially_related_dataset_version_set_cannot_stamp_freshness_either(db):
    """A request mixing one dataset version this run actually produced with
    one it never touched must still degrade to "unknown" -- checking only
    that AT LEAST ONE requested version is related would let the unrelated
    version silently inherit this run's freshness/cursor, which is exactly
    the exploit `_refresh_run_relates_to_dataset_versions` exists to close."""
    release = _ensure_release(db)
    # Two independently governed dataset versions, each produced by its own
    # distinct RefreshRun.
    materialize_refresh_fixture(db, refresh_run_id="refresh-success-001")
    materialize_refresh_fixture(db, refresh_run_id="refresh-success-002")

    snapshot = materialize_refresh_snapshot(
        db, refresh_run_id="refresh-success-001", ontology_release_id=release.id,
        dataset_version_ids=["dv-refresh-success-001", "dv-refresh-success-002"],
        created_by=USER_ID,
    )

    assert snapshot.freshness_state == "unknown"
    assert snapshot.freshness_lag_seconds is None
    assert snapshot.source_cursor is None
    assert snapshot.lineage_summary == {}


def test_get_last_successful_refresh_run_reads_the_durable_source_state_pointer(db):
    """`get_last_successful_refresh_run` must read the authoritative
    `RefreshSourceState.last_successful_run_id` pointer `record_refresh_outcome`
    maintains — never the most recently *created* run, which could be a
    later failed/dead-lettered attempt instead."""
    older_success = RefreshRun(
        id="run-older-success", source_id="source-pointer-001", resource="orders",
        policy="batch", config_version=1, cursor_contract="watermark_primary_key",
        status="succeeded", dispatch_state="dispatched",
        idempotency_key="idem-run-older-success", fencing_token=1, lag_seconds=30,
    )
    newer_failed = RefreshRun(
        id="run-newer-failed", source_id="source-pointer-001", resource="orders",
        policy="batch", config_version=1, cursor_contract="watermark_primary_key",
        status="failed", dispatch_state="dispatched",
        idempotency_key="idem-run-newer-failed", fencing_token=1,
    )
    db.add_all([older_success, newer_failed])
    db.flush()
    db.add(RefreshSourceState(
        id="state-pointer-001", source_id="source-pointer-001", resource="orders",
        cursor_contract="watermark_primary_key", config_version=1,
        last_successful_run_id=older_success.id, fencing_token=1,
    ))
    db.commit()

    result = get_last_successful_refresh_run(db, source_id="source-pointer-001", resource="orders")
    assert result is not None
    assert result.id == "run-older-success"


@pytest.mark.parametrize("case_id", ["soft-stale", "hard-stale", "unknown-cursor"])
def test_stale_policy_is_allow_hitl_or_deny_without_rewriting_snapshot(case_id, db, runtime_context):
    snapshot = materialize_refresh_fixture(db, refresh_run_id=case_id)
    before = snapshot.freshness_lag_seconds
    result = evaluate_fixture_runtime_freshness(case_id, db, runtime_context)
    assert result.reason_code in {"ALLOW", "SNAPSHOT_FRESHNESS_HITL", "SNAPSHOT_STALE"}
    assert snapshot.freshness_lag_seconds == before


def test_investigate_succeeds_with_real_production_shaped_refresh_provenance(db, runtime_context):
    """Regression test for a reviewer-reproduced crash: real `RefreshRun`/
    `PipelineRunInput` provenance, exactly as `polling.py` (line ~630) and
    `event_ingest.py` (line ~668) actually build it, carries
    `refresh_run_id`, `source_id`, `resource`, `config_version`,
    `cursor_contract`, `cursor_outcome`, `source_observed_at` — never
    `source_type`/`locator`/`content_hash`. Before the fix,
    `materialize_refresh_snapshot` on a `RefreshRun` shaped like that,
    followed by `RuntimeService.investigate`, crashed with an unhandled
    `pydantic.ValidationError` inside `EvidenceCitation(**citation)`
    (missing required fields plus `extra_forbidden` on `refresh_run_id`).
    This proves the real end-to-end flow now succeeds and that the refresh
    lineage survives into the returned citations rather than being
    silently dropped.
    """
    release = _ensure_release(db)
    refresh_run_id = "refresh-provenance-shape-001"
    dataset_version_id = f"dv-{refresh_run_id}"
    pipeline_run_id = f"pr-{refresh_run_id}"
    now = _now()

    # The exact dict shape production code builds — no source_type/locator/
    # content_hash key anywhere (see polling.py:630-638, event_ingest.py:668-678).
    provenance = {
        "refresh_run_id": refresh_run_id,
        "source_id": "source-shape-001",
        "resource": "orders",
        "config_version": 1,
        "cursor_contract": "watermark_primary_key",
        "cursor_outcome": "advanced",
        "source_observed_at": (now - timedelta(seconds=60)).isoformat(),
    }

    pipeline = Pipeline(id=f"pipeline-{pipeline_run_id}", name=pipeline_run_id, spec={})
    dataset = Dataset(id=f"dataset-{pipeline_run_id}", name=pipeline_run_id, kind="structured")
    version = DatasetVersion(
        id=dataset_version_id, dataset_id=dataset.id, version_no=1, rowcount=2,
        checksum="a" * 64, refresh_run_id=refresh_run_id,
    )
    pipeline_run = PipelineRun(
        id=pipeline_run_id, pipeline_id=pipeline.id, status="success",
        finished_at=datetime.now(timezone.utc), dataset_version_id=version.id,
        stats={"row_count": 2},
    )
    # `PipelineRunInput.provenance` is populated from this exact same dict in
    # real code (`contract.py`'s `input_provenance=provenance` call sites),
    # not a hand-picked clean shape — mirror that here.
    input_row = PipelineRunInput(
        id=f"input-{pipeline_run_id}", pipeline_run_id=pipeline_run.id,
        dataset_version_id=version.id, input_ordinal=0, provenance=dict(provenance),
    )
    db.add_all([pipeline, dataset, version, pipeline_run, input_row])
    run = RefreshRun(
        id=refresh_run_id, source_id="source-shape-001", resource="orders",
        policy="batch", config_version=1, cursor_contract="watermark_primary_key",
        status="succeeded", dispatch_state="dispatched",
        idempotency_key=f"idem-{refresh_run_id}",
        cursor_after_json={
            "watermark": None, "primary_key": "100", "opaque_value": None,
            "observed_at": (now - timedelta(seconds=60)).isoformat(),
        },
        fencing_token=1, lag_seconds=60, pipeline_run_id=pipeline_run_id,
        source_provenance=dict(provenance),
    )
    db.add(run)
    db.commit()

    snapshot = materialize_refresh_snapshot(
        db, refresh_run_id=run.id, ontology_release_id=release.id,
        dataset_version_ids=[dataset_version_id], created_by=USER_ID,
    )

    result = RuntimeService().investigate(
        InvestigationRequest(
            semantic_snapshot_id=snapshot.id, query=None,
            ontology_id=ONTOLOGY_ID, entity_type=None, filters={}, limit=20,
        ),
        runtime_context, db,
    )

    assert result.decision == "ALLOW"
    assert result.evidence_citations
    refresh_citations = [c for c in result.evidence_citations if c.refresh_run_id == refresh_run_id]
    assert refresh_citations, "refresh lineage must survive into the returned evidence citations"
    assert {c.source_id for c in refresh_citations} == {"source-shape-001"}


def test_new_refresh_creates_new_snapshot_and_preserves_old_plan_and_approval(db, runtime_context):
    old_snapshot, old_plan, old_approval = create_approved_fixture(db, runtime_context, "refresh-success-001")
    new_snapshot = materialize_refresh_fixture(db, refresh_run_id="refresh-success-002")
    assert new_snapshot.id != old_snapshot.id
    assert get_snapshot(db, old_snapshot.id).source_cursor == old_snapshot.source_cursor
    assert get_action_plan(db, old_plan.id).semantic_snapshot_id == old_snapshot.id
    assert get_approval(db, old_approval.id).plan_hash == old_plan.plan_hash
