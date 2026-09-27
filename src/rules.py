"""跨海光缆故障与抢修协调领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, iso_time, number, optional_text, parse_time, text, text_list


INITIAL_STATE = "detected"
TERMINAL_STATES = {"restored", "cancelled"}
CREATE_ROLES = {'noc_operator'}
ACTION_ROLES = {'approve': {'repair_manager'}, 'mobilize': {'vessel_master'}, 'survey': {'cable_engineer'}, 'splice': {'cable_engineer'}, 'test': {'noc_operator'}, 'restore': {'noc_operator', 'repair_manager'}, 'cancel': {'repair_manager'}}
TRANSITIONS = {'approve': {'detected': 'approved'}, 'mobilize': {'approved': 'mobilized'}, 'survey': {'mobilized': 'surveyed'}, 'splice': {'surveyed': 'spliced'}, 'test': {'spliced': 'tested'}, 'restore': {'tested': 'restored'}, 'cancel': {'detected': 'cancelled', 'approved': 'cancelled', 'mobilized': 'cancelled'}}

NOTICE_ACTION_ROLES = {'register': {'dispatcher'}, 'confirm': {'fishery_admin'}, 'reject': {'fishery_admin'}, 'resubmit': {'dispatcher'}, 'extend': {'dispatcher'}, 'revoke': {'dispatcher'}}
NOTICE_TRANSITIONS = {'confirm': {'pending': 'approved'}, 'reject': {'pending': 'rejected'}, 'extend': {'pending': 'pending', 'approved': 'pending'}, 'revoke': {'pending': 'revoked', 'approved': 'revoked'}}
ACTIVE_NOTICE_STATUSES = {"pending", "approved"}


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
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def role_can_notice_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in NOTICE_ACTION_ROLES.get(action, set())

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

    def validate_notice(self, data: Dict[str, Any]) -> Dict[str, Any]:
        notice = {
            "area": text(data, "area"),
            "start_time": iso_time(data, "start_time"),
            "end_time": iso_time(data, "end_time"),
            "contact_no": text(data, "contact_no"),
        }
        if parse_time(notice["end_time"]) <= parse_time(notice["start_time"]):
            raise ValidationError("结束时间必须大于开始时间")
        return notice

    def check_notice_overlap(self, candidate: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        start = parse_time(candidate["start_time"])
        end = parse_time(candidate["end_time"])
        for item in existing:
            if item["area"] != candidate["area"]:
                continue
            if start < parse_time(item["end_time"]) and end > parse_time(item["start_time"]):
                raise Conflict("同一海域时段重叠的通告只能有一份")

    def apply_notice_action(self, notice: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        if action not in NOTICE_TRANSITIONS:
            raise ValidationError("未知通告操作%s" % action)
        new_status = NOTICE_TRANSITIONS[action].get(notice["status"])
        if new_status is None:
            raise Conflict("当前通告状态不允许执行%s" % action)
        data = dict(data or {})
        updates: Dict[str, Any] = {"status": new_status}
        if action == "confirm":
            updates["receipt_no"] = optional_text(data, "receipt_no") or ("RCPT-%06d-%d" % (int(notice["id"]), int(notice.get("revision", 1))))
            summary = "渔政已确认航行通告"
        elif action == "reject":
            updates["reject_reason"] = text(data, "reject_reason")
            summary = "航行通告被退回"
        elif action == "extend":
            start = iso_time(data, "start_time")
            end = iso_time(data, "end_time")
            if parse_time(end) <= parse_time(start):
                raise ValidationError("结束时间必须大于开始时间")
            updates["start_time"] = start
            updates["end_time"] = end
            if notice["status"] == "approved":
                updates["voided_receipt_no"] = notice.get("receipt_no", "")
                updates["receipt_no"] = ""
                updates["revision"] = int(notice.get("revision", 1)) + 1
            summary = "作业期顺延，原回执失效"
        else:
            summary = "航行通告已撤销"
        return new_status, updates, summary

    def notice_blocking_reason(self, state: str, notice: Optional[Dict[str, Any]]) -> Optional[str]:
        if state != "mobilized":
            return None
        if notice is None:
            return "未登记航行通告，勘察不能开始"
        status = notice["status"]
        if status == "approved":
            return None
        if status == "pending":
            return "航行通告待渔政确认，勘察不能开始"
        if status == "rejected":
            return "航行通告被退回：%s" % notice.get("reject_reason", "")
        return "航行通告已撤销，需重新登记"

    def notice_view(self, state: str, notice: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        view: Dict[str, Any] = {"status": "none", "area": "", "start_time": "", "end_time": "", "contact_no": "", "receipt_no": "", "reject_reason": ""}
        if notice is not None:
            view["id"] = notice["id"]
            view["status"] = notice["status"]
            view["area"] = notice["area"]
            view["start_time"] = notice["start_time"]
            view["end_time"] = notice["end_time"]
            view["contact_no"] = notice["contact_no"]
            view["reject_reason"] = notice.get("reject_reason", "")
            if notice["status"] == "approved":
                view["receipt_no"] = notice.get("receipt_no", "")
        view["blocking_reason"] = self.notice_blocking_reason(state, notice)
        return view

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any], notice: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any], str]:
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
            reason = self.notice_blocking_reason(record["state"], notice)
            if reason:
                raise Conflict(reason)
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
