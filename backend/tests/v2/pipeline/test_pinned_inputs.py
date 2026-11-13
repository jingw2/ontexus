"""Pipeline input pinning and pipeline-level status ordering regressions."""
from datetime import datetime, timezone
from sqlalchemy.orm import sessionmaker

from app.models.v2.dataset import Dataset, DatasetVersion
from app.models.v2.pipeline import Pipeline, PipelineRun
from app.tasks.v2 import pipeline_run as pipeline_module


def _file_pipeline(db, *, pipeline_id="pinned-pipeline"):
    first = Dataset(id=f"{pipeline_id}-dataset-a", name="a", kind="structured")
    second = Dataset(id=f"{pipeline_id}-dataset-b", name="b", kind="structured")
    first_version = DatasetVersion(
        id=f"{pipeline_id}-version-a", dataset_id=first.id, version_no=1, rowcount=1,
    )
    second_version = DatasetVersion(
        id=f"{pipeline_id}-version-b", dataset_id=second.id, version_no=1, rowcount=1,
    )
    pipeline = Pipeline(
        id=pipeline_id, name=pipeline_id, spec={}, status="draft",
        definition={
            "nodes": [{
                "id": "source-a", "type": "connector",
                "config": {"source_type": "file", "files": [{"dataset_id": first.id, "name": "a.csv"}]},
            }, {
                "id": "source-b", "type": "connector",
                "config": {"source_type": "file", "files": [{"dataset_id": second.id, "name": "b.csv"}]},
            }],
            "edges": [],
        },
    )
    db.add_all([first, second, first_version, second_version, pipeline])
    db.commit()
    return pipeline, first_version, second_version


def test_incomplete_pinned_inputs_fail_before_latest_lookup(db, monkeypatch):
    pipeline, first_version, _ = _file_pipeline(db)
    run = PipelineRun(id="pinned-run-incomplete", pipeline_id=pipeline.id, status="pending")
    db.add(run)
    db.commit()

    def fail_if_latest_lookup(*args, **kwargs):
        raise AssertionError("latest version lookup must not run for incomplete pins")

    monkeypatch.setattr(
        "app.services.v2.incremental.polling.select_pinned_dataset_version",
        fail_if_latest_lookup,
    )
    monkeypatch.setattr("app.database.SessionLocal", sessionmaker(bind=db.get_bind()))
    pipeline_module.pipeline_run_task.run(pipeline.id, run.id, [first_version.id])
    db.expire_all()

    persisted = db.get(PipelineRun, run.id)
    assert persisted.status == "failed"
    assert persisted.error_log.startswith("PINNED_INPUTS_INCOMPLETE")


def test_duplicate_pins_for_one_source_are_rejected(db, monkeypatch):
    pipeline, first_version, _ = _file_pipeline(db, pipeline_id="pinned-pipeline-duplicate")
    replacement = DatasetVersion(
        id="pinned-pipeline-duplicate-version-a-2",
        dataset_id=first_version.dataset_id,
        version_no=2,
        rowcount=1,
    )
    db.add(replacement)
    run = PipelineRun(id="pinned-run-duplicate", pipeline_id=pipeline.id, status="pending")
    db.add(run)
    db.commit()

    monkeypatch.setattr("app.database.SessionLocal", sessionmaker(bind=db.get_bind()))
    pipeline_module.pipeline_run_task.run(
        pipeline.id, run.id, [first_version.id, replacement.id],
    )
    db.expire_all()

    persisted = db.get(PipelineRun, run.id)
    assert persisted.status == "failed"
    assert persisted.error_log.startswith("PINNED_INPUTS_INCOMPLETE")


def test_pipeline_status_summary_only_follows_latest_run(db):
    from app.tasks.v2.pipeline_run import _set_pipeline_status_if_latest

    pipeline = Pipeline(id="status-pipeline", name="status-pipeline", spec={}, status="draft")
    older = PipelineRun(
        id="status-run-a", pipeline_id=pipeline.id, status="failed",
        finished_at=datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc),
        error_log="older failure",
        created_at=datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc),
    )
    newer = PipelineRun(
        id="status-run-b", pipeline_id=pipeline.id, status="running",
        created_at=datetime(2026, 9, 24, 0, 1, tzinfo=timezone.utc),
    )
    db.add_all([pipeline, older, newer])
    db.commit()

    assert _set_pipeline_status_if_latest(db, pipeline, older, "published") is False
    assert db.get(Pipeline, pipeline.id).status == "draft"

    assert _set_pipeline_status_if_latest(db, pipeline, newer, "published") is True
    assert db.get(Pipeline, pipeline.id).status == "published"
