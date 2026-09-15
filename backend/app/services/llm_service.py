import json
import re
import unicodedata
from typing import Any

# graph-engineering discipline (per the extraction playbook): vague predicates
# make a graph untraversable, so relations carrying only a vague type are
# dropped during normalization instead of polluting the graph
VAGUE_RELATION_TYPES = frozenset({
    "关联", "related_to", "related", "有关系", "关联关系", "interacts_with",
})


def extract_ontology(text: str, prompt_content: str, model_config: dict, model_name: str, retry_count: int = 3) -> dict:
    provider = model_config.get("provider", "openai")
    api_key = model_config.get("api_key", "")
    api_base = model_config.get("api_base")

    messages = [
        {"role": "system", "content": prompt_content},
        {"role": "user", "content": (
            "请从以下文档中尽可能全面地提取本体信息，以JSON格式返回。\n"
            "要求：\n"
            "1. entities 只放概念/类型实体（如供应商分级、产品类别），不要为文中提到的具体公司名、产品名、"
            "物料名等命名实例单独建 entity——这些具体实例请放入 instances 数组\n"
            "2. 只提取与文档主题直接相关的核心概念，跳过偶然提及与噪音（如口号、版权行、纯数字段落）\n"
            "3. 每个实体必须写一句基于本文档的一句话描述（用于消歧）；同一概念出现多种写法时只保留一个规范实体，"
            "不要为同义写法建重复实体\n"
            "4. 关系要密集——每个概念实体至少参与1条关系，重点识别概念间的层级（IS-A、PART-OF）关系；"
            "关系类型必须语义明确（IS-A、PART-OF、INSTANCE-OF、SUPPLIES 等），禁止使用\"关联\"这类模糊类型；"
            "每条关系的 source/target 必须取自已提取的实体\n"
            "5. 逻辑规则按 Palantir Ontology Functions 模型分类为 derived_property/aggregation/"
            "complex_edit/external_query 之一，写入 function_type + definition 字段，禁止输出 "
            "IF-THEN 自然语言公式\n"
            "6. 业务规则、政策、判断条件、定价/审批/校验类规则（如\"促销定价规则\"）不是实体，"
            "禁止作为 entity 或 instance 输出，必须放入 logic_rules 数组\n"
            "7. 属性的枚举值/分级/分档/阶段标签（如信用分层的\"A层\"\"B层\"、逾期阶段的\"M0\"\"M1\"、"
            "融资轮次的\"A轮融资\"）不是独立概念也不是命名实例，不要为它们创建 entity 或 instance，"
            "只需体现为所属概念某个属性的枚举值\n\n"
            f"文档内容：\n\n{text}"
        )},
    ]

    for attempt in range(retry_count):
        try:
            raw = _call_llm(provider, api_key, api_base, model_name, messages)
            return normalize_extracted_ontology(_parse_response(raw))
        except Exception as e:
            if attempt == retry_count - 1:
                raise
    return {}


def normalize_extracted_ontology(parsed: Any) -> dict:
    """Deterministic post-extraction normalization (graph-engineering
    discipline, bounded): drop junk entities, merge duplicate surface forms
    into one canonical entity, drop dangling/vague relations and deduplicate
    relation triples.  Entities gain a one-line grounded description when the
    model omitted it."""
    if not isinstance(parsed, dict):
        return parsed if isinstance(parsed, dict) else {}

    entities: list[dict] = []
    seen_names: dict[str, str] = {}  # normalized name -> canonical name_cn
    for entity in parsed.get("entities") or []:
        if not isinstance(entity, dict):
            continue
        name = _clean_entity_name(entity.get("name_cn"))
        if not name or _is_junk_entity_name(name) or _looks_like_rule_name(name) or _looks_like_enum_value(name):
            continue
        key = _normalize_name(name)
        canonical = seen_names.get(key)
        if canonical is not None:
            # duplicate surface form of an existing concept -> keep the first
            # canonical entity only (its description survives for disambiguation)
            continue
        seen_names[key] = name
        entity["name_cn"] = name
        if not entity.get("description"):
            entity["description"] = f"文档中的概念：{name}"
        entities.append(entity)

    entity_names = set(seen_names.values())
    relations: list[dict] = []
    seen_relations: set[tuple] = set()
    for relation in parsed.get("relations") or []:
        if not isinstance(relation, dict):
            continue
        source = _clean_entity_name(relation.get("source"))
        target = _clean_entity_name(relation.get("target"))
        rel_type = str(relation.get("type") or "").strip()
        if not source or not target or not rel_type:
            continue
        # dangling references (an endpoint the model never extracted) are
        # structural noise — drop them (playbook: every relation must connect
        # two extracted entities)
        if source not in entity_names or target not in entity_names:
            continue
        if rel_type in VAGUE_RELATION_TYPES:
            continue
        key = (source, rel_type, target)
        if key in seen_relations:
            continue
        seen_relations.add(key)
        relation["source"] = source
        relation["target"] = target
        relation["type"] = rel_type
        relations.append(relation)

    result = dict(parsed)
    result["entities"] = entities
    result["relations"] = relations
    return result


