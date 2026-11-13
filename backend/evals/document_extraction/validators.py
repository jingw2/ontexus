"""Pure-function validators for the document-extraction eval. No database,
no network calls — every function here takes a plain extraction-result dict
and returns findings, and is unit-testable without an LLM API key."""
import re

from rapidfuzz import fuzz

_CATEGORY_KEYS = ("entities", "relations", "logic_rules", "actions", "instances")

# ID/numbered-record pattern: a bare letter-prefix+digits code (CUS0001), or
# any name ending in a run of 3+ digits (客户001) — both are the shape of a
# specific record identifier, not a concept/type name.
_ID_PATTERN = re.compile(r"^[A-Za-z]+\d+$")
_TRAILING_DIGITS_PATTERN = re.compile(r"\d{3,}$")

# A letter (+ optional 1-2 digits) followed by a small closed set of
# tier/stage/round markers, and nothing else — the shape of an enumeration
# VALUE of some concept's property (信用分层's "A层", 逾期阶段's "M2+",
# 融资轮次's "D轮融资"), not a concept in its own right. Kept in sync with
# app.services.llm_service._looks_like_enum_value.
_ENUM_VALUE_PATTERN = re.compile(
    r"^[A-Za-z]{1,2}[+-]?(轮融资|阶段|层|级|档|类)$|^M\d{1,2}\+?(阶段)?$"
)

_RULE_NAME_SUFFIXES = ("规则", "政策", "办法", "制度", "准则", "细则", "规程")


def _all_text_blobs(extraction_result: dict) -> list[str]:
    """Every name_cn/description/entity_type string across all categories,
    concatenated per-item so a keyword match can span name+description."""
    blobs: list[str] = []
    for category in _CATEGORY_KEYS:
        for item in extraction_result.get(category) or []:
            if not isinstance(item, dict):
                continue
            parts = [
                str(item.get("name_cn") or ""),
                str(item.get("description") or ""),
                str(item.get("entity_type") or ""),
            ]
            blobs.append(" ".join(parts))
    return blobs


def keyword_recall(ground_truth: list[dict], extraction_result: dict) -> dict:
    blobs = _all_text_blobs(extraction_result)
    passed: list[dict] = []
    failed: list[dict] = []
    for requirement in ground_truth:
        keywords = requirement["required_keywords"]
        found = any(all(kw in blob for kw in keywords) for blob in blobs)
        (passed if found else failed).append(requirement)
    total = len(ground_truth)
    score = (len(passed) / total) if total else 1.0
    return {"score": score, "passed": passed, "failed": failed}


def precision_recall(gold_entities: list[str], extraction_result: dict, fuzzy_threshold: int = 85) -> dict:
    """Real precision/recall/F1 against a full gold-labeled entity list (not
    keyword_recall's loose "does this keyword appear somewhere" check).
    Fuzzy-matched (rapidfuzz, same threshold convention as dedup_gate) so a
    minor surface-form difference between the gold name and the extracted
    name_cn doesn't count as a miss — each gold entity and each extracted
    entity is consumed by at most one match, so duplicates on either side
    can't inflate the score."""
    extracted_names = [
        str(item.get("name_cn") or "") for item in (extraction_result.get("entities") or [])
        if isinstance(item, dict) and item.get("name_cn")
    ]
    unmatched_extracted = list(extracted_names)
    matched_gold: list[str] = []
    missed_gold: list[str] = []
    for gold_name in gold_entities:
        best_idx, best_score = None, -1
        for idx, candidate in enumerate(unmatched_extracted):
            score = fuzz.ratio(gold_name, candidate)
            if score > best_score:
                best_idx, best_score = idx, score
        if best_idx is not None and best_score >= fuzzy_threshold:
            matched_gold.append(gold_name)
            unmatched_extracted.pop(best_idx)
        else:
            missed_gold.append(gold_name)

    true_positive = len(matched_gold)
    false_negative = len(missed_gold)
    false_positive = len(unmatched_extracted)  # extracted but never matched a gold entity
    precision = true_positive / (true_positive + false_positive) if (true_positive + false_positive) else 1.0
    recall = true_positive / (true_positive + false_negative) if (true_positive + false_negative) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1": round(f1, 3),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "missed_gold_entities": missed_gold,
        "unmatched_extracted_entities": unmatched_extracted,
    }


def dedup_gate(extraction_result: dict, threshold: int = 85) -> list[dict]:
    findings: list[dict] = []
    for category in ("entities", "logic_rules", "actions"):
        items = extraction_result.get(category) or []
        names = [str(item.get("name_cn") or "") for item in items if isinstance(item, dict)]
        names = [n for n in names if n]
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                similarity = fuzz.ratio(names[i], names[j])
                if similarity >= threshold:
                    findings.append({
                        "category": category,
                        "name_a": names[i],
                        "name_b": names[j],
                        "similarity": similarity,
                    })
    return findings


def _looks_like_instance_id(name: str) -> bool:
    return bool(_ID_PATTERN.match(name)) or bool(_TRAILING_DIGITS_PATTERN.search(name))


def _looks_like_enum_value(name: str) -> bool:
    return bool(_ENUM_VALUE_PATTERN.match(name))


def instance_leakage_gate(extraction_result: dict) -> list[dict]:
    findings: list[dict] = []
    for item in extraction_result.get("entities") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name_cn") or "")
        if not name:
            continue
        if _looks_like_instance_id(name):
            findings.append({"name": name, "reason": "matches ID/numbered-record pattern"})
        elif _looks_like_enum_value(name):
            findings.append({"name": name, "reason": "matches tier/stage/round enum-value pattern"})
    return findings


def rule_leakage_gate(extraction_result: dict) -> list[dict]:
    """A business rule/policy name (e.g. "促销定价规则") is not an entity —
    it belongs in logic_rules."""
    findings: list[dict] = []
    for item in extraction_result.get("entities") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name_cn") or "")
        if name and name.endswith(_RULE_NAME_SUFFIXES):
            findings.append({"name": name, "reason": "matches business-rule/policy name pattern"})
    return findings
