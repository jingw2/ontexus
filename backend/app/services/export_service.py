import csv
import html
import io
import json
import re
import yaml
import zipfile
from sqlalchemy.orm import Session
from app.models.ontology import OntologyProject
from app.models.entity import Entity
from app.models.logic import LogicRule
from app.models.action import Action
from app.models.relation import Relation
from app.services.relation_dedup import dedupe_relation_rows


def _collect_data(db: Session, ontology_id: str) -> dict:
    project = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    entities = db.query(Entity).filter(Entity.ontology_id == ontology_id).all()
    logic_rules = db.query(LogicRule).filter(LogicRule.ontology_id == ontology_id).all()
    actions = db.query(Action).filter(Action.ontology_id == ontology_id).all()
    relations = dedupe_relation_rows(
        db.query(Relation).filter(Relation.ontology_id == ontology_id).all()
    )
    return {
        "project": project,
        "entities": entities,
        "logic_rules": logic_rules,
        "actions": actions,
        "relations": relations,
    }


def _entity_map(entities: list) -> dict[str, Entity]:
    return {e.id: e for e in entities}


def _entity_label(e: Entity | None, lang: str = "cn") -> str:
    if not e:
        return ""
    if lang == "en":
        return e.name_en or e.name_cn or ""
    return e.name_cn or e.name_en or ""


def _relation_row(rel: Relation, emap: dict[str, Entity]) -> dict:
    src = emap.get(rel.source_entity)
    tgt = emap.get(rel.target_entity)
    return {
        "id": rel.id,
        "source_id": rel.source_entity,
        "target_id": rel.target_entity,
        "source_cn": _entity_label(src, "cn"),
        "source_en": _entity_label(src, "en"),
        "target_cn": _entity_label(tgt, "cn"),
        "target_en": _entity_label(tgt, "en"),
        "type": rel.type,
        "confidence": rel.confidence,
        "properties": rel.properties or {},
    }


def _h(text: str | None) -> str:
    return html.escape(text or "", quote=True)


def _safe_uri_token(value: str) -> str:
    return re.sub(r"[^\w]", "_", value or "unknown")


def _ontology_payload(data: dict) -> dict:
    p = data["project"]
    emap = _entity_map(data["entities"])
    return {
        "ontology": {"id": p.id, "name": p.name, "domain": p.domain, "version": p.version},
        "entities": [
            {
                "id": e.id,
                "name_cn": e.name_cn,
                "name_abbr": e.name_abbr,
                "name_en": e.name_en,
                "type": e.type,
                "description": e.description,
                "confidence": e.confidence,
            }
            for e in data["entities"]
        ],
        "relations": [_relation_row(r, emap) for r in data["relations"]],
        "logic_rules": [
            {
                "id": r.id,
                "name_cn": r.name_cn,
                "function_type": r.function_type,
                "definition": r.definition,
                "confidence": r.confidence,
            }
            for r in data["logic_rules"]
        ],
        "actions": [
            {
                "id": a.id,
                "name_cn": a.name_cn,
                "name_en": a.name_en,
                "description": a.description,
                "parameters": a.parameters,
                "rules": a.rules,
                "submission_criteria": a.submission_criteria,
                "side_effects": a.side_effects,
                "confidence": a.confidence,
            }
            for a in data["actions"]
        ],
    }


def export_json(db: Session, ontology_id: str) -> str:
    data = _collect_data(db, ontology_id)
    return json.dumps(_ontology_payload(data), ensure_ascii=False, indent=2)


def export_yaml(db: Session, ontology_id: str) -> str:
    data = json.loads(export_json(db, ontology_id))
    return yaml.dump(data, allow_unicode=True, default_flow_style=False)


def export_csv(db: Session, ontology_id: str) -> str:
    data = _collect_data(db, ontology_id)
    emap = _entity_map(data["entities"])
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([
        "record_type",
        "id",
        "name_cn",
        "name_en",
        "description",
        "confidence",
        "source_cn",
        "target_cn",
        "relation_type",
    ])
    for e in data["entities"]:
        w.writerow(["entity", e.id, e.name_cn, e.name_en or "", e.description or "", e.confidence, "", "", ""])
    for rel in data["relations"]:
        row = _relation_row(rel, emap)
        w.writerow([
            "relation",
            row["id"],
            "",
            "",
            f"{row['source_cn']} -> {row['target_cn']}",
            row["confidence"],
            row["source_cn"],
            row["target_cn"],
            row["type"],
        ])
    for r in data["logic_rules"]:
        w.writerow(["logic_rule", r.id, r.name_cn, r.name_en or "", r.description or "", r.confidence, "", "", ""])
    for a in data["actions"]:
        w.writerow(["action", a.id, a.name_cn, a.name_en or "", a.description or "", a.confidence, "", "", ""])
    return out.getvalue()


