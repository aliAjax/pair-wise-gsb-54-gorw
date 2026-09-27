import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
NOTICE_DATA = {'area': 'EAST-SEA-7', 'start_time': '2026-10-01T00:00:00Z', 'end_time': '2026-10-05T00:00:00Z', 'contact_no': 'LX-1001'}
DISPATCHER = Actor("dispatcher-1", "dispatcher")
FISHERY = Actor("fishery-1", "fishery_admin")
ENGINEER = Actor("engineer-1", "cable_engineer")
SURVEY_DATA = {'survey_complete': True, 'fault_location_km': 128}


class NoticeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _mobilized(self, reference="CABLE-40001", cable="SEA-1", segment="S3"):
        data = dict(CREATE_DATA)
        data.update({'cable': cable, 'segment': segment})
        record = self.service.create(Actor("creator", "noc_operator"), reference, data)
        record = self.service.act(Actor("rm", "repair_manager"), record["id"], record["version"], "approve", {'repair_manager': 'RM-2'})
        record = self.service.act(Actor("vm", "vessel_master"), record["id"], record["version"], "mobilize", {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'})
        return record

    def test_survey_blocked_until_fishery_confirms(self):
        record = self._mobilized()
        with self.assertRaises(Conflict) as caught:
            self.service.act(ENGINEER, record["id"], record["version"], "survey", SURVEY_DATA)
        self.assertIn("未登记航行通告", str(caught.exception))
        detail = self.service.get_record(Actor("creator", "noc_operator"), record["id"])
        self.assertEqual(detail["notice"]["receipt_no"], "")
        self.assertIn("未登记", detail["notice"]["blocking_reason"])

        notice = self.service.register_notice(DISPATCHER, record["id"], NOTICE_DATA)
        self.assertEqual(notice["status"], "pending")
        with self.assertRaises(Conflict):
            self.service.act(ENGINEER, record["id"], record["version"], "survey", SURVEY_DATA)
        detail = self.service.get_record(Actor("creator", "noc_operator"), record["id"])
        self.assertIn("待渔政确认", detail["notice"]["blocking_reason"])

        notice = self.service.notice_action(FISHERY, record["id"], notice["id"], "confirm", {})
        self.assertEqual(notice["status"], "approved")
        self.assertTrue(notice["receipt_no"])
        record = self.service.act(ENGINEER, record["id"], record["version"], "survey", SURVEY_DATA)
        self.assertEqual(record["state"], "surveyed")
        detail = self.service.get_record(Actor("creator", "noc_operator"), record["id"])
        self.assertEqual(detail["notice"]["receipt_no"], notice["receipt_no"])
        self.assertIsNone(detail["notice"]["blocking_reason"])

    def test_only_one_notice_for_overlapping_area_window(self):
        first = self._mobilized("CABLE-40001")
        second = self._mobilized("CABLE-40002", "SEA-2", "S9")
        notice = self.service.register_notice(DISPATCHER, first["id"], NOTICE_DATA)

        overlapping = {'area': 'EAST-SEA-7', 'start_time': '2026-10-04T00:00:00Z', 'end_time': '2026-10-08T00:00:00Z', 'contact_no': 'LX-2002'}
        with self.assertRaises(Conflict) as caught:
            self.service.register_notice(DISPATCHER, second["id"], overlapping)
        self.assertIn("同一海域时段重叠", str(caught.exception))

        touching = dict(overlapping, start_time='2026-10-05T00:00:00Z')
        other_area = dict(overlapping, area='WEST-SEA-3')
        self.service.register_notice(DISPATCHER, second["id"], touching)
        third = self._mobilized("CABLE-40003", "SEA-3", "S1")
        self.service.register_notice(DISPATCHER, third["id"], other_area)

        # 退回的通告不占用，原窗口可重新登记
        self.service.notice_action(FISHERY, first["id"], notice["id"], "reject", {'reject_reason': '时间冲突'})
        fourth = self._mobilized("CABLE-40004", "SEA-4", "S7")
        self.service.register_notice(DISPATCHER, fourth["id"], NOTICE_DATA)

    def test_reject_retains_reason_and_resubmit_needs_new_sheet(self):
        record = self._mobilized()
        notice = self.service.register_notice(DISPATCHER, record["id"], NOTICE_DATA)
        with self.assertRaises(ValidationError):
            self.service.notice_action(FISHERY, record["id"], notice["id"], "reject", {})
        notice = self.service.notice_action(FISHERY, record["id"], notice["id"], "reject", {'reject_reason': '联系单信息有误'})
        self.assertEqual(notice["status"], "rejected")
        self.assertEqual(notice["reject_reason"], "联系单信息有误")

        detail = self.service.get_record(Actor("creator", "noc_operator"), record["id"])
        self.assertIn("联系单信息有误", detail["notice"]["blocking_reason"])

        same_sheet = dict(NOTICE_DATA)
        with self.assertRaises(ValidationError) as caught:
            self.service.notice_action(DISPATCHER, record["id"], notice["id"], "resubmit", same_sheet)
        self.assertIn("更换联系单号", str(caught.exception))

        new_sheet = dict(NOTICE_DATA, contact_no="LX-1001-R2")
        new_notice = self.service.notice_action(DISPATCHER, record["id"], notice["id"], "resubmit", new_sheet)
        self.assertEqual(new_notice["status"], "pending")
        self.assertEqual(new_notice["contact_no"], "LX-1001-R2")
        history = self.service.list_notices(Actor("creator", "noc_operator"), record["id"])
        self.assertEqual([item["status"] for item in history], ["pending", "rejected"])
        self.assertEqual(history[1]["reject_reason"], "联系单信息有误")

        with self.assertRaises(Conflict):
            self.service.act(ENGINEER, record["id"], record["version"], "survey", SURVEY_DATA)
        self.service.notice_action(FISHERY, record["id"], new_notice["id"], "confirm", {})
        self.service.act(ENGINEER, record["id"], record["version"], "survey", SURVEY_DATA)

    def test_extend_voids_receipt_and_requires_reconfirm(self):
        record = self._mobilized()
        other = self._mobilized("CABLE-40002", "SEA-2", "S9")
        notice = self.service.register_notice(DISPATCHER, record["id"], NOTICE_DATA)
        notice = self.service.notice_action(FISHERY, record["id"], notice["id"], "confirm", {})
        old_receipt = notice["receipt_no"]

        self.service.register_notice(DISPATCHER, other["id"], {'area': 'EAST-SEA-7', 'start_time': '2026-10-06T00:00:00Z', 'end_time': '2026-10-08T00:00:00Z', 'contact_no': 'LX-2002'})
        with self.assertRaises(Conflict) as caught:
            self.service.notice_action(DISPATCHER, record["id"], notice["id"], "extend", {'start_time': '2026-10-01T00:00:00Z', 'end_time': '2026-10-09T00:00:00Z'})
        self.assertIn("同一海域时段重叠", str(caught.exception))

        notice = self.service.notice_action(DISPATCHER, record["id"], notice["id"], "extend", {'start_time': '2026-10-01T00:00:00Z', 'end_time': '2026-10-06T00:00:00Z'})
        self.assertEqual(notice["status"], "pending")
        self.assertEqual(notice["receipt_no"], "")
        self.assertEqual(notice["voided_receipt_no"], old_receipt)

        detail = self.service.get_record(Actor("creator", "noc_operator"), record["id"])
        self.assertEqual(detail["notice"]["receipt_no"], "")
        self.assertIn("待渔政确认", detail["notice"]["blocking_reason"])
        with self.assertRaises(Conflict):
            self.service.act(ENGINEER, record["id"], record["version"], "survey", SURVEY_DATA)

        notice = self.service.notice_action(FISHERY, record["id"], notice["id"], "confirm", {})
        self.assertTrue(notice["receipt_no"])
        self.assertNotEqual(notice["receipt_no"], old_receipt)
        record = self.service.act(ENGINEER, record["id"], record["version"], "survey", SURVEY_DATA)
        self.assertEqual(record["state"], "surveyed")

    def test_revoke_releases_occupancy(self):
        first = self._mobilized("CABLE-40001")
        second = self._mobilized("CABLE-40002", "SEA-2", "S9")
        notice = self.service.register_notice(DISPATCHER, first["id"], NOTICE_DATA)
        self.service.notice_action(FISHERY, first["id"], notice["id"], "confirm", {})
        with self.assertRaises(Conflict):
            self.service.register_notice(DISPATCHER, second["id"], dict(NOTICE_DATA, contact_no='LX-2002'))
        self.service.notice_action(DISPATCHER, first["id"], notice["id"], "revoke", {})
        self.service.register_notice(DISPATCHER, second["id"], dict(NOTICE_DATA, contact_no='LX-2002'))

    def test_register_requires_mobilized_and_dispatcher(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-40001", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.register_notice(DISPATCHER, record["id"], NOTICE_DATA)
        with self.assertRaises(PermissionDenied):
            self.service.register_notice(ENGINEER, record["id"], NOTICE_DATA)
        with self.assertRaises(PermissionDenied):
            self.service.notice_action(ENGINEER, record["id"], 1, "confirm", {})

    def test_list_shows_receipt_and_blocking_reason(self):
        record = self._mobilized()
        self.service.register_notice(DISPATCHER, record["id"], NOTICE_DATA)
        items = self.service.list_records(Actor("creator", "noc_operator"))
        self.assertEqual(items[0]["notice"]["contact_no"], "LX-1001")
        self.assertIn("待渔政确认", items[0]["notice"]["blocking_reason"])
