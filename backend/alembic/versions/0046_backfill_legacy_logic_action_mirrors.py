"""Backfill v2 mirror rows for existing legacy Logic rules/Actions.

Revision ID: 0046_backfill_legacy_logic_action_mirrors
Revises: 0045_ontology_entity_search_depth
Create Date: 2026-09-11

Publication and the Agent tool catalog read only `v2_ontology_logic_rules`/
`v2_ontology_action_types` (see `app/services/publication/legacy_sync.py`
for why two tables exist and how the app now keeps them in sync going
forward for Logic rules/Actions created through the legacy `/logic`/`/actions`
API). Every such row created BEFORE that sync existed — in particular every
`simple_llm`-mode ontology's LLM-extraction-task rules, which have never had
a v2 counterpart at all — has no mirror row yet, so those ontologies' Agent
tool-binding UI shows zero Logic/Action tools no matter how many the
Ontology's own Logic/Actions tabs list. This one-time backfill creates the
missing mirrors so existing ontologies pick up the fix immediately, without
needing every row individually re-saved or toggled first.

`pipeline_mapping`-mode ontologies are a DIFFERENT case this must not touch:
`app/services/v2/mapping/mapping_service.py` already dual-writes each
discovered rule/action to BOTH tables directly (`_upsert_v1_logic` +
`_upsert_v2_logic`, `_upsert_v1_action` + `_upsert_v2_action`), under two
independently generated ids correlated only by matching `ontology_id`+name.
Backfilling by id alone (as an earlier version of this migration did) cannot
see that correlation and inserts a second, duplicate v2 row for every one of
those already-mirrored legacy rows. This version additionally skips a legacy
row whenever a v2 row already exists for the same ontology and name,
regardless of id.
"""
from alembic import op

revision = "0046_backfill_legacy_logic_action_mirrors"
down_revision = "0045_ontology_entity_search_depth"
branch_labels = None
depends_on = None


# PostgreSQL spellings, with MySQL equivalents for runtime databases there.
_PG = {"obj": "json_build_object", "empty_obj": "'{}'::json", "empty_arr": "'[]'::json"}
_MYSQL = {"obj": "JSON_OBJECT", "empty_obj": "JSON_OBJECT()", "empty_arr": "JSON_ARRAY()"}


def upgrade() -> None:
    mysql = op.get_bind().dialect.name == "mysql"
    sql = _MYSQL if mysql else _PG

    def text(col: str) -> str:
        return f"CAST({col} AS CHAR)" if mysql else f"{col}::text"

    op.execute(f"""
        INSERT INTO v2_ontology_logic_rules
            (id, ontology_id, name, logic_type, description, expression,
             severity, enabled, status, version, created_at, updated_at)
        SELECT
            lr.id, lr.ontology_id, lr.name_cn,
            COALESCE(NULLIF(lr.function_type, ''), 'validation'),
            lr.description,
            CASE WHEN lr.definition IS NOT NULL AND lr.definition <> ''
                 THEN {sql['obj']}('definition', lr.definition)
                 ELSE {sql['empty_obj']} END,
            'info', lr.enabled, COALESCE(NULLIF(lr.status, ''), 'draft'),
            1, lr.created_at, lr.updated_at
        FROM logic_rules lr
        WHERE NOT EXISTS (
            SELECT 1 FROM v2_ontology_logic_rules v2
            WHERE v2.id = lr.id
               OR (v2.ontology_id = lr.ontology_id AND v2.name = lr.name_cn)
        )
    """)
    op.execute(f"""
        INSERT INTO v2_ontology_action_types
            (id, ontology_id, name, description, action_category, parameters,
             submission_criteria, effects, side_effects, permission_rules,
             enabled, status, version, created_at, updated_at)
        SELECT
            a.id, a.ontology_id, a.name_cn, a.description, 'custom',
            COALESCE(a.parameters, {sql['empty_arr']}),
            CASE WHEN a.submission_criteria IS NOT NULL AND {text('a.submission_criteria')} <> '[]'
                 THEN {sql['obj']}('items', a.submission_criteria) ELSE NULL END,
            COALESCE(a.side_effects, {sql['empty_arr']}),
            CASE WHEN a.side_effects IS NOT NULL AND {text('a.side_effects')} <> '[]'
                 THEN {sql['obj']}('items', a.side_effects) ELSE NULL END,
            CASE WHEN a.rules IS NOT NULL AND {text('a.rules')} <> '[]'
                 THEN {sql['obj']}('rules', a.rules) ELSE NULL END,
            a.enabled, COALESCE(NULLIF(a.status, ''), 'draft'),
            1, a.created_at, a.updated_at
        FROM actions a
        WHERE NOT EXISTS (
            SELECT 1 FROM v2_ontology_action_types v2
            WHERE v2.id = a.id
               OR (v2.ontology_id = a.ontology_id AND v2.name = a.name_cn)
        )
    """)


def downgrade() -> None:
    # backfilled rows are indistinguishable from rows the normal v2 pathway
    # (or the sync in app/routers/logic.py|actions.py) creates after this
    # point -- there is nothing safe to selectively revert
    pass
