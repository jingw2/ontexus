"""Database connector source -> snapshot -> Route A pipeline coverage."""
from unittest.mock import MagicMock

from sqlalchemy.orm import sessionmaker

from app.models.v2.connection import Connection
from app.models.v2.pipeline import Pipeline, PipelineRun, PipelineRunInput
from app.services import encryption_service
from app.tasks.v2 import pipeline_run as pipeline_module
from app.routers.v2.pipelines import _sanitize_definition


def test_pipeline_definition_strips_database_credentials_before_persisting():
    definition = {
        "nodes": [{
            "type": "connector",
            "config": {
                "source_type": "postgresql",
                "connection_id": "db-source-001",
                "resource": "codex_pipeline_probe",
                "config_values": {
                    "host": "127.0.0.1",
                    "password": "do-not-store",
                },
                "password": "do-not-store-either",
                "credentials": {
                    "username": "runtime",
                    "password": "nested-do-not-store",
                },
            },
        }],
        "edges": [],
    }

    sanitized = _sanitize_definition(definition)

    assert sanitized is not definition
    config = sanitized["nodes"][0]["config"]
    assert config["connection_id"] == "db-source-001"
    assert "password" not in config
    assert "config_values" not in config
    assert "credentials" not in config
    assert definition["nodes"][0]["config"]["password"] == "do-not-store-either"


def test_pipeline_run_snapshots_selected_database_table_and_records_lineage(db, monkeypatch):
    class MemoryStorage:
        def __init__(self):
            self.objects = {}

        def put_bytes(self, bucket, key, data, content_type="application/octet-stream"):
            uri = f"s3://{bucket}/{key}"
            self.objects[uri] = data
            return uri

        def get_object(self, uri):
            return self.objects[uri]

    storage = MemoryStorage()
    monkeypatch.setattr("app.database.SessionLocal", sessionmaker(bind=db.get_bind()))
    monkeypatch.setattr("app.services.v2.dataset_service.get_storage_service", lambda: storage)
    connector = MagicMock()
    connector.pull_full.return_value = [
        {"id": row_id, "customer": f"Customer {row_id}", "amount": row_id}
        for row_id in range(10001)
    ]
    monkeypatch.setattr(pipeline_module, "get_connector", lambda kind, config: connector)
    monkeypatch.setattr(
        pipeline_module,
        "_execute_route",
        lambda route, context, data: (data, context),
    )

    connection = Connection(
        id="db-source-001",
        name="runtime postgres",
        kind="postgres",
        config={"_encrypted": encryption_service.encrypt(
            '{"host":"127.0.0.1","port":"55432","database":"runtime",'
            '"user":"runtime","password":"secret"}'
        )},
        status="active",
    )
    pipeline = Pipeline(
        id="db-pipeline-001",
        name="database pipeline",
        route="A",
        spec={},
        definition={
            "nodes": [{
                "id": "source",
                "type": "connector",
                "config": {
                    "source_type": "postgresql",
                    "connection_id": connection.id,
                    "resource": "codex_pipeline_probe",
                },
            }],
            "edges": [],
        },
        status="draft",
    )
    run = PipelineRun(id="db-run-001", pipeline_id=pipeline.id, status="pending")
    db.add_all([connection, pipeline, run])
    db.commit()

    pipeline_module.pipeline_run_task.run(pipeline.id, run.id)
    db.expire_all()

    persisted = db.get(PipelineRun, run.id)
    assert persisted.status == "success"
    assert persisted.dataset_version_id is not None
    assert persisted.stats["rows_in"] == 10001
    assert db.query(PipelineRunInput).filter(
        PipelineRunInput.pipeline_run_id == run.id,
    ).count() >= 2
    connector.pull_full.assert_called_once_with("codex_pipeline_probe")
    from app.models.v2.dataset import Dataset
    from app.services.v2.incremental.polling import _resolve_pipeline

    source_dataset = db.query(Dataset).filter(
        Dataset.source_connection_id == connection.id,
        Dataset.name == "codex_pipeline_probe",
    ).one()
    assert db.get(Pipeline, pipeline.id).source_dataset_id == source_dataset.id
    assert _resolve_pipeline(db, dataset=source_dataset).id == pipeline.id
