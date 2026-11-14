"""Skip agent_index_outbox emission when the owning ontology is gone.

Revision ID: 0044_index_outbox_skip_deleted_ontology
Revises: 0043_ontology_tool_catalog_limit
Create Date: 2026-09-09

`emit_agent_index_outbox()` (0006_agent_runtime) fires on every
entity_instances/entity_instance_relations DELETE, including the cascade
deletes that happen when an ontology itself is deleted. It unconditionally
INSERTs a new agent_index_outbox row referencing that ontology_id — but by
the time the trigger runs mid-transaction, `ontology_projects` no longer has
that row, so the outbox insert's own FK constraint (`fk_aio_ontology`) fails
and the whole ontology delete rolls back. There is no consumer left to care
about index events for an ontology that no longer exists, so this guards
every emission on the ontology still being present.
"""
from alembic import op

revision = "0044_index_outbox_skip_deleted_ontology"
down_revision = "0043_ontology_tool_catalog_limit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The trigger function only exists on PostgreSQL (0006).
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION emit_agent_index_outbox() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          evt text;
          payload json;
          target_ontology_id varchar;
        BEGIN
          target_ontology_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.ontology_id ELSE NEW.ontology_id END;
          IF NOT EXISTS (SELECT 1 FROM ontology_projects WHERE id = target_ontology_id) THEN
            RETURN NULL;
          END IF;
          IF TG_TABLE_NAME = 'entity_instances' THEN
            IF TG_OP = 'DELETE' THEN
              evt := 'delete_instance';
              payload := json_build_object('row_identity', OLD.row_identity, 'deleted_at', OLD.deleted_at);
              EXECUTE format(
                'INSERT INTO %I.agent_index_outbox (id, ontology_id, event_type, instance_id, instance_revision, entity_id, payload) '
                'VALUES (gen_random_uuid(), %L, %L, %L, %L, %L, %L::json)',
                TG_TABLE_SCHEMA, OLD.ontology_id, evt, OLD.id, OLD.revision, OLD.entity_id, payload::text);
            ELSE
              IF NEW.deleted_at IS NOT NULL THEN evt := 'delete_instance';
              ELSE evt := 'upsert_instance'; END IF;
              payload := json_build_object('row_identity', NEW.row_identity, 'deleted_at', NEW.deleted_at);
              EXECUTE format(
                'INSERT INTO %I.agent_index_outbox (id, ontology_id, event_type, instance_id, instance_revision, entity_id, payload) '
                'VALUES (gen_random_uuid(), %L, %L, %L, %L, %L, %L::json)',
                TG_TABLE_SCHEMA, NEW.ontology_id, evt, NEW.id, NEW.revision, NEW.entity_id, payload::text);
            END IF;
          ELSE
            IF TG_OP = 'DELETE' THEN
              evt := 'delete_edge';
              payload := json_build_object('deleted_at', OLD.deleted_at);
              EXECUTE format(
                'INSERT INTO %I.agent_index_outbox (id, ontology_id, event_type, edge_id, source_instance_id, '
                'target_instance_id, relation_definition_id, payload) '
                'VALUES (gen_random_uuid(), %L, %L, %L, %L, %L, %L, %L::json)',
                TG_TABLE_SCHEMA, OLD.ontology_id, evt, OLD.id, OLD.source_instance_id,
                OLD.target_instance_id, OLD.relation_definition_id, payload::text);
            ELSE
              IF NEW.deleted_at IS NOT NULL THEN evt := 'delete_edge';
              ELSE evt := 'upsert_edge'; END IF;
              payload := json_build_object('deleted_at', NEW.deleted_at);
              EXECUTE format(
                'INSERT INTO %I.agent_index_outbox (id, ontology_id, event_type, edge_id, source_instance_id, '
                'target_instance_id, relation_definition_id, payload) '
                'VALUES (gen_random_uuid(), %L, %L, %L, %L, %L, %L, %L::json)',
                TG_TABLE_SCHEMA, NEW.ontology_id, evt, NEW.id, NEW.source_instance_id,
                NEW.target_instance_id, NEW.relation_definition_id, payload::text);
            END IF;
          END IF;
          RETURN NULL;
        END;
        $$;
        """
    )


def downgrade() -> None:
    # The trigger function only exists on PostgreSQL (0006).
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION emit_agent_index_outbox() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          evt text;
          payload json;
        BEGIN
          IF TG_TABLE_NAME = 'entity_instances' THEN
            IF TG_OP = 'DELETE' THEN
              evt := 'delete_instance';
              payload := json_build_object('row_identity', OLD.row_identity, 'deleted_at', OLD.deleted_at);
              EXECUTE format(
                'INSERT INTO %I.agent_index_outbox (id, ontology_id, event_type, instance_id, instance_revision, entity_id, payload) '
                'VALUES (gen_random_uuid(), %L, %L, %L, %L, %L, %L::json)',
                TG_TABLE_SCHEMA, OLD.ontology_id, evt, OLD.id, OLD.revision, OLD.entity_id, payload::text);
            ELSE
              IF NEW.deleted_at IS NOT NULL THEN evt := 'delete_instance';
              ELSE evt := 'upsert_instance'; END IF;
              payload := json_build_object('row_identity', NEW.row_identity, 'deleted_at', NEW.deleted_at);
              EXECUTE format(
                'INSERT INTO %I.agent_index_outbox (id, ontology_id, event_type, instance_id, instance_revision, entity_id, payload) '
                'VALUES (gen_random_uuid(), %L, %L, %L, %L, %L, %L::json)',
                TG_TABLE_SCHEMA, NEW.ontology_id, evt, NEW.id, NEW.revision, NEW.entity_id, payload::text);
            END IF;
          ELSE
            IF TG_OP = 'DELETE' THEN
              evt := 'delete_edge';
              payload := json_build_object('deleted_at', OLD.deleted_at);
              EXECUTE format(
                'INSERT INTO %I.agent_index_outbox (id, ontology_id, event_type, edge_id, source_instance_id, '
                'target_instance_id, relation_definition_id, payload) '
                'VALUES (gen_random_uuid(), %L, %L, %L, %L, %L, %L, %L::json)',
                TG_TABLE_SCHEMA, OLD.ontology_id, evt, OLD.id, OLD.source_instance_id,
                OLD.target_instance_id, OLD.relation_definition_id, payload::text);
            ELSE
              IF NEW.deleted_at IS NOT NULL THEN evt := 'delete_edge';
              ELSE evt := 'upsert_edge'; END IF;
              payload := json_build_object('deleted_at', NEW.deleted_at);
              EXECUTE format(
                'INSERT INTO %I.agent_index_outbox (id, ontology_id, event_type, edge_id, source_instance_id, '
                'target_instance_id, relation_definition_id, payload) '
                'VALUES (gen_random_uuid(), %L, %L, %L, %L, %L, %L, %L::json)',
                TG_TABLE_SCHEMA, NEW.ontology_id, evt, NEW.id, NEW.source_instance_id,
                NEW.target_instance_id, NEW.relation_definition_id, payload::text);
            END IF;
          END IF;
          RETURN NULL;
        END;
        $$;
        """
    )
