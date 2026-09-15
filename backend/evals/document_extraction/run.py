"""CLI runner for the document-extraction eval. Mirrors
backend/evals/agent_ontology/run.py's --manifest/--adapter/--output shape."""
import argparse
import json
import os

from evals.document_extraction.validators import (
    dedup_gate, instance_leakage_gate, keyword_recall, precision_recall, rule_leakage_gate,
)


def run_eval(manifest_path: str, ground_truth_dir: str, adapter, repo_root: str) -> dict:
    with open(manifest_path, encoding="utf-8") as f:
        cases = json.load(f)

    case_results = []
    for case in cases:
        gt_path = os.path.join(ground_truth_dir, f"{case['ground_truth_id']}.json")
        with open(gt_path, encoding="utf-8") as f:
            ground_truth = json.load(f)

        # optional: a full gold entity list for real precision/recall/F1,
        # separate from the keyword-hint ground_truth above — a domain
        # without this file simply skips precision/recall, no breakage
        entities_gt_path = os.path.join(ground_truth_dir, f"{case['ground_truth_id']}.entities.json")
        gold_entities = None
        if os.path.exists(entities_gt_path):
            with open(entities_gt_path, encoding="utf-8") as f:
                gold_entities = json.load(f)

        source_path = os.path.join(repo_root, case["source_file"])
        try:
            extraction_result = adapter.extract(source_path)
            case_entry = {
                "case_id": case["case_id"],
                "domain": case["domain"],
                "keyword_recall": keyword_recall(ground_truth, extraction_result),
                "dedup_findings": dedup_gate(extraction_result),
                "leakage_findings": instance_leakage_gate(extraction_result),
                "rule_leakage_findings": rule_leakage_gate(extraction_result),
            }
            if gold_entities is not None:
                case_entry["precision_recall"] = precision_recall(gold_entities, extraction_result)
        except Exception as e:
            case_entry = {
                "case_id": case["case_id"],
                "domain": case["domain"],
                "error": str(e),
            }
        case_results.append(case_entry)

    total = len(case_results)
    recall_cases = [c for c in case_results if "keyword_recall" in c]
    mean_recall = (
        sum(c["keyword_recall"]["score"] for c in recall_cases) / len(recall_cases)
        if recall_cases else 0.0
    )
    pr_cases = [c for c in case_results if "precision_recall" in c]
    mean_f1 = sum(c["precision_recall"]["f1"] for c in pr_cases) / len(pr_cases) if pr_cases else None
    summary = {
        "total_cases": total,
        "mean_keyword_recall": mean_recall,
        "cases_with_dedup_findings": sum(1 for c in case_results if c.get("dedup_findings")),
        "cases_with_leakage_findings": sum(1 for c in case_results if c.get("leakage_findings")),
        "cases_with_rule_leakage_findings": sum(1 for c in case_results if c.get("rule_leakage_findings")),
        "cases_with_precision_recall": len(pr_cases),
        "mean_f1": mean_f1,
    }
    return {"cases": case_results, "summary": summary}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the document-extraction quality eval")
    parser.add_argument("--manifest", default="core-v1", help="manifest name (without .json)")
    parser.add_argument("--adapter", default="deepseek", choices=["deepseek", "fake"])
    parser.add_argument("--output", default=None, help="path to write the result JSON")
    args = parser.parse_args()

    eval_root = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(eval_root, "..", "..", ".."))

    if args.adapter == "fake":
        from evals.document_extraction.adapters import FakeExtractionAdapter
        adapter = FakeExtractionAdapter()
    else:
        if not os.environ.get("DEEPSEEK_API_KEY"):
            print("DEEPSEEK_API_KEY not set — skipping document-extraction eval (no-op, not an error)")
            if args.output:
                os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump({"skipped": True, "reason": "DEEPSEEK_API_KEY not set"}, f)
            return
        from evals.document_extraction.adapters import DeepSeekExtractionAdapter
        adapter = DeepSeekExtractionAdapter()

    result = run_eval(
        manifest_path=os.path.join(eval_root, "manifest", f"{args.manifest}.json"),
        ground_truth_dir=os.path.join(eval_root, "ground_truth"),
        adapter=adapter,
        repo_root=repo_root,
    )

    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
