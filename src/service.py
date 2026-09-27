"""业务用例编排、权限检查与审计。"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, PermissionDenied, text, to_iso
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _attach_notice(self, record: Dict[str, Any]) -> Dict[str, Any]:
        latest = self.repository.latest_notices_by_record([record["id"]]).get(record["id"])
        record["navigational_notice"] = self.rules.notice_summary(latest, self._now())
        return record

    def _attach_notices(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        latest_map = self.repository.latest_notices_by_record([record["id"] for record in records])
        now = self._now()
        for record in records:
            record["navigational_notice"] = self.rules.notice_summary(latest_map.get(record["id"]), now)
        return records

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self._attach_notice(self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id))

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        records = self.repository.list_records(state=state, limit=limit)
        return self._attach_notices(records)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self._attach_notice(self.repository.get(record_id))

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        if action == "survey":
            latest = self.repository.latest_notices_by_record([record_id]).get(record_id)
            block_reason = self.rules.notice_block_reason(latest, self._now())
            if block_reason is not None:
                raise Conflict(block_reason)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        saved = self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )
        return self._attach_notice(saved)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()

    # ---- 航行通告用例 ----

    def register_notice(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "register_notice"):
            raise PermissionDenied("角色无权登记航行通告")
        record = self.repository.get(record_id)
        self.rules.require_record_state_for_notice(record)
        fields = self.rules.validate_notice(data or {})
        latest = self.repository.latest_notices_by_record([record_id]).get(record_id)
        self.rules.require_no_active_notice(latest)
        # 海域时段唯一性最终由仓储事务兜底，这里提供跨记录的预检查
        self.rules.check_notice_overlap(
            fields["sea_area"], fields["start_at"], fields["end_at"],
            self.repository.latest_notices_by_record().values(),
            exclude_record_id=record_id,
        )
        return self.repository.insert_notice(
            record_id=record_id,
            sea_area=fields["sea_area"],
            contact_no=fields["contact_no"],
            start_at=to_iso(fields["start_at"]),
            end_at=to_iso(fields["end_at"]),
            actor_id=actor.user_id,
        )

    def list_notices(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_notices_for_record(record_id)

    def notice_action(self, actor: Actor, notice_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if action not in {"approve_notice", "reject_notice", "revoke_notice"}:
            raise Conflict("不支持的通告操作")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该通告操作")
        notice = self.repository.get_notice(notice_id)
        new_state = self.rules.require_notice_transition(notice, action)
        changes, summary = self.rules.validate_notice_action(action, data or {})
        return self.repository.mutate_notice(
            notice_id=notice_id,
            expected_version=int(expected_version),
            state=new_state,
            fields=changes,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": notice["state"], "to": new_state},
        )

    def reschedule_notice(self, actor: Actor, notice_id: int, expected_version: int, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_action(actor.role, "reschedule_notice"):
            raise PermissionDenied("角色无权办理作业期顺延")
        notice = self.repository.get_notice(notice_id)
        fields = self.rules.validate_reschedule(notice, data or {})
        saved = self.repository.supersede_and_insert(
            notice_id=notice_id,
            expected_version=int(expected_version),
            fields={"sea_area": fields["sea_area"], "contact_no": fields["contact_no"], "start_at": to_iso(fields["start_at"]), "end_at": to_iso(fields["end_at"])},
            actor_id=actor.user_id,
        )
        return saved
