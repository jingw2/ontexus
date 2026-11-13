"""PRD v1.1 Ontology Logic & Actions API"""
from __future__ import annotations
import uuid
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
from datetime import datetime, timezone
from app.database import SessionLocal
from app.deps import get_current_user, require_editor
from app.services.publication.working_copy import OntologyWorkingCopyService
from app.models.user import User
from app.models.v2.logic import OntologyLogicRule, OntologyStateMachine
from app.models.v2.action import OntologyActionType, OntologyActionRun
from app.services.ontology_access import GrantDenied, require_project_grant

router = APIRouter(dependencies=[Depends(get_current_user)])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _require_read(ontology_id: str, db: Session = Depends(get_db), actor: User = Depends(get_current_user)):
    try:
        require_project_grant(db, actor, ontology_id, "read")
    except GrantDenied as exc:
        raise HTTPException(status_code=404, detail="ONTOLOGY_NOT_FOUND") from exc


def _require_edit(ontology_id: str, db: Session = Depends(get_db), actor: User = Depends(require_editor)):
    try:
        require_project_grant(db, actor, ontology_id, "edit")
    except GrantDenied as exc:
        raise HTTPException(status_code=404, detail="ONTOLOGY_NOT_FOUND") from exc


# ── Logic Rules ─────────────────────────────────────────────────

class LogicRuleCreate(BaseModel):
    name: str
    logic_type: str
    description: str = ""
    target_entity_type: Optional[str] = None
    expression: dict = {}
    source_type: Optional[str] = None
    severity: str = "info"
    enabled: bool = True


class LogicRulePatch(BaseModel):
    name: Optional[str] = None
    logic_type: Optional[str] = None
    description: Optional[str] = None
    target_entity_type: Optional[str] = None
    expression: Optional[dict] = None
    source_type: Optional[str] = None
    severity: Optional[str] = None
    enabled: Optional[bool] = None


class LogicReviewRequest(BaseModel):
    enabled: Optional[bool] = None
    status: Optional[str] = None
    severity: Optional[str] = None
    notes: str = ""


class LogicTestRequest(BaseModel):
    row: dict = {}
    parameters: dict = {}


@router.get("/{ontology_id}/logic", dependencies=[Depends(_require_read)])
def list_logic_rules(ontology_id: str, logic_type: str = "", db: Session = Depends(get_db)):
    q = db.query(OntologyLogicRule).filter(OntologyLogicRule.ontology_id == ontology_id)
    if logic_type:
        q = q.filter(OntologyLogicRule.logic_type == logic_type)
    rules = q.order_by(OntologyLogicRule.created_at.desc()).all()
    return [{"id": r.id, "name": r.name, "logic_type": r.logic_type, "description": r.description,
             "target_entity_type": r.target_entity_type, "severity": r.severity, "enabled": r.enabled,
             "status": r.status, "version": r.version, "expression": r.expression,
             "source_type": r.source_type, "source_ref": r.source_ref,
             "created_at": r.created_at.isoformat() if r.created_at else None}
            for r in rules]


