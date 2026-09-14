"""Validator unit tests — pure functions, no API key, no network calls."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evals.document_extraction.validators import keyword_recall, dedup_gate, instance_leakage_gate, rule_leakage_gate

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