def export_ttl(db: Session, ontology_id: str) -> str:
    from rdflib import Graph, Literal, Namespace, RDF, OWL, RDFS

    data = _collect_data(db, ontology_id)
    p = data["project"]
    emap = _entity_map(data["entities"])
    g = Graph()
    NS = Namespace(f"http://ontexus.local/ontologies/{ontology_id}#")
    g.bind("onto", NS)
    g.bind("owl", OWL)

    g.add((NS[_safe_uri_token(p.name)], RDF.type, OWL.Ontology))

    entity_uris: dict[str, object] = {}
    for e in data["entities"]:
        uri = NS[f"entity_{_safe_uri_token(e.id)}"]
        entity_uris[e.id] = uri
        g.add((uri, RDF.type, OWL.Class))
        g.add((uri, RDFS.label, Literal(e.name_cn, lang="zh")))
        if e.name_en:
            g.add((uri, RDFS.label, Literal(e.name_en, lang="en")))
        if e.description:
            g.add((uri, RDFS.comment, Literal(e.description)))

    prop_uris: dict[str, object] = {}
    for rel in data["relations"]:
        prop_key = _safe_uri_token(rel.type)
        if prop_key not in prop_uris:
            prop_uri = NS[f"rel_{prop_key}"]
            prop_uris[prop_key] = prop_uri
            g.add((prop_uri, RDF.type, OWL.ObjectProperty))
            g.add((prop_uri, RDFS.label, Literal(rel.type)))
        src_uri = entity_uris.get(rel.source_entity)
        tgt_uri = entity_uris.get(rel.target_entity)
        if src_uri and tgt_uri:
            g.add((src_uri, prop_uris[prop_key], tgt_uri))

    return g.serialize(format="turtle")


def export_neo4j_cypher(db: Session, ontology_id: str) -> str:
    data = _collect_data(db, ontology_id)
    p = data["project"]
    lines: list[str] = []

    lines.append(f"// Neo4j Cypher — {p.name} (domain: {p.domain}, version: {p.version})")
    lines.append("// Generated by Ontexus")
    lines.append("")

    lines.append("// ── Nodes ──────────────────────────────────────────────────────────────")
    for e in data["entities"]:
        label = re.sub(r"[^\w]", "_", e.type or "Entity")
        props: dict = {
            "id": e.id,
            "name_cn": e.name_cn or "",
            "name_en": e.name_en or "",
            "confidence": e.confidence,
        }
        if e.name_abbr:
            props["name_abbr"] = e.name_abbr
        if e.description:
            props["description"] = e.description
        if e.properties:
            props.update(e.properties)
        props_str = _cypher_props(props)
        lines.append(f"MERGE (n:{label} {{id: {_cypher_val(e.id)}}})")
        lines.append(f"  SET n += {props_str};")

    lines.append("")
    lines.append("// ── Relationships ───────────────────────────────────────────────────────")
    for rel in data["relations"]:
        rel_type = re.sub(r"[^\w]", "_", rel.type or "RELATED_TO").upper()
        props: dict = {"confidence": rel.confidence}
        if rel.properties:
            props.update(rel.properties)
        props_str = _cypher_props(props)
        lines.append(f"MATCH (a {{id: {_cypher_val(rel.source_entity)}}}), (b {{id: {_cypher_val(rel.target_entity)}}})")
        lines.append(f"MERGE (a)-[r:{rel_type}]->(b)")
        lines.append(f"  SET r += {props_str};")

    return "\n".join(lines) + "\n"


def _cypher_str(s: str) -> str:
    escaped = (
        s.replace("\\", "\\\\")
         .replace("'", "\\'")
         .replace("\n", "\\n")
         .replace("\r", "\\r")
         .replace("\t", "\\t")
    )
    return f"'{escaped}'"


