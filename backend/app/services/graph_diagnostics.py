"""Graph connectivity diagnostics (graph-engineering playbook, Section V.E):
after entity resolution, a few structural signals reveal whether resolution
actually worked. A single connected component means every entity is
reachable from every other — fragmented islands mean surface-form variants
that should have merged did not. The edge/node ratio gives a density read;
the highest-degree entities are the hub nodes a future summarization stage
would prioritize.

Pure functions only — no database access, no LLM calls — so this is callable
both from the extraction pipeline and, later, on demand for any ontology.
"""


def compute_graph_diagnostics(entities: list[dict], edges: list[tuple[str, str]], top_n: int = 5) -> dict:
    """`entities`: [{"id": ..., "name_cn": ...}, ...] every entity in the
    ontology. `edges`: [(source_id, target_id), ...] every relation,
    direction ignored (weak connectivity, per the playbook)."""
    ids = [e["id"] for e in entities if e.get("id")]
    names = {e["id"]: e.get("name_cn") or e["id"] for e in entities if e.get("id")}
    id_set = set(ids)

    adjacency: dict[str, list[str]] = {i: [] for i in ids}
    degree: dict[str, int] = {i: 0 for i in ids}
    edge_count = 0
    for source, target in edges:
        if source not in id_set or target not in id_set:
            continue  # a dangling reference is a separate concern (P0 validation)
        adjacency[source].append(target)
        adjacency[target].append(source)
        degree[source] += 1
        degree[target] += 1
        edge_count += 1

    components = _weakly_connected_components(ids, adjacency)
    component_sizes = sorted((len(c) for c in components), reverse=True)
    isolated = [i for i in ids if degree[i] == 0]
    hubs = sorted(ids, key=lambda i: degree[i], reverse=True)[:top_n]

    return {
        "entity_count": len(ids),
        "edge_count": edge_count,
        "edge_node_ratio": round(edge_count / len(ids), 2) if ids else 0.0,
        "connected_components": len(components),
        "largest_component_size": component_sizes[0] if component_sizes else 0,
        "isolated_entity_count": len(isolated),
        "isolated_entities": [names[i] for i in isolated[:top_n]],
        "hub_entities": [{"name_cn": names[i], "degree": degree[i]} for i in hubs if degree[i] > 0],
    }


def _weakly_connected_components(ids: list[str], adjacency: dict[str, list[str]]) -> list[set]:
    seen: set[str] = set()
    components: list[set] = []
    for start in ids:
        if start in seen:
            continue
        component = {start}
        stack = [start]
        seen.add(start)
        while stack:
            node = stack.pop()
            for neighbor in adjacency.get(node, []):
                if neighbor not in seen:
                    seen.add(neighbor)
                    component.add(neighbor)
                    stack.append(neighbor)
        components.append(component)
    return components
