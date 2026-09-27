"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError, text
from .repository import Repository
from .rules import ACTIVE_NOTICE_STATUSES, TERMINAL_STATES, DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        records = self.repository.list_records(state=state, limit=limit)
        notices = self.repository.latest_notices([record["id"] for record in records])
        return [self._with_notice(record, notices.get(record["id"])) for record in records]

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        return self._with_notice(record, self.repository.latest_notice(record_id))

    def _with_notice(self, record: Dict[str, Any], notice: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        record["notice"] = self.rules.notice_view(record["state"], notice)
        return record

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        notice = self.repository.latest_notice(record_id) if action == "survey" else None
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {}, notice=notice)
        result = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        if new_state in TERMINAL_STATES:
            self._release_notice(record_id, actor.user_id, "通信恢复，通告占用释放" if new_state == "restored" else "抢修取消，通告占用释放")
        return result

    def _release_notice(self, record_id: int, actor_id: str, summary: str) -> None:
        notice = self.repository.latest_notice(record_id)
        if notice is None or notice["status"] not in ACTIVE_NOTICE_STATUSES:
            return
        self.repository.update_notice(
            notice["id"],
            notice["status"],
            {"status": "revoked"},
            actor_id,
            "notice_revoke",
            {"summary": summary, "notice_id": notice["id"], "contact_no": notice["contact_no"], "from": notice["status"], "to": "revoked", "auto": True},
        )

    def register_notice(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_notice_action(actor.role, "register"):
            raise PermissionDenied("角色无权登记航行通告")
        record = self.repository.get(record_id)
        if record["state"] != "mobilized":
            raise Conflict("动员后才能登记航行通告")
        latest = self.repository.latest_notice(record_id)
        if latest is not None and latest["status"] in ACTIVE_NOTICE_STATUSES:
            raise Conflict("已存在有效航行通告")
        prepared = self.rules.validate_notice(data or {})
        self.rules.check_notice_overlap(prepared, self.repository.active_notices())
        return self.repository.create_notice(
            record_id,
            prepared,
            actor.user_id,
            "notice_register",
            {"summary": "航行通告已登记", "area": prepared["area"], "start_time": prepared["start_time"], "end_time": prepared["end_time"], "contact_no": prepared["contact_no"]},
        )

    def list_notices(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_notices(record_id)

    def notice_action(self, actor: Actor, record_id: int, notice_id: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_notice_action(actor.role, action):
            raise PermissionDenied("角色无权执行该通告操作")
        record = self.repository.get(record_id)
        notice = self.repository.get_notice(notice_id)
        if notice["record_id"] != record["id"]:
            raise NotFound("通告不属于该记录")
        if record["state"] in TERMINAL_STATES:
            raise Conflict("抢修已结束，通告不可变更")
        data = data or {}
        if action == "resubmit":
            return self._resubmit_notice(actor.user_id, record_id, notice, data)
        new_status, updates, summary = self.rules.apply_notice_action(notice, action, data)
        if action == "extend":
            self.rules.check_notice_overlap(
                {"area": notice["area"], "start_time": updates["start_time"], "end_time": updates["end_time"]},
                self.repository.active_notices(exclude_id=notice_id),
            )
        details: Dict[str, Any] = {"summary": summary, "notice_id": notice_id, "contact_no": notice["contact_no"], "from": notice["status"], "to": new_status}
        if action == "reject":
            details["reject_reason"] = updates["reject_reason"]
        elif action == "confirm":
            details["receipt_no"] = updates["receipt_no"]
        elif action == "extend":
            details["start_time"] = updates["start_time"]
            details["end_time"] = updates["end_time"]
            details["voided_receipt_no"] = updates.get("voided_receipt_no", "")
        return self.repository.update_notice(notice_id, notice["status"], updates, actor.user_id, "notice_" + action, details)

    def _resubmit_notice(self, actor_id: str, record_id: int, notice: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        if notice["status"] != "rejected":
            raise Conflict("仅退回的通告可换新单重提")
        prepared = self.rules.validate_notice(data)
        if prepared["contact_no"] == notice["contact_no"]:
            raise ValidationError("重提需更换联系单号")
        self.rules.check_notice_overlap(prepared, self.repository.active_notices())
        return self.repository.create_notice(
            record_id,
            prepared,
            actor_id,
            "notice_resubmit",
            {"summary": "退回后换新单重提", "previous_notice_id": notice["id"], "reject_reason": notice.get("reject_reason", ""), "area": prepared["area"], "start_time": prepared["start_time"], "end_time": prepared["end_time"], "contact_no": prepared["contact_no"]},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
