import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}


def iso(dt):
    return dt.isoformat()


def window(start_offset_hours=1, end_days=3):
    start = datetime.now(timezone.utc) + timedelta(hours=start_offset_hours)
    end = start + timedelta(days=end_days)
    return iso(start), iso(end)


def mobilized(service, reference="CABLE-30001", segment=None):
    data = dict(CREATE_DATA)
    data["segment"] = segment if segment is not None else "S-%s" % reference
    record = service.create(Actor("creator", "noc_operator"), reference, data)
    record = service.act(Actor("rm", "repair_manager"), record["id"], record["version"], "approve", {"repair_manager": "RM-2"})
    record = service.act(Actor("vm", "vessel_master"), record["id"], record["version"], "mobilize", {"weather_window_hours": 40, "available_spare_km": 18, "vessel_name": "CS-1"})
    return record


class NavigationNoticeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_survey_blocked_before_fishery_approval(self):
        record = mobilized(self.service)
        detail = self.service.get_record(Actor("u", "dispatcher"), record["id"])
        self.assertTrue(detail["navigational_notice"]["blocked"])
        self.assertIsNone(detail["navigational_notice"]["valid_receipt"])
        with self.assertRaises(Conflict):
            self.service.act(Actor("ce", "cable_engineer"), record["id"], record["version"], "survey", {"survey_complete": True, "fault_location_km": 128})
        start_at, end_at = window()
        notice = self.service.register_notice(Actor("d", "dispatcher"), record["id"], {"sea_area": "A3", "contact_no": "NT-1", "start_at": start_at, "end_at": end_at})
        # 待确认期间仍然阻塞
        with self.assertRaises(Conflict):
            self.service.act(Actor("ce", "cable_engineer"), record["id"], record["version"], "survey", {"survey_complete": True, "fault_location_km": 128})
        notice = self.service.notice_action(Actor("f", "fisheries_officer"), notice["id"], notice["version"], "approve_notice", {"receipt_no": "RZ-1"})
        record = self.service.get_record(Actor("u", "dispatcher"), record["id"])
        self.assertEqual(record["navigational_notice"]["valid_receipt"]["receipt_no"], "RZ-1")
        self.assertFalse(record["navigational_notice"]["blocked"])

    def test_overlapping_notice_same_sea_area_rejected(self):
        r1 = mobilized(self.service, "CABLE-1")
        r2 = mobilized(self.service, "CABLE-2")
        r3 = mobilized(self.service, "CABLE-3")
        start_at, end_at = window()
        self.service.register_notice(Actor("d", "dispatcher"), r1["id"], {"sea_area": "A3", "contact_no": "NT-1", "start_at": start_at, "end_at": end_at})
        # 同海域时段重叠（另一抢修单）只能有一份
        with self.assertRaises(Conflict):
            self.service.register_notice(Actor("d", "dispatcher"), r2["id"], {"sea_area": "A3", "contact_no": "NT-2", "start_at": start_at, "end_at": end_at})
        # 不同海域允许
        other = self.service.register_notice(Actor("d", "dispatcher"), r2["id"], {"sea_area": "B7", "contact_no": "NT-2", "start_at": start_at, "end_at": end_at})
        self.assertEqual(other["state"], "pending")
        # 同海域不重叠时段允许（第三张抢修单）
        later_start = end_at
        later_end = iso(datetime.fromisoformat(end_at) + timedelta(days=1))
        third = self.service.register_notice(Actor("d", "dispatcher"), r3["id"], {"sea_area": "A3", "contact_no": "NT-3", "start_at": later_start, "end_at": later_end})
        self.assertEqual(third["state"], "pending")

    def test_rejected_keeps_reason_and_resubmit_with_new_contact(self):
        record = mobilized(self.service)
        start_at, end_at = window()
        notice = self.service.register_notice(Actor("d", "dispatcher"), record["id"], {"sea_area": "A3", "contact_no": "NT-1", "start_at": start_at, "end_at": end_at})
        notice = self.service.notice_action(Actor("f", "fisheries_officer"), notice["id"], notice["version"], "reject_notice", {"reject_reason": "休渔期禁止作业"})
        self.assertEqual(notice["state"], "rejected")
        history = self.service.list_notices(Actor("u", "dispatcher"), record["id"])
        self.assertEqual(history[0]["reject_reason"], "休渔期禁止作业")
        detail = self.service.get_record(Actor("u", "dispatcher"), record["id"])
        self.assertIn("休渔期禁止作业", detail["navigational_notice"]["block_reason"])
        # 退回释放占用，换新单重提
        notice2 = self.service.register_notice(Actor("d", "dispatcher"), record["id"], {"sea_area": "A3", "contact_no": "NT-2", "start_at": start_at, "end_at": end_at})
        self.assertEqual(notice2["state"], "pending")
        self.assertEqual(notice2["contact_no"], "NT-2")

    def test_reschedule_invalidates_old_receipt(self):
        record = mobilized(self.service)
        start_at, end_at = window()
        notice = self.service.register_notice(Actor("d", "dispatcher"), record["id"], {"sea_area": "A3", "contact_no": "NT-1", "start_at": start_at, "end_at": end_at})
        notice = self.service.notice_action(Actor("f", "fisheries_officer"), notice["id"], notice["version"], "approve_notice", {"receipt_no": "RZ-1"})
        # 顺延后原回执失效，需按新时段确认
        new_end = iso(datetime.fromisoformat(end_at) + timedelta(days=2))
        successor = self.service.reschedule_notice(Actor("d", "dispatcher"), notice["id"], notice["version"], {"end_at": new_end})
        self.assertEqual(successor["state"], "pending")
        self.assertEqual(successor["supersedes_id"], notice["id"])
        detail = self.service.get_record(Actor("u", "dispatcher"), record["id"])
        self.assertIsNone(detail["navigational_notice"]["valid_receipt"])
        self.assertIn("顺延", detail["navigational_notice"]["block_reason"])
        history = self.service.list_notices(Actor("u", "dispatcher"), record["id"])
        self.assertEqual({n["state"] for n in history}, {"superseded", "pending"})
        # 新时段再次经渔政同意
        successor = self.service.notice_action(Actor("f", "fisheries_officer"), successor["id"], successor["version"], "approve_notice", {"receipt_no": "RZ-2"})
        detail = self.service.get_record(Actor("u", "dispatcher"), record["id"])
        self.assertEqual(detail["navigational_notice"]["valid_receipt"]["receipt_no"], "RZ-2")

    def test_revoke_releases_occupation(self):
        r1 = mobilized(self.service, "CABLE-1")
        r2 = mobilized(self.service, "CABLE-2")
        start_at, end_at = window()
        notice = self.service.register_notice(Actor("d", "dispatcher"), r1["id"], {"sea_area": "A3", "contact_no": "NT-1", "start_at": start_at, "end_at": end_at})
        notice = self.service.notice_action(Actor("f", "fisheries_officer"), notice["id"], notice["version"], "approve_notice", {"receipt_no": "RZ-1"})
        with self.assertRaises(Conflict):
            self.service.register_notice(Actor("d", "dispatcher"), r2["id"], {"sea_area": "A3", "contact_no": "NT-2", "start_at": start_at, "end_at": end_at})
        notice = self.service.notice_action(Actor("f", "fisheries_officer"), notice["id"], notice["version"], "revoke_notice", {"revoke_reason": "气象原因取消"})
        # 撤销后占用释放
        new_notice = self.service.register_notice(Actor("d", "dispatcher"), r2["id"], {"sea_area": "A3", "contact_no": "NT-2", "start_at": start_at, "end_at": end_at})
        self.assertEqual(new_notice["state"], "pending")

    def test_permissions_and_gates(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-X", CREATE_DATA)
        start_at, end_at = window()
        # 动员前不能登记
        with self.assertRaises(Conflict):
            self.service.register_notice(Actor("d", "dispatcher"), record["id"], {"sea_area": "A3", "contact_no": "NT-1", "start_at": start_at, "end_at": end_at})
        record = self.service.act(Actor("rm", "repair_manager"), record["id"], record["version"], "approve", {"repair_manager": "RM-2"})
        record = self.service.act(Actor("vm", "vessel_master"), record["id"], record["version"], "mobilize", {"weather_window_hours": 40, "available_spare_km": 18, "vessel_name": "CS-1"})
        # 船长不能登记通告
        with self.assertRaises(PermissionDenied):
            self.service.register_notice(Actor("vm", "vessel_master"), record["id"], {"sea_area": "A3", "contact_no": "NT-1", "start_at": start_at, "end_at": end_at})
        notice = self.service.register_notice(Actor("d", "dispatcher"), record["id"], {"sea_area": "A3", "contact_no": "NT-1", "start_at": start_at, "end_at": end_at})
        # 调度员不能审批
        with self.assertRaises(PermissionDenied):
            self.service.notice_action(Actor("d", "dispatcher"), notice["id"], notice["version"], "approve_notice", {"receipt_no": "RZ-1"})
        # 缺回执号
        with self.assertRaises(ValidationError):
            self.service.notice_action(Actor("f", "fisheries_officer"), notice["id"], notice["version"], "approve_notice", {})
        # 重复登记被拒
        with self.assertRaises(Conflict):
            self.service.register_notice(Actor("d", "dispatcher"), record["id"], {"sea_area": "A3", "contact_no": "NT-9", "start_at": start_at, "end_at": end_at})

    def test_list_carries_notice_status(self):
        record = mobilized(self.service)
        items = self.service.list_records(Actor("u", "dispatcher"))
        self.assertIn("navigational_notice", items[0])
        self.assertTrue(items[0]["navigational_notice"]["blocked"])

    def test_expired_approval_is_blocking(self):
        record = mobilized(self.service)
        past_start = iso(datetime.now(timezone.utc) - timedelta(days=2))
        past_end = iso(datetime.now(timezone.utc) - timedelta(days=1))
        notice = self.service.register_notice(Actor("d", "dispatcher"), record["id"], {"sea_area": "A3", "contact_no": "NT-1", "start_at": past_start, "end_at": past_end})
        self.service.notice_action(Actor("f", "fisheries_officer"), notice["id"], notice["version"], "approve_notice", {"receipt_no": "RZ-OLD"})
        detail = self.service.get_record(Actor("u", "dispatcher"), record["id"])
        self.assertIsNone(detail["navigational_notice"]["valid_receipt"])
        self.assertTrue(detail["navigational_notice"]["blocked"])