def _cypher_val(v) -> str:
    if isinstance(v, str):
        return _cypher_str(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if v is None:
        return "null"
    if isinstance(v, list):
        return "[" + ", ".join(_cypher_val(item) for item in v) + "]"
    if isinstance(v, dict):
        return _cypher_props(v)
    # fallback: stringify and escape as a string
    return _cypher_str(str(v))


_CYPHER_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _cypher_key(k: str) -> str:
    """Backtick-quote property keys that are not plain ASCII identifiers."""
    if _CYPHER_IDENT_RE.match(k):
        return k
    return "`" + k.replace("`", "``") + "`"


def _cypher_props(props: dict) -> str:
    pairs = ", ".join(f"{_cypher_key(k)}: {_cypher_val(v)}" for k, v in props.items())
    return "{" + pairs + "}"


# ── TuGraph bulk-import ───────────────────────────────────────────────────────
# Output: ZIP containing import.json + one CSV per vertex label + one CSV per
# edge type.  Load with: lgraph_import -c import.json --dir /path/to/db

_TG_VERTEX_COLS = ["id", "name_cn", "name_en", "name_abbr", "description", "confidence"]
_TG_EDGE_COLS   = ["SRC_ID", "DST_ID", "confidence"]

_TG_PROP_DEFS = [
    {"name": "id",          "type": "STRING"},
    {"name": "name_cn",     "type": "STRING"},
    {"name": "name_en",     "type": "STRING"},
    {"name": "name_abbr",   "type": "STRING"},
    {"name": "description", "type": "STRING"},
    {"name": "confidence",  "type": "DOUBLE"},
]
_TG_EDGE_PROP_DEFS = [{"name": "confidence", "type": "DOUBLE"}]


def export_tugraph_zip(db: Session, ontology_id: str) -> bytes:
    data = _collect_data(db, ontology_id)
    emap = _entity_map(data["entities"])

    # Group entities by label (entity.type, normalised)
    by_label: dict[str, list[Entity]] = {}
    for e in data["entities"]:
        label = re.sub(r"[^\w]", "_", e.type or "Entity")
        by_label.setdefault(label, []).append(e)

    # Group relations by type (normalised to upper)
    by_rel: dict[str, list] = {}
    for rel in data["relations"]:
        rtype = re.sub(r"[^\w]", "_", rel.type or "RELATED_TO").upper()
        by_rel.setdefault(rtype, []).append(rel)

    schema_vertices = [
        {
            "label": label,
            "type": "VERTEX",
            "primary": "id",
            "properties": _TG_PROP_DEFS,
        }
        for label in sorted(by_label)
    ]

    schema_edges = [
        {
            "label": rtype,
            "type": "EDGE",
            "properties": _TG_EDGE_PROP_DEFS,
        }
        for rtype in sorted(by_rel)
    ]

    file_entries = []

    # Vertex file entries
    for label in sorted(by_label):
        file_entries.append({
            "path": f"vertex_{label}.csv",
            "format": "CSV",
            "label": label,
            "header": 1,
            "columns": _TG_VERTEX_COLS,
        })

    # Edge file entries — TuGraph needs SRC_ID/DST_ID to know which vertex
    # label each endpoint belongs to.  Since we may have edges between
    # different vertex labels, use a catch-all approach: declare the first
    # observed (src_label, dst_label) pair per edge type.
    edge_label_pairs: dict[str, tuple[str, str]] = {}
    for rtype in sorted(by_rel):
        for rel in by_rel[rtype]:
            src = emap.get(rel.source_entity)
            tgt = emap.get(rel.target_entity)
            if src and tgt:
                src_label = re.sub(r"[^\w]", "_", src.type or "Entity")
                tgt_label = re.sub(r"[^\w]", "_", tgt.type or "Entity")
                edge_label_pairs[rtype] = (src_label, tgt_label)
                break

    for rtype in sorted(by_rel):
        src_label, tgt_label = edge_label_pairs.get(rtype, ("Entity", "Entity"))
        file_entries.append({
            "path": f"edge_{rtype}.csv",
            "format": "CSV",
            "label": rtype,
            "SRC_ID": src_label,
            "DST_ID": tgt_label,
            "header": 1,
            "columns": _TG_EDGE_COLS,
        })

    import_json = json.dumps(
        {"schema": schema_vertices + schema_edges, "files": file_entries},
        ensure_ascii=False,
        indent=2,
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("import.json", import_json)

        for label, entities in sorted(by_label.items()):
            csv_buf = io.StringIO()
            w = csv.writer(csv_buf)
            w.writerow(_TG_VERTEX_COLS)
            for e in entities:
                w.writerow([
                    e.id,
                    e.name_cn or "",
                    e.name_en or "",
                    e.name_abbr or "",
                    e.description or "",
                    e.confidence,
                ])
            zf.writestr(f"vertex_{label}.csv", csv_buf.getvalue())

        for rtype, rels in sorted(by_rel.items()):
            csv_buf = io.StringIO()
            w = csv.writer(csv_buf)
            w.writerow(_TG_EDGE_COLS)
            for rel in rels:
                w.writerow([rel.source_entity, rel.target_entity, rel.confidence])
            zf.writestr(f"edge_{rtype}.csv", csv_buf.getvalue())

    return buf.getvalue()


def export_html(db: Session, ontology_id: str) -> str:
    data = _collect_data(db, ontology_id)
    p = data["project"]
    emap = _entity_map(data["entities"])

    entity_rows = "".join(
        f"<tr><td>{_h(_entity_label(e, 'cn'))}</td>"
        f"<td>{_h(_entity_label(e, 'en'))}</td>"
        f"<td>{_h(e.type)}</td><td>{e.confidence}</td></tr>"
        for e in data["entities"]
    )

    relation_rows = "".join(
        f"<tr><td>{_h(row['source_cn'])}</td>"
        f"<td>{_h(row['target_cn'])}</td>"
        f"<td>{_h(row['type'])}</td><td>{row['confidence']}</td></tr>"
        for row in (_relation_row(r, emap) for r in data["relations"])
    )

    logic_rows = "".join(
        f"<tr><td>{_h(r.name_cn)}</td><td>{_h(r.name_en)}</td>"
        f"<td>{_h(r.function_type)}</td><td>{_h(r.definition)}</td><td>{r.confidence}</td></tr>"
        for r in data["logic_rules"]
    )

    def _rules_summary(rules: list | None) -> str:
        return "; ".join(f"{r.get('operation', '')} {r.get('target', '')}" for r in (rules or []))

    action_rows = "".join(
        f"<tr><td>{_h(a.name_cn)}</td><td>{_h(a.name_en)}</td>"
        f"<td>{_h(_rules_summary(a.rules))}</td><td>{a.confidence}</td></tr>"
        for a in data["actions"]
    )

    n_ent = len(data["entities"])
    n_rel = len(data["relations"])

    sections = f"""
<h2>实体 <span class="count">({n_ent})</span></h2>
<table>
<thead><tr><th>中文名</th><th>英文名</th><th>类型</th><th>置信度</th></tr></thead>
<tbody>{entity_rows or '<tr><td colspan="4">无</td></tr>'}</tbody>
</table>

<h2>关系 <span class="count">({n_rel})</span></h2>
<table>
<thead><tr><th>源实体</th><th>目标实体</th><th>关系类型</th><th>置信度</th></tr></thead>
<tbody>{relation_rows or '<tr><td colspan="4">无</td></tr>'}</tbody>
</table>
"""

    if data["logic_rules"]:
        sections += f"""
<h2>逻辑规则 <span class="count">({len(data['logic_rules'])})</span></h2>
<table>
<thead><tr><th>中文名</th><th>英文名</th><th>类型</th><th>定义</th><th>置信度</th></tr></thead>
<tbody>{logic_rows}</tbody>
</table>
"""

    if data["actions"]:
        sections += f"""
<h2>动作 <span class="count">({len(data['actions'])})</span></h2>
<table>
<thead><tr><th>中文名</th><th>英文名</th><th>规则</th><th>置信度</th></tr></thead>
<tbody>{action_rows}</tbody>
</table>
"""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{_h(p.name)}</title>
<style>
body{{font-family:sans-serif;padding:2rem;max-width:1200px;margin:0 auto}}
h1{{margin-bottom:.25rem}} .meta{{color:#666;margin-bottom:2rem}}
h2{{margin-top:2rem;border-bottom:1px solid #eee;padding-bottom:.5rem}}
.count{{font-size:.85rem;color:#888;font-weight:normal}}
table{{border-collapse:collapse;width:100%;margin-bottom:1rem}}
th,td{{border:1px solid #ddd;padding:8px;text-align:left}}
th{{background:#f5f5f5}}
</style></head>
<body>
<h1>{_h(p.name)}</h1>
<p class="meta">Domain: {_h(p.domain)} | Version: {_h(p.version)} | Entities: {n_ent} | Relations: {n_rel}</p>
{sections}
</body></html>"""
