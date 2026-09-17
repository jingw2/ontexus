def test_delete_ontology_cleans_up_neo4j_and_chroma(client, auth_headers, monkeypatch):
    """Deleting an ontology must also remove its Neo4j nodes/relations and its
    Chroma vector collection — those stores have no other owner once the
    Postgres row is gone, and previously nothing in the delete path ever
    called their (already-implemented) delete methods."""
    calls = {}

    class FakeNeo4j:
        def delete_by_ontology(self, ontology_id):
            calls["neo4j_ontology_id"] = ontology_id
            return 3

    class FakeChroma:
        def delete_collection(self, name):
            calls["chroma_collection"] = name
            return True

    monkeypatch.setattr("app.services.v2.graph.neo4j_service.Neo4jService", FakeNeo4j)
    monkeypatch.setattr("app.services.v2.vector.chroma_service.ChromaService", FakeChroma)

    r = client.post("/api/v1/ontologies", json={"name": "CleanupTest", "domain": "财务"}, headers=auth_headers)
    oid = r.json()["data"]["id"]
    r2 = client.delete(f"/api/v1/ontologies/{oid}", headers=auth_headers)
    assert r2.status_code == 204
    assert calls["neo4j_ontology_id"] == oid
    assert calls["chroma_collection"] == f"ontology_{oid}"


def test_delete_ontology_survives_neo4j_and_chroma_failure(client, auth_headers, monkeypatch):
    """External-store cleanup is best-effort: a Postgres delete that already
    committed must still return 204 even if Neo4j/Chroma cleanup blows up."""
    class BrokenNeo4j:
        def delete_by_ontology(self, ontology_id):
            raise ConnectionError("neo4j unreachable")

    class BrokenChroma:
        def delete_collection(self, name):
            raise ConnectionError("chroma unreachable")

    monkeypatch.setattr("app.services.v2.graph.neo4j_service.Neo4jService", BrokenNeo4j)
    monkeypatch.setattr("app.services.v2.vector.chroma_service.ChromaService", BrokenChroma)

    r = client.post("/api/v1/ontologies", json={"name": "CleanupFailTest", "domain": "财务"}, headers=auth_headers)
    oid = r.json()["data"]["id"]
    r2 = client.delete(f"/api/v1/ontologies/{oid}", headers=auth_headers)
    assert r2.status_code == 204


def test_create_ontology(client, auth_headers):
    r = client.post("/api/v1/ontologies",
                    json={"name": "供应链测试", "domain": "供应链"},
                    headers=auth_headers)
    assert r.status_code == 201
    assert r.json()["data"]["name"] == "供应链测试"

def test_duplicate_name_returns_409(client, auth_headers):
    client.post("/api/v1/ontologies", json={"name": "供应链测试", "domain": "供应链"}, headers=auth_headers)
    r = client.post("/api/v1/ontologies", json={"name": "供应链测试", "domain": "供应链"}, headers=auth_headers)
    assert r.status_code == 409

def test_invalid_domain_returns_422(client, auth_headers):
    r = client.post("/api/v1/ontologies", json={"name": "Test", "domain": "invalid"}, headers=auth_headers)
    assert r.status_code == 422

def test_list_ontologies(client, auth_headers):
    client.post("/api/v1/ontologies", json={"name": "A", "domain": "供应链"}, headers=auth_headers)
    client.post("/api/v1/ontologies", json={"name": "B", "domain": "采购"}, headers=auth_headers)
    r = client.get("/api/v1/ontologies", headers=auth_headers)
    assert r.json()["data"]["total"] == 2

def test_list_ontologies_requires_auth(client):
    r = client.get("/api/v1/ontologies")
    assert r.status_code == 403

def test_get_ontology(client, auth_headers):
    r = client.post("/api/v1/ontologies", json={"name": "GetTest", "domain": "财务"}, headers=auth_headers)
    oid = r.json()["data"]["id"]
    r2 = client.get(f"/api/v1/ontologies/{oid}", headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["data"]["name"] == "GetTest"

def test_delete_ontology(client, auth_headers):
    r = client.post("/api/v1/ontologies", json={"name": "Del", "domain": "财务"}, headers=auth_headers)
    oid = r.json()["data"]["id"]
    r2 = client.delete(f"/api/v1/ontologies/{oid}", headers=auth_headers)
    assert r2.status_code == 204

def test_update_ontology(client, auth_headers):
    r = client.post("/api/v1/ontologies", json={"name": "Update", "domain": "医疗"}, headers=auth_headers)
    oid = r.json()["data"]["id"]
    r2 = client.put(f"/api/v1/ontologies/{oid}", json={"description": "updated desc"}, headers=auth_headers)
    assert r2.status_code == 200
    assert r2.json()["data"]["description"] == "updated desc"