@router.post("/{ontology_id}/logic", status_code=201, dependencies=[Depends(_require_edit)])
def create_logic_rule(ontology_id: str, body: LogicRuleCreate, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    def _write():
        rule = OntologyLogicRule(
            ontology_id=ontology_id, name=body.name, logic_type=body.logic_type,
            description=body.description, target_entity_type=body.target_entity_type,
            expression=body.expression, source_type=body.source_type,
            severity=body.severity, enabled=body.enabled,
        )
        db.add(rule); db.flush()
        return {"id": rule.id, "name": rule.name, "status": rule.status}
    return OntologyWorkingCopyService.mutate(db, ontology_id=ontology_id, actor_id=current_user.id, operation="v2logic.create", callback=_write)


@router.put("/{ontology_id}/logic/{rule_id}", dependencies=[Depends(_require_edit)])
def update_logic_rule(ontology_id: str, rule_id: str, body: LogicRuleCreate, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    rule = db.query(OntologyLogicRule).filter(
        OntologyLogicRule.id == rule_id, OntologyLogicRule.ontology_id == ontology_id
    ).first()
    if not rule:
        raise HTTPException(404, "Logic rule not found")
    def _write():
        for k, v in body.model_dump(exclude_unset=True).items():
            setattr(rule, k, v)
        rule.updated_at = datetime.now(timezone.utc)
        return {"id": rule.id, "status": "updated"}
    return OntologyWorkingCopyService.mutate(db, ontology_id=ontology_id, actor_id=current_user.id, operation="v2logic.update", callback=_write)


@router.patch("/{ontology_id}/logic/{rule_id}", dependencies=[Depends(_require_edit)])
def patch_logic_rule(ontology_id: str, rule_id: str, body: LogicRulePatch, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    rule = db.query(OntologyLogicRule).filter(
        OntologyLogicRule.id == rule_id, OntologyLogicRule.ontology_id == ontology_id
    ).first()
    if not rule:
        raise HTTPException(404, "Logic rule not found")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "At least one editable field is required")
    def _write():
        for key, value in changes.items():
            setattr(rule, key, value)
        rule.version += 1
        rule.updated_at = datetime.now(timezone.utc)
        return {"id": rule.id, "status": "updated", "version": rule.version}
    return OntologyWorkingCopyService.mutate(db, ontology_id=ontology_id, actor_id=current_user.id, operation="v2logic.patch", callback=_write)


@router.post("/{ontology_id}/logic/{rule_id}/review", dependencies=[Depends(_require_edit)])
def review_logic_rule(ontology_id: str, rule_id: str, body: LogicReviewRequest, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    rule = db.query(OntologyLogicRule).filter(
        OntologyLogicRule.id == rule_id, OntologyLogicRule.ontology_id == ontology_id
    ).first()
    if not rule:
        raise HTTPException(404, "Logic rule not found")
    def _write():
        if body.enabled is not None:
            rule.enabled = body.enabled
        if body.severity is not None:
            rule.severity = body.severity
        if body.status is not None:
            if body.status not in ("draft", "reviewed", "disabled", "published"):
                raise HTTPException(400, "Invalid logic status")
            rule.status = body.status
        if body.notes:
            ref = dict(rule.source_ref or {})
            ref["review_notes"] = body.notes
            ref["reviewed_at"] = datetime.now(timezone.utc).isoformat()
            rule.source_ref = ref
        rule.updated_at = datetime.now(timezone.utc)
        return {"id": rule.id, "enabled": rule.enabled, "status": rule.status, "severity": rule.severity}
    return OntologyWorkingCopyService.mutate(db, ontology_id=ontology_id, actor_id=current_user.id, operation="v2logic.review", callback=_write)


@router.post("/{ontology_id}/logic/{rule_id}/test", dependencies=[Depends(_require_read)])
def test_logic_rule(ontology_id: str, rule_id: str, body: LogicTestRequest, db: Session = Depends(get_db), _=Depends(require_editor)):
    rule = db.query(OntologyLogicRule).filter(
        OntologyLogicRule.id == rule_id, OntologyLogicRule.ontology_id == ontology_id
    ).first()
    if not rule:
        raise HTTPException(404, "Logic rule not found")
    return {"rule_id": rule.id, **_evaluate_logic_rule(rule, body.row or {}, body.parameters or {})}


@router.delete("/{ontology_id}/logic/{rule_id}", dependencies=[Depends(_require_edit)])
def delete_logic_rule(ontology_id: str, rule_id: str, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    rule = db.query(OntologyLogicRule).filter(
        OntologyLogicRule.id == rule_id, OntologyLogicRule.ontology_id == ontology_id
    ).first()
    if not rule:
        raise HTTPException(404, "Logic rule not found")
    def _write():
        db.delete(rule); db.flush()
        return {"status": "deleted"}
    return OntologyWorkingCopyService.mutate(db, ontology_id=ontology_id, actor_id=current_user.id, operation="v2logic.delete", callback=_write)


@router.post("/{ontology_id}/logic/publish", dependencies=[Depends(_require_edit)])
def publish_logic_rules_v2(ontology_id: str, db: Session = Depends(get_db), _=Depends(require_editor)):
    """Direct tool publication is closed; use the Ontology compiler (P1C)."""
    raise HTTPException(status_code=403, detail="PUBLICATION_NOT_ENABLED")
    from app.models.logic import LogicRule as LogicRuleV1

    rules = db.query(OntologyLogicRule).filter(
        OntologyLogicRule.ontology_id == ontology_id,
        OntologyLogicRule.status != "published",
        OntologyLogicRule.enabled == True,  # noqa: E712
    ).all()
    for rule in rules:
        rule.status = "published"
        rule.updated_at = datetime.now(timezone.utc)

    v1_rules = db.query(LogicRuleV1).filter(
        LogicRuleV1.ontology_id == ontology_id,
        LogicRuleV1.status != "published",
    ).all()
    for rule in v1_rules:
        rule.status = "published"
    db.commit()
    return {"published_v2": len(rules), "published_v1": len(v1_rules), "status": "published"}


# ── Logic: Discovery ────────────────────────────────────────────

@router.post("/{ontology_id}/logic/discover", dependencies=[Depends(_require_edit)])
def discover_logic_rules(ontology_id: str, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    """发现 Logic Rules（同步写入 v2 + v1 表，供前端 LogicTab 读取）"""
    from app.services.v2.mapping.mapping_service import MappingService
    from app.models.logic import LogicRule as LogicRuleV1
    import uuid
    svc = MappingService(db)
    mappings = svc.get_mappings(ontology_id)
    created = []
    for m in mappings:
        name = f"Mapping: {m.entity_class}"
        exists = db.query(OntologyLogicRule).filter(
            OntologyLogicRule.ontology_id == ontology_id,
            OntologyLogicRule.name == name,
        ).first()
        if not exists:
            db.add(OntologyLogicRule(
                ontology_id=ontology_id, name=name, logic_type="mapping",
                description=f"Entity Mapping: {m.entity_class}",
                target_entity_type=m.entity_class,
                expression={"field_mapping": m.field_mapping},
                source_type="mapping", severity="info",
            ))
            # v1 表（前端 LogicTab 读取）
            if not db.query(LogicRuleV1).filter(
                LogicRuleV1.ontology_id == ontology_id, LogicRuleV1.name_cn == name,
            ).first():
                db.add(LogicRuleV1(
                    id=str(uuid.uuid4()), ontology_id=ontology_id,
                    name_cn=name, name_en=name,
                    description=f"Entity Mapping: {m.entity_class}",
                    function_type="external_query", definition=f"mapping:{m.entity_class}", confidence=0.85,
                    enabled=True, status="draft",
                    linked_entities=[m.entity_class],
                ))
            created.append(name)
    # PRD ④: 额外发现 Quality/Validation/State/Security/Automation 规则
    quality_rules = [
        ("Validation: 数据完整性检查", "validation", "完整性: 检查必填字段和 null 率", 0.8),
        ("Quality: 数据质量监控", "validation", "质量: 监控重复率和异常值", 0.75),
        ("Business: 业务规则推导", "business", "业务: 基于领域知识的业务规则", 0.7),
        ("Inference: 图推导关系", "inference", "推导: 基于实体关系推导隐含关联", 0.7),
        ("State: 状态流转规则", "state", "状态: 数据状态流转和审核流程", 0.75),
        ("Security: 访问控制", "security", "安全: 基于角色限制读写权限", 0.85),
        ("Automation: 自动同步触发", "automation", "自动化: Curated 审批通过后触发增量更新", 0.9),
    ]
    function_type_by_qtype = {
        "validation": "derived_property", "business": "derived_property", "state": "derived_property",
        "inference": "complex_edit", "security": "complex_edit", "automation": "complex_edit",
    }
    for qname, qtype, qdesc, qconf in quality_rules:
        if not db.query(LogicRuleV1).filter(
            LogicRuleV1.ontology_id == ontology_id, LogicRuleV1.name_cn == qname,
        ).first():
            db.add(LogicRuleV1(
                id=str(uuid.uuid4()), ontology_id=ontology_id,
                name_cn=qname, name_en=qname.replace(": ", "_"),
                description=qdesc, function_type=function_type_by_qtype.get(qtype, "derived_property"),
                definition=qtype, confidence=qconf,
                enabled=True, status="draft",
            ))
    db.flush()
    def _result():
        total_v1 = db.query(LogicRuleV1).filter(LogicRuleV1.ontology_id == ontology_id).count()
        total_v2 = db.query(OntologyLogicRule).filter(OntologyLogicRule.ontology_id == ontology_id).count()
        return {"discovered": len(created), "total_v2": total_v2, "total_v1": total_v1}
    return OntologyWorkingCopyService.mutate(db, ontology_id=ontology_id, actor_id=current_user.id, operation="v2logic.discover", callback=_result)


# ── State Machines ──────────────────────────────────────────────

@router.get("/{ontology_id}/state-machines", dependencies=[Depends(_require_read)])
def list_state_machines(ontology_id: str, db: Session = Depends(get_db)):
    machines = db.query(OntologyStateMachine).filter(
        OntologyStateMachine.ontology_id == ontology_id
    ).all()
    return [{"id": m.id, "entity_type_name": m.entity_type_name, "state_property": m.state_property,
             "states": m.states, "transitions": m.transitions} for m in machines]


# ── Action Types ────────────────────────────────────────────────

class ActionTypeCreate(BaseModel):
    name: str
    action_category: str
    description: str = ""
    target_entity_type: Optional[str] = None
    parameters: list = []
    submission_criteria: Optional[list] = None
    effects: list = []
    side_effects: Optional[list] = None
    permission_rules: Optional[list] = None
    enabled: bool = True


class ActionTypePatch(BaseModel):
    name: Optional[str] = None
    action_category: Optional[str] = None
    description: Optional[str] = None
    target_entity_type: Optional[str] = None
    parameters: Optional[list] = None
    submission_criteria: Optional[list] = None
    effects: Optional[list] = None
    side_effects: Optional[list] = None
    permission_rules: Optional[list] = None
    backed_by_function: Optional[str] = None
    enabled: Optional[bool] = None


class ActionRunRequest(BaseModel):
    target_object_id: Optional[str] = None
    parameters: dict = {}


class ActionReviewRequest(BaseModel):
    enabled: Optional[bool] = None
    status: Optional[str] = None
    permission_rules: Optional[list] = None
    submission_criteria: Optional[list] = None
    notes: str = ""


@router.get("/{ontology_id}/actions", dependencies=[Depends(_require_read)])
def list_action_types(ontology_id: str, category: str = "", db: Session = Depends(get_db)):
    q = db.query(OntologyActionType).filter(OntologyActionType.ontology_id == ontology_id)
    if category:
        q = q.filter(OntologyActionType.action_category == category)
    actions = q.order_by(OntologyActionType.created_at.desc()).all()
    return [{"id": a.id, "name": a.name, "action_category": a.action_category,
             "description": a.description, "target_entity_type": a.target_entity_type,
             "enabled": a.enabled, "status": a.status, "version": a.version,
             "parameters": a.parameters, "submission_criteria": a.submission_criteria,
             "effects": a.effects, "side_effects": a.side_effects,
             "permission_rules": a.permission_rules, "backed_by_function": a.backed_by_function,
             "created_at": a.created_at.isoformat() if a.created_at else None} for a in actions]


@router.post("/{ontology_id}/actions", status_code=201, dependencies=[Depends(_require_edit)])
def create_action_type(ontology_id: str, body: ActionTypeCreate, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    def _write():
        act = OntologyActionType(
            ontology_id=ontology_id, name=body.name, action_category=body.action_category,
            description=body.description, target_entity_type=body.target_entity_type,
            parameters=body.parameters, submission_criteria=body.submission_criteria,
            effects=body.effects, side_effects=body.side_effects,
            permission_rules=body.permission_rules, enabled=body.enabled,
        )
        db.add(act); db.flush()
        return {"id": act.id, "name": act.name, "status": act.status}
    return OntologyWorkingCopyService.mutate(db, ontology_id=ontology_id, actor_id=current_user.id, operation="v2action.create", callback=_write)


@router.patch("/{ontology_id}/actions/{action_id}", dependencies=[Depends(_require_edit)])
def patch_action_type(ontology_id: str, action_id: str, body: ActionTypePatch, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    act = db.query(OntologyActionType).filter(
        OntologyActionType.id == action_id, OntologyActionType.ontology_id == ontology_id
    ).first()
    if not act:
        raise HTTPException(404, "Action type not found")
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "At least one editable field is required")
    def _write():
        for key, value in changes.items():
            setattr(act, key, value)
        act.version += 1
        act.updated_at = datetime.now(timezone.utc)
        return {"id": act.id, "status": "updated", "version": act.version}
    return OntologyWorkingCopyService.mutate(db, ontology_id=ontology_id, actor_id=current_user.id, operation="v2action.patch", callback=_write)


@router.post("/{ontology_id}/actions/{action_id}/review", dependencies=[Depends(_require_edit)])
def review_action_type(ontology_id: str, action_id: str, body: ActionReviewRequest, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    act = db.query(OntologyActionType).filter(
        OntologyActionType.id == action_id, OntologyActionType.ontology_id == ontology_id
    ).first()
    if not act:
        raise HTTPException(404, "Action type not found")
    def _write():
        if body.enabled is not None:
            act.enabled = body.enabled
        if body.status is not None:
            if body.status not in ("draft", "reviewed", "disabled", "published"):
                raise HTTPException(400, "Invalid action status")
            act.status = body.status
        if body.permission_rules is not None:
            act.permission_rules = body.permission_rules
        if body.submission_criteria is not None:
            act.submission_criteria = body.submission_criteria
        if body.notes:
            side_effects = list(act.side_effects or [])
            side_effects.append({
                "type": "review_note",
                "notes": body.notes,
                "reviewed_at": datetime.now(timezone.utc).isoformat(),
            })
            act.side_effects = side_effects
        act.updated_at = datetime.now(timezone.utc)
        return {"id": act.id, "enabled": act.enabled, "status": act.status}
    return OntologyWorkingCopyService.mutate(db, ontology_id=ontology_id, actor_id=current_user.id, operation="v2action.review", callback=_write)


@router.post("/{ontology_id}/actions/discover", dependencies=[Depends(_require_edit)])
def discover_actions(ontology_id: str, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    """发现 Actions（同步写入 v2 + v1 表，供前端 ActionsTab 读取）"""
    from app.services.v2.mapping.mapping_service import MappingService
    from app.models.action import Action as ActionV1
    import uuid
    svc = MappingService(db)
    mappings = svc.get_mappings(ontology_id)
    created = []
    for m in mappings:
        name = f"Create {m.entity_class}"
        if not db.query(OntologyActionType).filter(
            OntologyActionType.ontology_id == ontology_id,
            OntologyActionType.name == name,
        ).first():
            db.add(OntologyActionType(
                ontology_id=ontology_id, name=name, action_category="crud",
                description=f"创建 {m.entity_class} 实体",
                target_entity_type=m.entity_class,
                parameters=[{"name": "data", "type": "object", "required": True}],
                effects=[{"action": "create_node", "entity_type": m.entity_class}],
            ))
            if not db.query(ActionV1).filter(
                ActionV1.ontology_id == ontology_id, ActionV1.name_cn == name,
            ).first():
                db.add(ActionV1(
                    id=str(uuid.uuid4()), ontology_id=ontology_id,
                    name_cn=name, name_en=name,
                    description=f"创建 {m.entity_class}", confidence=0.85,
                    enabled=True, status="draft",
                    linked_entities=[m.entity_class],
                ))
            created.append(name)
    # PRD ⑤: Review/Repair 动作类型
    for aname, acat, adesc, aconf in [
        ("Link: 关系维护", "link", "维护实体间的关系链接", 0.8),
        ("State: 状态流转", "state_transition", "执行实体状态迁移操作", 0.8),
        ("Review: 人工审核确认", "review", "对 pending_review 数据进行人工审核", 0.85),
        ("Repair: 数据质量修复", "repair", "修复质量报告中标记的异常数据", 0.75),
        ("Writeback: 外部写回", "writeback", "将审核通过的数据写回外部系统", 0.7),
    ]:
        if not db.query(ActionV1).filter(
            ActionV1.ontology_id == ontology_id, ActionV1.name_cn == aname,
        ).first():
            db.add(ActionV1(
                id=str(uuid.uuid4()), ontology_id=ontology_id,
                name_cn=aname, name_en=aname.replace(": ","_"),
                description=adesc, confidence=aconf,
                enabled=True, status="draft",
            ))
    db.flush()
    def _result():
        tv1 = db.query(ActionV1).filter(ActionV1.ontology_id == ontology_id).count()
        tv2 = db.query(OntologyActionType).filter(OntologyActionType.ontology_id == ontology_id).count()
        return {"discovered": len(created), "total_v2": tv2, "total_v1": tv1}
    return OntologyWorkingCopyService.mutate(db, ontology_id=ontology_id, actor_id=current_user.id, operation="v2action.discover", callback=_result)


@router.delete("/{ontology_id}/actions/{action_id}", dependencies=[Depends(_require_edit)])
def delete_action_type(ontology_id: str, action_id: str, db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    act = db.query(OntologyActionType).filter(
        OntologyActionType.id == action_id, OntologyActionType.ontology_id == ontology_id
    ).first()
    if not act:
        raise HTTPException(404, "Action type not found")
    def _write():
        db.delete(act); db.flush()
        return {"status": "deleted"}
    return OntologyWorkingCopyService.mutate(db, ontology_id=ontology_id, actor_id=current_user.id, operation="v2action.delete", callback=_write)


@router.post("/{ontology_id}/actions/publish", dependencies=[Depends(_require_edit)])
def publish_actions_v2(ontology_id: str, db: Session = Depends(get_db), _=Depends(require_editor)):
    """Direct tool publication is closed; use the Ontology compiler (P1C)."""
    raise HTTPException(status_code=403, detail="PUBLICATION_NOT_ENABLED")
    from app.models.action import Action as ActionV1

    actions = db.query(OntologyActionType).filter(
        OntologyActionType.ontology_id == ontology_id,
        OntologyActionType.status != "published",
        OntologyActionType.enabled == True,  # noqa: E712
    ).all()
    for action in actions:
        action.status = "published"
        action.updated_at = datetime.now(timezone.utc)

    v1_actions = db.query(ActionV1).filter(
        ActionV1.ontology_id == ontology_id,
        ActionV1.status != "published",
    ).all()
    for action in v1_actions:
        action.status = "published"
    db.commit()
    return {"published_v2": len(actions), "published_v1": len(v1_actions), "status": "published"}


# ── Action Runs ─────────────────────────────────────────────────

@router.get("/{ontology_id}/action-runs", dependencies=[Depends(_require_read)])
def list_action_runs(ontology_id: str, limit: int = 20, db: Session = Depends(get_db)):
    runs = db.query(OntologyActionRun).filter(
        OntologyActionRun.ontology_id == ontology_id
    ).order_by(OntologyActionRun.started_at.desc()).limit(limit).all()
    return [{"id": r.id, "action_type_id": r.action_type_id, "status": r.status,
             "target_object_id": r.target_object_id, "error": r.error,
             "started_at": r.started_at.isoformat() if r.started_at else None} for r in runs]


@router.post("/{ontology_id}/actions/{action_id}/run", dependencies=[Depends(_require_edit)])
def run_action_type(
    ontology_id: str,
    action_id: str,
    body: ActionRunRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_editor),
):
    from app.models.entity import Entity
    from app.models.relation import Relation

    action = db.query(OntologyActionType).filter(
        OntologyActionType.id == action_id,
        OntologyActionType.ontology_id == ontology_id,
    ).first()
    if not action:
        raise HTTPException(404, "Action type not found")
    if not action.enabled:
        raise HTTPException(400, "Action type is disabled")
    if action.status != "published":
        raise HTTPException(400, "Action type must be published before runtime execution")

    criteria_errors = _validate_action_submission(action, body.parameters or {}, body.target_object_id, db, ontology_id)
    if criteria_errors:
        raise HTTPException(400, {"errors": criteria_errors})

    run = OntologyActionRun(
        action_type_id=action.id,
        ontology_id=ontology_id,
        target_object_id=body.target_object_id,
        parameters=body.parameters or {},
        status="running",
        executed_by=getattr(current_user, "id", None),
    )
    db.add(run)
    db.commit()
    db.refresh(run)

    before_snapshot = {}
    after_snapshot = {}
    side_effect_results = []
    try:
        params = body.parameters or {}
        for effect in action.effects or []:
            effect_name = effect.get("action")
            if effect_name == "set_property":
                target_id = body.target_object_id or params.get("target_id")
                prop = effect.get("property")
                if not target_id or not prop:
                    raise ValueError("set_property requires target_object_id and effect.property")
                entity = db.query(Entity).filter(
                    Entity.id == target_id,
                    Entity.ontology_id == ontology_id,
                ).first()
                if not entity:
                    raise ValueError(f"Target entity not found: {target_id}")
                before_snapshot[target_id] = dict(entity.properties or {})
                props = dict(entity.properties or {})
                props[str(prop)] = params.get(str(prop))
                entity.properties = props
                entity.updated_at = datetime.now(timezone.utc)
                after_snapshot[target_id] = props
                side_effect_results.append({"action": effect_name, "target_id": target_id, "property": prop})

            elif effect_name == "create_object":
                data = dict(params.get("data") or {})
                entity_type = effect.get("entity_type") or action.target_entity_type or "Object"
                entity_id = str(uuid.uuid4())
                entity = Entity(
                    id=entity_id,
                    ontology_id=ontology_id,
                    name_cn=str(data.get("display_name") or data.get("name") or f"{entity_type} {entity_id[:8]}")[:200],
                    name_en=entity_type,
                    type=entity_type,
                    properties=data,
                    confidence=1.0,
                )
                db.add(entity)
                after_snapshot[entity_id] = data
                side_effect_results.append({"action": effect_name, "entity_id": entity_id, "entity_type": entity_type})

            elif effect_name in ("merge_relationship", "delete_relationship"):
                source_id = params.get("source_id")
                target_id = params.get("target_id")
                rel_type = effect.get("relation_type")
                if not source_id or not target_id or not rel_type:
                    raise ValueError(f"{effect_name} requires source_id, target_id and relation_type")
                if effect_name == "merge_relationship":
                    relation = Relation(
                        id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{ontology_id}:{source_id}:{rel_type}:{target_id}:action")),
                        ontology_id=ontology_id,
                        source_entity=source_id,
                        target_entity=target_id,
                        type=rel_type,
                        properties={"source": "action_runtime", "action_type_id": action.id},
                        confidence=1.0,
                    )
                    db.merge(relation)
                    side_effect_results.append({"action": effect_name, "relation_type": rel_type})
                else:
                    deleted = db.query(Relation).filter(
                        Relation.ontology_id == ontology_id,
                        Relation.source_entity == source_id,
                        Relation.target_entity == target_id,
                        Relation.type == rel_type,
                    ).delete()
                    side_effect_results.append({"action": effect_name, "relation_type": rel_type, "deleted": deleted})

            else:
                side_effect_results.append({"action": effect_name or "unknown", "status": "skipped"})

        run.status = "completed"
        run.before_snapshot = before_snapshot
        run.after_snapshot = after_snapshot
        run.side_effect_results = side_effect_results
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        return {"run_id": run.id, "status": run.status, "side_effect_results": side_effect_results}
    except Exception as e:
        db.rollback()
        db.add(run)
        run.status = "failed"
        run.error = str(e)
        run.before_snapshot = before_snapshot or None
        run.after_snapshot = after_snapshot or None
        run.side_effect_results = side_effect_results or None
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        raise HTTPException(400, {"run_id": run.id, "error": str(e)})


def _evaluate_logic_rule(rule: OntologyLogicRule, row: dict, parameters: dict) -> dict:
    expr = dict(rule.expression or {})
    op = str(expr.get("operator") or expr.get("op") or "").lower()
    field = expr.get("field") or expr.get("column") or expr.get("property")
    value = row.get(field) if field else None

    if not op and rule.logic_type in ("mapping", "inference", "automation"):
        return {"status": "skipped", "passed": None, "reason": f"{rule.logic_type} rule is declarative"}
    if not op and expr.get("missing_count") is not None:
        return {"status": "completed", "passed": int(expr.get("missing_count") or 0) == 0}
    if not op:
        return {"status": "skipped", "passed": None, "reason": "No executable operator configured"}

    expected = expr.get("value", parameters.get(str(field)) if field else None)
    try:
        if op in ("required", "non_empty"):
            passed = value not in (None, "")
        elif op in ("equals", "eq"):
            passed = str(value) == str(expected)
        elif op in ("not_equals", "ne"):
            passed = str(value) != str(expected)
        elif op in ("gt", "gte", "lt", "lte"):
            left = float(value)
            right = float(expected)
            passed = {
                "gt": left > right,
                "gte": left >= right,
                "lt": left < right,
                "lte": left <= right,
            }[op]
        elif op == "in":
            choices = expected if isinstance(expected, list) else []
            passed = value in choices or str(value) in {str(v) for v in choices}
        else:
            return {"status": "skipped", "passed": None, "reason": f"Unsupported operator: {op}"}
        return {"status": "completed", "passed": bool(passed), "operator": op, "field": field}
    except Exception as e:
        return {"status": "failed", "passed": False, "error": str(e), "operator": op, "field": field}


def _validate_action_submission(action: OntologyActionType, params: dict, target_object_id: str | None,
                                db: Session, ontology_id: str) -> list[dict]:
    from app.models.entity import Entity

    errors: list[dict] = []
    for item in action.parameters or []:
        if not isinstance(item, dict) or not item.get("required"):
            continue
        name = item.get("name")
        if name and params.get(str(name)) in (None, ""):
            errors.append({"type": "missing_parameter", "parameter": name})

    for criterion in action.submission_criteria or []:
        if not isinstance(criterion, dict):
            continue
        ctype = str(criterion.get("type") or "").lower()
        if not ctype and criterion.get("logic_type"):
            continue
        if ctype == "required_target" and not target_object_id and not params.get("target_id"):
            errors.append({"type": ctype, "message": "target_object_id is required"})
        elif ctype == "entity_exists":
            entity_id = target_object_id or params.get("target_id")
            if not entity_id or not db.query(Entity).filter(Entity.id == entity_id, Entity.ontology_id == ontology_id).first():
                errors.append({"type": ctype, "message": "target entity does not exist"})
        elif ctype == "field_equals":
            field = criterion.get("field")
            expected = criterion.get("value")
            entity_id = target_object_id or params.get("target_id")
            entity = db.query(Entity).filter(Entity.id == entity_id, Entity.ontology_id == ontology_id).first() if entity_id else None
            actual = (entity.properties or {}).get(field) if entity and field else None
            if str(actual) != str(expected):
                errors.append({"type": ctype, "field": field, "expected": expected, "actual": actual})
        elif ctype == "required_param":
            name = criterion.get("name")
            if name and params.get(str(name)) in (None, ""):
                errors.append({"type": ctype, "parameter": name})
    return errors