def _clean_entity_name(value: Any) -> str:
    """Trim and strip bracketed transliterations/annotations (e.g. the
    `（Supplier）` annotation a model may append) so surface forms merge."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    text = re.sub(r"[（(].*?[)）]", "", text).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def _normalize_name(name: str) -> str:
    """Case/width/space-insensitive key for duplicate detection."""
    return unicodedata.normalize("NFKC", name).strip().lower()


def _is_junk_entity_name(name: str) -> bool:
    """Names that are pure digits/symbols, whitespace-only, or structural
    artifacts (file paths, repeated punctuation) add no graph value."""
    if len(name) < 2:
        return True
    if name.isdigit():
        return True
    alnum = [ch for ch in name if ch.isalnum()]
    if not alnum:
        return True
    # a name that is a file path or a bare document filename
    if re.search(r"(?:^|/)[^/\s]+\.(?:md|docx?|csv|xlsx?|pdf|pptx?|json|txt)$", name, re.IGNORECASE):
        return True
    return False


_RULE_NAME_SUFFIXES = ("规则", "政策", "办法", "制度", "准则", "细则", "规程")


def _looks_like_rule_name(name: str) -> bool:
    """A business rule/policy (e.g. "促销定价规则") is not an entity — it
    belongs in logic_rules, not the concept graph."""
    return name.endswith(_RULE_NAME_SUFFIXES)


# A letter (+ optional 1-2 digits) followed by a small closed set of
# tier/stage/round markers, and nothing else — the shape of an enumeration
# VALUE of some concept's property (信用分层's "A层", 逾期阶段's "M0"/"M1+",
# 融资轮次's "D轮融资"), not a concept in its own right.
_ENUM_VALUE_PATTERN = re.compile(
    r"^[A-Za-z]{1,2}[+-]?(轮融资|阶段|层|级|档|类)$|^M\d{1,2}\+?(阶段)?$"
)


def _looks_like_enum_value(name: str) -> bool:
    return bool(_ENUM_VALUE_PATTERN.match(name))


def resolve_entities(
    entities: list[dict], model_config: dict, model_name: str,
    existing_entities: list[dict] | None = None,
) -> dict[str, str]:
    """Cross-document entity resolution (graph-engineering playbook, resolution
    stage): within each entity type, cluster surface-form variants of the same
    real-world concept using the extraction-time descriptions as disambiguation
    context — catches cases exact-name matching misses (abbreviation vs full
    name, alias vs formal name across files).  Returns an alias map
    {raw_name_cn: canonical_name_cn} covering every input name; unmatched names
    and failed calls fall back to identity so no entity is ever silently lost.

    `existing_entities` (playbook incremental-update guidance: resolve new
    entities against the existing canonical set, not against each other) are
    entities already saved on this ontology from a prior run. They are never
    renamed — if a new entity clusters with one of them, that existing
    entity's name_cn is forced as the canonical, so re-running extraction
    never silently relabels an already-published entity out from under its
    existing relations/instances."""
    alias_map: dict[str, str] = {}
    by_type: dict[str, list[dict]] = {}
    for e in entities:
        name = e.get("name_cn")
        if not name:
            continue
        alias_map[name] = name
        by_type.setdefault(e.get("type") or "", []).append(e)

    existing_by_type: dict[str, list[dict]] = {}
    existing_names: set[str] = set()
    for e in existing_entities or []:
        name = e.get("name_cn")
        if not name:
            continue
        existing_names.add(name)
        existing_by_type.setdefault(e.get("type") or "", []).append(e)

    provider = model_config.get("provider", "openai")
    api_key = model_config.get("api_key", "")
    api_base = model_config.get("api_base")

    for etype, group in by_type.items():
        existing_group = existing_by_type.get(etype, [])
        if len(group) + len(existing_group) < 2:
            continue
        entity_list = "\n".join(
            f"- {e['name_cn']}: {(e.get('description') or '').strip()[:100]}" for e in group
        )
        if existing_group:
            entity_list += "\n" + "\n".join(
                f"- [已存在] {e['name_cn']}: {(e.get('description') or '').strip()[:100]}"
                for e in existing_group
            )
        incremental_rule = (
            "\n标记为 [已存在] 的实体是本体里已经发布的规范实体：如果某个未标记的新实体与其属于"
            "同一概念，该簇的 canonical 必须使用 [已存在] 实体的名字（去掉标记），绝不能改名或"
            "另选一个新名字。"
            if existing_group else ""
        )
        system_prompt = (
            f"你是实体消歧专家。下面是同一类型（{etype or '未分类'}）从文档中提取的实体，"
            "其中部分可能是同一概念的不同写法（如全称/简称、中英文并列、别名）。请聚类："
            "每个输入名称必须出现在且仅出现在一个簇的 aliases 中；确实不同的概念各自单独成簇；"
            "结合描述判断，不要仅凭字面相似合并不同概念；canonical 取信息最完整、无歧义的写法。"
            f"{incremental_rule}\n\n"
            '只返回 JSON：{"clusters": [{"canonical": "规范名", "aliases": ["写法1", "写法2"]}]}'
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"实体列表：\n{entity_list}"},
        ]
        try:
            parsed = _parse_response(_call_llm(provider, api_key, api_base, model_name, messages))
        except Exception:
            continue  # resolution is best-effort; keep identity mapping on failure
        group_names = {e["name_cn"] for e in group}
        existing_group_names = {e["name_cn"] for e in existing_group}
        all_names = group_names | existing_group_names
        for cluster in (parsed.get("clusters") or []) if isinstance(parsed, dict) else []:
            if not isinstance(cluster, dict):
                continue
            canonical = _clean_entity_name(cluster.get("canonical"))
            aliases = [_clean_entity_name(a) for a in (cluster.get("aliases") or [])]
            aliases = [a for a in aliases if a and a in all_names]
            # an existing (already-published) entity in this cluster always
            # wins the canonical slot, regardless of what the model picked
            existing_in_cluster = [a for a in aliases if a in existing_group_names] + (
                [canonical] if canonical in existing_group_names else []
            )
            if existing_in_cluster:
                canonical = existing_in_cluster[0]
            elif not canonical or canonical not in group_names:
                continue
            for alias in aliases:
                if alias in group_names:  # only remap names from this run's own entities
                    alias_map[alias] = canonical
    return alias_map


def apply_entity_resolution(result: dict, alias_map: dict) -> dict:
    """Rewrite entities/relations/logic_rules/actions through an alias map from
    `resolve_entities()`, merging duplicate surface forms into their canonical
    entity (keeping the richer description/properties) and remapping every
    downstream name reference so nothing is left pointing at a dropped alias."""
    if not alias_map or all(k == v for k, v in alias_map.items()):
        return result  # no-op: nothing to merge

    def _remap(name: Any) -> Any:
        return alias_map.get(name, name) if isinstance(name, str) else name

    merged: dict[str, dict] = {}
    for e in result.get("entities") or []:
        name = e.get("name_cn")
        if not name:
            continue
        canonical = _remap(name)
        if canonical not in merged:
            e = dict(e)
            e["name_cn"] = canonical
            merged[canonical] = e
        else:
            existing = merged[canonical]
            if len(e.get("description") or "") > len(existing.get("description") or ""):
                existing["description"] = e["description"]
            if isinstance(e.get("properties"), dict):
                existing.setdefault("properties", {})
                for k, v in e["properties"].items():
                    existing["properties"].setdefault(k, v)

    result = dict(result)
    result["entities"] = list(merged.values())

    relations = []
    seen_rel: set[tuple] = set()
    for r in result.get("relations") or []:
        r = dict(r)
        r["source"] = _remap(r.get("source", ""))
        r["target"] = _remap(r.get("target", ""))
        key = (r["source"], r.get("type"), r["target"])
        if r["source"] and r["target"] and r["source"] != r["target"] and key not in seen_rel:
            seen_rel.add(key)
            relations.append(r)
    result["relations"] = relations

    for coll in ("logic_rules", "actions"):
        items = []
        for item in result.get(coll) or []:
            item = dict(item)
            linked = item.get("linked_entities")
            if isinstance(linked, list):
                item["linked_entities"] = list(dict.fromkeys(_remap(n) for n in linked))
            items.append(item)
        result[coll] = items

    return result


def infer_relations(entities: list, existing_relations: list, text: str,
                    model_config: dict, model_name: str) -> list:
    """Second-pass relation inference: find IS-A / PART-OF / INSTANCE-OF links the first pass missed."""
    if len(entities) < 3:
        return []

    provider  = model_config.get("provider", "openai")
    api_key   = model_config.get("api_key", "")
    api_base  = model_config.get("api_base")

    # Build entity snapshot (limit to 50 to keep prompt manageable)
    entity_lines = "\n".join(
        f"- {e.get('name_cn','?')} ({e.get('type','?')}): {(e.get('description') or '')[:60]}"
        for e in entities[:50]
    )
    existing_set = {
        (r.get("source"), r.get("type"), r.get("target"))
        for r in existing_relations
        if r.get("source") and r.get("target")
    }

    system_prompt = (
        "你是本体关系补全专家。给定已提取实体列表和原始文档，找出实体间遗漏的层级和关联关系。\n\n"
        "关系类型（只能使用以下类型，全部英文大写）：\n"
        "  IS-A、PART-OF、INSTANCE-OF、SUPPLIES、STORES、PROCESSES、TREATS、CAUSES、"
        "TRIGGERS、DEPENDS_ON、PRODUCES、HAS_STATUS、GOVERNED_BY、ASSIGNED_TO\n\n"
        "重点寻找：\n"
        "1. IS-A：A 是 B 的一种（如 销售费用 IS-A 费用）\n"
        "2. PART-OF：A 是 B 的组成部分（如 流动资产 PART-OF 资产）\n"
        "3. INSTANCE-OF：A 是 B 的具体实例（如 华为供应链 INSTANCE-OF S级战略客户）\n"
        "4. TRIGGERS：A 触发或发起 B（如 采购申请 TRIGGERS 采购订单）\n"
        "5. DEPENDS_ON：A 依赖 B（如 出货 DEPENDS_ON 库存）\n\n"
        "要求：\n"
        "- 禁止使用\"关联\"等模糊中文关系类型，必须从上方列表中选择语义明确的英文类型\n"
        "- 只输出新发现的关系，不要重复已有关系\n"
        "- source 和 target 必须是实体列表中的 name_cn\n"
        "- 每对实体最多一条关系\n"
        "- 至少找 15 条，最多 50 条\n\n"
        '返回 JSON（不要有其他文字）：{"relations": [{"source": "A", "target": "B", "type": "IS-A", "confidence": 0.85}]}'
    )
    user_msg = (
        f"已提取实体：\n{entity_lines}\n\n"
        f"文档节选：\n{text[:4000]}"
    )

    try:
        raw = _call_llm(provider, api_key, api_base, model_name,
                        [{"role": "system", "content": system_prompt},
                         {"role": "user", "content": user_msg}])
        parsed = _parse_response(raw)
        candidates = parsed.get("relations", []) if isinstance(parsed, dict) else (parsed if isinstance(parsed, list) else [])

        new_rels = []
        for r in candidates:
            if not isinstance(r, dict):
                continue
            key = (r.get("source"), r.get("type"), r.get("target"))
            if key[0] and key[2] and key not in existing_set:
                new_rels.append(r)
                existing_set.add(key)
        return new_rels
    except Exception:
        return []  # relation inference failure is non-fatal


def summarize_entity(
    name: str, description: str, relations_text: str, model_config: dict, model_name: str,
) -> dict | None:
    """Hub-node profile synthesis (graph-engineering playbook, Section V.A):
    for a high-degree entity, synthesize a 2-3 paragraph summary, 3-5 atomic
    traceable key facts, and a time range from its grounded description and
    its known graph relations. Returns None on failure — summarization is
    best-effort and never blocks extraction."""
    provider = model_config.get("provider", "openai")
    api_key = model_config.get("api_key", "")
    api_base = model_config.get("api_base")

    system_prompt = (
        f"为本体实体「{name}」生成知识图谱画像。\n\n"
        f"已知描述：{description or '（无）'}\n\n"
        f"该实体在图中的已知关系：\n{relations_text or '（无）'}\n\n"
        "基于以上信息写一段2-3段的事实性综合描述；如信息有冲突，优先采用更具体的说法；"
        "提炼3-5条可溯源到上述描述/关系的原子关键事实；不要编造描述和关系之外没有支持的事实；"
        "时间范围用 YYYY 或 YYYY-MM 格式，没有明确时间信息就填 \"unknown\"。\n\n"
        '只返回 JSON：{"summary": "...", "key_facts": ["...", "..."], '
        '"time_range": {"start": "...", "end": "..."}}'
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"实体：{name}"},
    ]
    try:
        parsed = _parse_response(_call_llm(provider, api_key, api_base, model_name, messages))
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    time_range = parsed.get("time_range")
    if not isinstance(time_range, dict):
        time_range = {"start": "unknown", "end": "unknown"}
    return {
        "summary": str(parsed.get("summary") or ""),
        "key_facts": [f for f in (parsed.get("key_facts") or []) if isinstance(f, str)][:5],
        "time_range": {
            "start": str(time_range.get("start") or "unknown"),
            "end": str(time_range.get("end") or "unknown"),
        },
    }


def _call_llm(provider: str, api_key: str, api_base: str | None, model: str, messages: list, json_mode: bool = True) -> str:
    # Stable seed for reproducibility: derived from message content so same input → same seed.
    import hashlib as _hashlib, json as _json
    try:
        _seed_src = _json.dumps([m.get("content", "")[:500] for m in messages], ensure_ascii=False, sort_keys=True)
        _seed = int(_hashlib.md5(_seed_src.encode()).hexdigest()[:8], 16)
    except Exception:
        _seed = 42

    if provider == "anthropic":
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model, max_tokens=8192, temperature=0,
            system=messages[0]["content"],
            messages=[{"role": "user", "content": messages[1]["content"] + ("\n\n```json\n{" if json_mode else "")}],
        )
        return ("{" + resp.content[0].text) if json_mode else resp.content[0].text
    else:
        import openai
        kwargs = {"api_key": api_key}
        if api_base:
            kwargs["base_url"] = api_base
        client = openai.OpenAI(**kwargs)
        create_kwargs: dict = {"model": model, "messages": messages, "timeout": 300, "max_tokens": 65536,
                               "temperature": 0, "seed": _seed}
        if json_mode:
            create_kwargs["response_format"] = {"type": "json_object"}
        try:
            resp = client.chat.completions.create(**create_kwargs)
        except Exception:
            # seed not supported by all providers — retry without it
            create_kwargs.pop("seed", None)
            resp = client.chat.completions.create(**create_kwargs)
        return resp.choices[0].message.content or ""


def chat_completion(
    provider: str, api_key: str, api_base: str | None, model: str, messages: list,
    *, tools: list | None = None, options: dict | None = None, timeout: float = 300,
    on_delta=None,
) -> dict:
    """Conversation chat completion (Agent Turn model call).

    OpenAI-compatible chat.completions for `openai`/`compatible` providers
    (honoring the pinned version's `options` like temperature/max_tokens and
    the optional `tools` schema for tool calling); `anthropic` maps the
    messages/tools onto the Messages API.  Returns a normalized dict with
    `content` (str) and `tool_calls` (list of {id, name, arguments_json}).

    `on_delta`, when given, streams the response from an OpenAI-compatible
    provider (`stream=True`) and is called with the cumulative answer text
    after every content chunk — lets the caller show the answer as it's
    generated instead of only once the full response returns. Ignored for
    `anthropic` (streaming not implemented there; falls back to blocking).
    """
    if provider == "anthropic":
        return _anthropic_chat_completion(api_key, model, messages, tools, timeout)
    import openai
    kwargs: dict = {"api_key": api_key, "timeout": timeout}
    if api_base:
        kwargs["base_url"] = api_base
    client = openai.OpenAI(**kwargs)
    create_kwargs: dict = {"model": model, "messages": messages}
    if tools:
        create_kwargs["tools"] = tools
    if options:
        if options.get("temperature") is not None:
            create_kwargs["temperature"] = float(options["temperature"])
        if options.get("max_tokens") is not None:
            create_kwargs["max_tokens"] = int(options["max_tokens"])
    if on_delta is not None:
        create_kwargs["stream"] = True
        try:
            stream = client.chat.completions.create(**create_kwargs)
        except TypeError:
            create_kwargs.pop("temperature", None)
            create_kwargs.pop("max_tokens", None)
            stream = client.chat.completions.create(**create_kwargs)
        return _consume_chat_stream(stream, on_delta)
    try:
        resp = client.chat.completions.create(**create_kwargs)
    except TypeError:
        # options unsupported by this provider surface — retry without them
        create_kwargs.pop("temperature", None)
        create_kwargs.pop("max_tokens", None)
        resp = client.chat.completions.create(**create_kwargs)
    message = resp.choices[0].message
    tool_calls = []
    for call in message.tool_calls or []:
        tool_calls.append({
            "id": call.id,
            "name": call.function.name,
            "arguments_json": call.function.arguments or "{}",
        })
    return {"content": message.content or "", "tool_calls": tool_calls}


def _consume_chat_stream(stream, on_delta) -> dict:
    """Accumulate an OpenAI-compatible streaming response into the same
    `{"content", "tool_calls"}` shape the blocking call returns. Tool-call
    argument fragments arrive over several chunks keyed by index — they are
    buffered and joined, never shown live (raw partial JSON isn't
    meaningful to a user); content fragments are shown live via `on_delta`."""
    content_parts: list[str] = []
    tool_call_slots: dict[int, dict[str, str]] = {}
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue
        if delta.content:
            content_parts.append(delta.content)
            on_delta("".join(content_parts))
        for tc_delta in delta.tool_calls or []:
            slot = tool_call_slots.setdefault(tc_delta.index, {"id": "", "name": "", "arguments": ""})
            if tc_delta.id:
                slot["id"] = tc_delta.id
            if tc_delta.function and tc_delta.function.name:
                slot["name"] = tc_delta.function.name
            if tc_delta.function and tc_delta.function.arguments:
                slot["arguments"] += tc_delta.function.arguments
    tool_calls = [
        {"id": slot["id"], "name": slot["name"], "arguments_json": slot["arguments"] or "{}"}
        for _, slot in sorted(tool_call_slots.items())
    ]
    return {"content": "".join(content_parts), "tool_calls": tool_calls}


def _anthropic_chat_completion(api_key: str, model: str, messages: list, tools: list | None,
                               timeout: float = 300) -> dict:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
    system = None
    api_messages = []
    for message in messages:
        if message.get("role") == "system":
            system = (system or "") + (message.get("content") or "")
        elif message.get("role") == "tool":
            api_messages.append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": message.get("tool_call_id", ""),
                             "content": message.get("content") or ""}],
            })
        else:
            content = message.get("content") or ""
            if message.get("tool_calls"):
                blocks = [{"type": "text", "text": content}]
                for call in message["tool_calls"]:
                    try:
                        import json as _json
                        payload = _json.loads(call.get("arguments_json") or "{}")
                    except Exception:
                        payload = {}
                    blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"],
                                   "input": payload})
                api_messages.append({"role": "user" if message["role"] == "assistant" else message["role"],
                                     "content": blocks})
            else:
                api_messages.append({"role": message["role"], "content": content})
    api_tools = None
    if tools:
        api_tools = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                      "input_schema": t["function"]["parameters"]} for t in tools]
    kwargs: dict = {"model": model, "max_tokens": 8192, "messages": api_messages}
    if system:
        kwargs["system"] = system
    if api_tools:
        kwargs["tools"] = api_tools
    resp = client.messages.create(**kwargs)
    tool_calls = []
    content_text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use":
            import json as _json
            tool_calls.append({"id": block.id, "name": block.name,
                               "arguments_json": _json.dumps(block.input, ensure_ascii=False)})
        else:
            content_text += getattr(block, "text", "") or ""
    return {"content": content_text, "tool_calls": tool_calls}



def _parse_response(raw: str) -> dict:
    if not raw:
        raise ValueError("Empty LLM response")

    # Strip markdown code fences (```json ... ``` or ``` ... ```)
    text = raw.strip()
    text = re.sub(r'^```(?:json)?\s*\n?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\n?```\s*$', '', text).strip()

    # Remove control characters that are illegal inside JSON strings
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)

    # Fast path: well-formed JSON
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try json_repair (handles unescaped quotes, truncated output, etc.)
    try:
        from json_repair import repair_json
        repaired = repair_json(text)
        result = json.loads(repaired)
        if isinstance(result, dict):
            return result
    except Exception:
        pass

    # Last resort: slice from first { to last } and try again
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Cannot parse LLM response as JSON: {raw[:300]}")
