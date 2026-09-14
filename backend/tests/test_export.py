import json


def _seed_entities_and_relation(client, auth_headers, oid: str):
    e1 = client.post(
        f"/api/v1/ontologies/{oid}/entities",
        json={"name_cn": "谵妄", "name_en": "Delirium"},
        headers=auth_headers,
    ).json()["data"]
    e2 = client.post(
        f"/api/v1/ontologies/{oid}/entities",
        json={"name_cn": "多动", "name_en": "Hyperactivity"},
        headers=auth_headers,
    ).json()["data"]
    client.post(
        f"/api/v1/ontologies/{oid}/graph/relations",
        json={
            "source_entity": e1["id"],
            "target_entity": e2["id"],
            "type": "is_a",
            "confidence": 0.92,
        },
        headers=auth_headers,
    )
    return e1, e2


def test_export_json_includes_relations(client, auth_headers, ontology):
    oid = ontology["id"]
    _seed_entities_and_relation(client, auth_headers, oid)
    r = client.get(f"/api/v1/ontologies/{oid}/export?format=json", headers=auth_headers)
    assert r.status_code == 200
    data = json.loads(r.content)
    assert len(data["entities"]) == 2
    assert len(data["relations"]) == 1
    rel = data["relations"][0]
    assert rel["source_cn"] == "谵妄"
    assert rel["target_cn"] == "多动"
    assert rel["type"] == "is_a"
    assert rel["confidence"] == 0.92


def test_export_csv_includes_relation_rows(client, auth_headers, ontology):
    oid = ontology["id"]
    _seed_entities_and_relation(client, auth_headers, oid)
    r = client.get(f"/api/v1/ontologies/{oid}/export?format=csv", headers=auth_headers)
    assert r.status_code == 200
    text = r.content.decode("utf-8")
    assert "record_type" in text
    assert "relation" in text
    assert "谵妄" in text
    assert "多动" in text
    assert "is_a" in text


def test_export_html_includes_relations_section(client, auth_headers, ontology):
    oid = ontology["id"]
    _seed_entities_and_relation(client, auth_headers, oid)
    r = client.get(f"/api/v1/ontologies/{oid}/export?format=html", headers=auth_headers)
    assert r.status_code == 200
    html = r.content.decode("utf-8")
    assert "关系" in html
    assert "谵妄" in html
    assert "多动" in html
    assert "is_a" in html


def test_export_ttl_includes_relation_triples(client, auth_headers, ontology):
    oid = ontology["id"]
    _seed_entities_and_relation(client, auth_headers, oid)
    r = client.get(f"/api/v1/ontologies/{oid}/export?format=ttl", headers=auth_headers)
    assert r.status_code == 200
    ttl = r.content.decode("utf-8")
    assert "owl:ObjectProperty" in ttl
    assert "rel_is_a" in ttl or "is_a" in ttl


def test_export_json_includes_full_action_fields(client, auth_headers, ontology):
    oid = ontology["id"]
    client.post(
        f"/api/v1/ontologies/{oid}/actions",
        json={
            "name_cn": "发起贷款申请",
            "name_en": "SubmitLoanApplication",
            "description": "借款人提交一笔新的贷款申请",
            "parameters": [{"name": "amount", "type": "number", "required": True}],
            "rules": [{"description": "授信额度校验"}],
            "submission_criteria": ["借款人信用状态正常"],
            "side_effects": [{"description": "创建一条贷款记录"}],
            "confidence": 0.9,
        },
        headers=auth_headers,
    )
    r = client.get(f"/api/v1/ontologies/{oid}/export?format=json", headers=auth_headers)
    assert r.status_code == 200
    data = json.loads(r.content)
    assert len(data["actions"]) == 1
    action = data["actions"][0]
    assert action["name_en"] == "SubmitLoanApplication"
    assert action["description"] == "借款人提交一笔新的贷款申请"
    assert action["parameters"] == [{"name": "amount", "type": "number", "required": True}]
    assert action["submission_criteria"] == ["借款人信用状态正常"]
    assert action["side_effects"] == [{"description": "创建一条贷款记录"}]
    assert action["confidence"] == 0.9
