"""Validator unit tests — pure functions, no API key, no network calls."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evals.document_extraction.validators import (
    keyword_recall, dedup_gate, instance_leakage_gate, rule_leakage_gate, precision_recall,
)

SAMPLE_RESULT = {
    "entities": [
        {"name_cn": "借款人", "description": "申请贷款的个人"},
        {"name_cn": "信用分层", "description": "按信用评分划分的等级"},
    ],
    "relations": [],
    "logic_rules": [
        {"name_cn": "授信额度规则", "description": "根据信用评分确定授信额度"},
    ],
    "actions": [],
    "instances": [],
}


def test_keyword_recall_all_pass():
    ground_truth = [
        {"category": "entity", "required_keywords": ["借款人"]},
        {"category": "entity", "required_keywords": ["信用"]},
        {"category": "logic_rule", "required_keywords": ["授信额度"]},
    ]
    result = keyword_recall(ground_truth, SAMPLE_RESULT)
    assert result["score"] == 1.0
    assert len(result["passed"]) == 3
    assert len(result["failed"]) == 0


def test_keyword_recall_partial_failure():
    ground_truth = [
        {"category": "entity", "required_keywords": ["借款人"]},
        {"category": "entity", "required_keywords": ["不存在的关键词"]},
    ]
    result = keyword_recall(ground_truth, SAMPLE_RESULT)
    assert result["score"] == 0.5
    assert len(result["passed"]) == 1
    assert len(result["failed"]) == 1
    assert result["failed"][0]["required_keywords"] == ["不存在的关键词"]


def test_keyword_recall_searches_across_all_categories_not_just_stated_one():
    # "required_keywords" states category "logic_rule" but the keyword
    # actually only appears in an entity — must still count as found,
    # per this plan's Global Constraint that category is informational only.
    ground_truth = [{"category": "logic_rule", "required_keywords": ["借款人"]}]
    result = keyword_recall(ground_truth, SAMPLE_RESULT)
    assert result["score"] == 1.0


def test_dedup_gate_flags_near_duplicate_entity_names():
    result = {
        "entities": [
            {"name_cn": "产品研发部", "description": "负责产品开发"},
            {"name_cn": "产品研发部门", "description": "负责新产品研发工作"},
            {"name_cn": "市场部", "description": "负责市场推广"},
        ],
        "relations": [], "logic_rules": [], "actions": [], "instances": [],
    }
    findings = dedup_gate(result)
    assert len(findings) == 1
    assert {findings[0]["name_a"], findings[0]["name_b"]} == {"产品研发部", "产品研发部门"}
    assert findings[0]["category"] == "entities"


def test_dedup_gate_clean_when_no_near_duplicates():
    result = {
        "entities": [
            {"name_cn": "借款人", "description": "x"},
            {"name_cn": "资金方", "description": "y"},
        ],
        "relations": [], "logic_rules": [], "actions": [], "instances": [],
    }
    assert dedup_gate(result) == []


def test_instance_leakage_gate_flags_id_pattern_entity_names():
    result = {
        "entities": [
            {"name_cn": "客户", "description": "概念实体"},
            {"name_cn": "CUS0001", "description": "leaked instance"},
            {"name_cn": "客户001", "description": "leaked instance"},
        ],
        "relations": [], "logic_rules": [], "actions": [], "instances": [],
    }
    findings = instance_leakage_gate(result)
    flagged_names = {f["name"] for f in findings}
    assert flagged_names == {"CUS0001", "客户001"}


def test_instance_leakage_gate_clean_for_concept_names():
    result = {
        "entities": [
            {"name_cn": "客户", "description": "概念实体"},
            {"name_cn": "信用分层", "description": "概念实体"},
        ],
        "relations": [], "logic_rules": [], "actions": [], "instances": [],
    }
    assert instance_leakage_gate(result) == []


def test_instance_leakage_gate_flags_enum_value_entity_names():
    result = {
        "entities": [
            {"name_cn": "信用分层", "description": "概念实体"},
            {"name_cn": "A层", "description": "leaked enum value"},
            {"name_cn": "M2+", "description": "leaked enum value"},
            {"name_cn": "D轮融资", "description": "leaked enum value"},
        ],
        "relations": [], "logic_rules": [], "actions": [], "instances": [],
    }
    findings = instance_leakage_gate(result)
    flagged_names = {f["name"] for f in findings}
    assert flagged_names == {"A层", "M2+", "D轮融资"}


def test_rule_leakage_gate_flags_business_rule_entity_names():
    result = {
        "entities": [
            {"name_cn": "借款人", "description": "概念实体"},
            {"name_cn": "促销定价规则", "description": "leaked business rule"},
            {"name_cn": "风险准入政策", "description": "leaked business rule"},
        ],
        "relations": [], "logic_rules": [], "actions": [], "instances": [],
    }
    findings = rule_leakage_gate(result)
    flagged_names = {f["name"] for f in findings}
    assert flagged_names == {"促销定价规则", "风险准入政策"}


def test_rule_leakage_gate_clean_for_concept_names():
    result = {
        "entities": [
            {"name_cn": "借款人", "description": "概念实体"},
            {"name_cn": "信用分层", "description": "概念实体"},
        ],
        "relations": [], "logic_rules": [], "actions": [], "instances": [],
    }
    assert rule_leakage_gate(result) == []


def test_precision_recall_perfect_match():
    gold = ["借款人", "信用分层"]
    result = {"entities": [{"name_cn": "借款人"}, {"name_cn": "信用分层"}]}
    pr = precision_recall(gold, result)
    assert pr["precision"] == 1.0
    assert pr["recall"] == 1.0
    assert pr["f1"] == 1.0
    assert pr["false_positive"] == 0
    assert pr["false_negative"] == 0


def test_precision_recall_counts_extra_extracted_entities_as_false_positives():
    gold = ["借款人"]
    result = {"entities": [{"name_cn": "借款人"}, {"name_cn": "促销定价规则"}]}
    pr = precision_recall(gold, result)
    assert pr["true_positive"] == 1
    assert pr["false_positive"] == 1
    assert pr["recall"] == 1.0
    assert pr["precision"] == 0.5
    assert pr["unmatched_extracted_entities"] == ["促销定价规则"]


def test_precision_recall_counts_missing_gold_entities_as_false_negatives():
    gold = ["借款人", "授信额度"]
    result = {"entities": [{"name_cn": "借款人"}]}
    pr = precision_recall(gold, result)
    assert pr["true_positive"] == 1
    assert pr["false_negative"] == 1
    assert pr["precision"] == 1.0
    assert pr["recall"] == 0.5
    assert pr["missed_gold_entities"] == ["授信额度"]


def test_precision_recall_fuzzy_matches_minor_surface_form_differences():
    gold = ["产品研发部"]
    result = {"entities": [{"name_cn": "产品研发部门"}]}
    pr = precision_recall(gold, result)
    assert pr["true_positive"] == 1
    assert pr["missed_gold_entities"] == []


def test_precision_recall_does_not_double_count_one_extracted_entity_for_two_gold_entities():
    """Each extracted entity can satisfy at most one gold entity — a single
    lucky match must not inflate recall for an unrelated second gold entity."""
    gold = ["借款人", "还款人"]
    result = {"entities": [{"name_cn": "借款人"}]}
    pr = precision_recall(gold, result)
    assert pr["true_positive"] == 1
    assert pr["false_negative"] == 1


def test_precision_recall_empty_gold_is_perfect_precision_no_recall_penalty():
    pr = precision_recall([], {"entities": [{"name_cn": "任意实体"}]})
    assert pr["recall"] == 1.0  # nothing was required
    assert pr["false_positive"] == 1
    assert pr["precision"] == 0.0
