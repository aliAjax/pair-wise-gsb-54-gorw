"""跨海光缆故障与抢修协调领域规则与状态转换。"""
from datetime import datetime
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, moment, number, text, text_list


INITIAL_STATE = "detected"
CREATE_ROLES = {'noc_operator'}
ACTION_ROLES = {'approve': {'repair_manager'}, 'mobilize': {'vessel_master'}, 'survey': {'cable_engineer'}, 'splice': {'cable_engineer'}, 'test': {'noc_operator'}, 'restore': {'noc_operator', 'repair_manager'}, 'cancel': {'repair_manager'}}
TRANSITIONS = {'approve': {'detected': 'approved'}, 'mobilize': {'approved': 'mobilized'}, 'survey': {'mobilized': 'surveyed'}, 'splice': {'surveyed': 'spliced'}, 'test': {'spliced': 'tested'}, 'restore': {'tested': 'restored'}, 'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled'}}

NOTICE_PENDING = "pending"
NOTICE_APPROVED = "approved"
NOTICE_REJECTED = "rejected"
NOTICE_REVOKED = "revoked"
NOTICE_SUPERSEDED = "superseded"
NOTICE_ACTIVE_STATES = {NOTICE_PENDING, NOTICE_APPROVED}
NOTICE_TRANSITIONS = {
    'approve_notice': {NOTICE_PENDING: NOTICE_APPROVED},
    'reject_notice': {NOTICE_PENDING: NOTICE_REJECTED},
    'revoke_notice': {NOTICE_PENDING: NOTICE_REVOKED, NOTICE_APPROVED: NOTICE_REVOKED},
}
NOTICE_ACTION_ROLES = {
    'register_notice': {'dispatcher'},
    'approve_notice': {'fisheries_officer'},
    'reject_notice': {'fisheries_officer'},
    'revoke_notice': {'dispatcher', 'fisheries_officer'},
    'reschedule_notice': {'dispatcher'},
}
NOTICE_REGISTERABLE_STATES = {'mobilized', 'surveyed', 'spliced', 'tested'}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        for roles in NOTICE_ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        if action in NOTICE_ACTION_ROLES:
            return role == "admin" or role in NOTICE_ACTION_ROLES[action]
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "cable")
        text(p, "segment")
        start = number(p, "start_km", 0)
        end = number(p, "end_km", 0)
        number(p, "depth_m", 1)
        integer(p, "sea_state", 0, 9)
        boolean(p, "vessel_available")
        number(p, "spare_length_km", 0)
        boolean(p, "permit_valid")
        integer(p, "capacity_gbps", 1)
        if end <= start:
            raise ValidationError("结束里程必须大于开始里程")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        distance = float(p["end_km"]) - float(p["start_km"])
        p["repair_distance_km"] = round(distance, 2)
        p["required_spare_km"] = round(distance * 1.05, 2)
        p["estimated_repair_hours"] = round(distance / 2.0 + float(p["depth_m"]) / 100.0 + int(p["sea_state"]) * 2.0, 2)
        p["repair_feasible"] = bool(p["vessel_available"] and p["permit_valid"] and p["spare_length_km"] >= p["required_spare_km"] and int(p["sea_state"]) <= 5)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"restored", "cancelled"} or item["payload"].get("cable") != payload.get("cable") or item["payload"].get("segment") != payload.get("segment"):
                continue
            if float(payload["start_km"]) < float(item["payload"].get("end_km", 0)) and float(payload["end_km"]) > float(item["payload"].get("start_km", 0)):
                raise Conflict("同一光缆区段已有未结束抢修")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "approve":
            if not bool(p["permit_valid"]) or not bool(p["vessel_available"]):
                raise ValidationError("许可或船舶条件不满足")
            changes["repair_manager"] = text(data, "repair_manager")
            summary = "抢修方案已批准"
        elif action == "mobilize":
            if float(data.get("weather_window_hours", 0)) < float(p["estimated_repair_hours"]):
                raise ValidationError("海况窗口不足以完成抢修")
            if float(data.get("available_spare_km", 0)) < float(p["required_spare_km"]):
                raise ValidationError("船上备缆不足")
            changes["weather_window_hours"] = float(data["weather_window_hours"])
            changes["vessel_name"] = text(data, "vessel_name")
            summary = "抢修船已动员"
        elif action == "survey":
            if not boolean(data, "survey_complete"):
                raise ValidationError("勘察尚未完成")
            fault_km = number(data, "fault_location_km", 0)
            if not (float(p["start_km"]) <= fault_km <= float(p["end_km"])):
                raise ValidationError("故障点不在申报区段")
            changes["fault_location_km"] = fault_km
            summary = "故障点勘察完成"
        elif action == "splice":
            loss = number(data, "splice_loss_db", 0)
            if loss > 0.2:
                raise ValidationError("接续损耗超过阈值")
            if float(data.get("spare_used_km", 0)) < float(p["repair_distance_km"]):
                raise ValidationError("备缆使用长度不足")
            changes["splice_loss_db"] = loss
            changes["spare_used_km"] = float(data["spare_used_km"])
            summary = "光缆接续完成"
        elif action == "test":
            end_loss = number(data, "end_to_end_loss_db", 0)
            if end_loss > 0.5:
                raise ValidationError("端到端损耗不合格")
            changes["end_to_end_loss_db"] = end_loss
            changes["test_passed"] = True
            summary = "系统测试通过"
        elif action == "restore":
            if not boolean(data, "traffic_restored"):
                raise ValidationError("业务流量尚未恢复")
            changes["traffic_restored"] = True
            changes["restore_capacity_gbps"] = integer(data, "restore_capacity_gbps", 1)
            summary = "通信恢复"
        elif action == "cancel":
            changes["cancel_reason"] = text(data, "cancel_reason")
            summary = "抢修取消"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    # ---- 航行通告 ----

    def validate_notice(self, data: Dict[str, Any]) -> Dict[str, Any]:
        sea_area = text(data, "sea_area")
        contact_no = text(data, "contact_no")
        start = moment(data, "start_at")
        end = moment(data, "end_at")
        if end <= start:
            raise ValidationError("作业结束时间必须晚于开始时间")
        return {"sea_area": sea_area, "contact_no": contact_no, "start_at": start, "end_at": end}

    def check_notice_overlap(self, sea_area: str, start: datetime, end: datetime, others: Iterable[Dict[str, Any]], exclude_record_id: int = None) -> None:
        for item in others:
            if item.get("state") not in NOTICE_ACTIVE_STATES:
                continue
            if item.get("sea_area") != sea_area:
                continue
            if exclude_record_id is not None and item.get("record_id") == exclude_record_id:
                continue
            if start.isoformat() < item["end_at"] and end.isoformat() > item["start_at"]:
                raise Conflict("同一海域时段重叠的通告只能有一份（冲突联系单：%s）" % item.get("contact_no", ""))

    def require_record_state_for_notice(self, record: Dict[str, Any]) -> None:
        if record["state"] not in NOTICE_REGISTERABLE_STATES:
            raise Conflict("抢修船动员后才能登记航行通告")

    def require_no_active_notice(self, latest: Optional[Dict[str, Any]]) -> None:
        if latest is not None and latest["state"] in NOTICE_ACTIVE_STATES:
            raise Conflict("当前联系单仍有效，不能重复登记；如需调整作业期请办理顺延")

    def require_notice_transition(self, notice: Dict[str, Any], action: str) -> str:
        allowed = NOTICE_TRANSITIONS.get(action, {}).get(notice["state"])
        if allowed is None:
            raise Conflict("通告当前状态不允许执行%s" % action)
        return allowed

    def validate_notice_action(self, action: str, data: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        if action == "approve_notice":
            changes = {"receipt_no": text(data, "receipt_no")}
            return changes, "渔政已同意作业，回执号%s" % changes["receipt_no"]
        if action == "reject_notice":
            changes = {"reject_reason": text(data, "reject_reason")}
            return changes, "渔政退回：%s" % changes["reject_reason"]
        if action == "revoke_notice":
            reason = text(data, "revoke_reason")
            changes = {"revoke_reason": reason}
            return changes, "通告已撤销：%s" % reason
        raise ValidationError("未知通告操作")

    def validate_reschedule(self, notice: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
        if notice["state"] != NOTICE_APPROVED:
            raise Conflict("只有已同意的通告才能办理作业期顺延")
        sea_area = text(data, "sea_area") if data.get("sea_area") else notice["sea_area"]
        contact_no = text(data, "contact_no") if data.get("contact_no") else notice["contact_no"]
        start = moment(data, "start_at") if data.get("start_at") else datetime.fromisoformat(notice["start_at"].replace("Z", "+00:00"))
        end = moment(data, "end_at")
        if end <= start:
            raise ValidationError("作业结束时间必须晚于开始时间")
        if end.isoformat() <= notice["end_at"]:
            raise ValidationError("顺延后的结束时间必须晚于原作业期")
        return {"sea_area": sea_area, "contact_no": contact_no, "start_at": start, "end_at": end}

    @staticmethod
    def notice_block_reason(latest: Optional[Dict[str, Any]], now: datetime) -> Optional[str]:
        if latest is None:
            return "航行通告尚未登记，渔政同意前勘察不能开始"
        state = latest["state"]
        if state == NOTICE_PENDING:
            if latest.get("supersedes_id"):
                return "作业期已顺延，原回执失效，新时段（联系单%s）尚待渔政确认，勘察不能开始" % latest.get("contact_no", "")
            return "等待渔政确认（联系单%s），勘察不能开始" % latest.get("contact_no", "")
        if state == NOTICE_REJECTED:
            return "渔政已退回（联系单%s，原因：%s），请换新联系单重新登记" % (latest.get("contact_no", ""), latest.get("reject_reason", ""))
        if state == NOTICE_REVOKED:
            return "通告已撤销（原因：%s），请重新登记" % latest.get("revoke_reason", "")
        if state == NOTICE_SUPERSEDED:
            return "原作业期已顺延，原回执失效，需按新时段重新取得渔政确认"
        if state == NOTICE_APPROVED:
            if now.isoformat() >= latest["end_at"]:
                return "回执%s已超过作业期，需重新确认" % latest.get("receipt_no", "")
            return None
        return "通告状态不可用于勘察"

    def notice_summary(self, latest: Optional[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
        if latest is None:
            return {"registered": False, "valid_receipt": None, "blocked": True, "block_reason": self.notice_block_reason(None, now)}
        block_reason = self.notice_block_reason(latest, now)
        valid = bool(latest["state"] == NOTICE_APPROVED and block_reason is None)
        return {
            "registered": True,
            "state": latest["state"],
            "contact_no": latest.get("contact_no"),
            "sea_area": latest.get("sea_area"),
            "start_at": latest.get("start_at"),
            "end_at": latest.get("end_at"),
            "receipt_no": latest.get("receipt_no") if valid else None,
            "valid_receipt": (
                {"receipt_no": latest.get("receipt_no"), "start_at": latest.get("start_at"), "end_at": latest.get("end_at"), "sea_area": latest.get("sea_area"), "contact_no": latest.get("contact_no")}
                if valid else None
            ),
            "reject_reason": latest.get("reject_reason"),
            "revoke_reason": latest.get("revoke_reason"),
            "blocked": block_reason is not None,
            "block_reason": block_reason,
        }
