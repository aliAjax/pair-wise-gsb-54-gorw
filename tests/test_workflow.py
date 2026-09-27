import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
NOTICE_DATA = {'area': 'EAST-SEA-7', 'start_time': '2026-10-01T00:00:00Z', 'end_time': '2026-10-06T00:00:00Z', 'contact_no': 'LX-9001'}
FLOW = [('approve', 'repair_manager', {'repair_manager': 'RM-2'}, 'approved'), ('mobilize', 'vessel_master', {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'}, 'mobilized'), ('survey', 'cable_engineer', {'survey_complete': True, 'fault_location_km': 128}, 'surveyed'), ('splice', 'cable_engineer', {'splice_loss_db': 0.12, 'spare_used_km': 16}, 'spliced'), ('test', 'noc_operator', {'end_to_end_loss_db': 0.3}, 'tested'), ('restore', 'noc_operator', {'traffic_restored': True, 'restore_capacity_gbps': 400}, 'restored')]
DISPATCHER = Actor("dispatcher-1", "dispatcher")
FISHERY = Actor("fishery-1", "fishery_admin")


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_workflow_and_audit(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-30001", CREATE_DATA)
        self.assertEqual(record["state"], "detected")
        for action, role, data, expected_state in FLOW:
            if action == "survey":
                notice = self.service.register_notice(DISPATCHER, record["id"], NOTICE_DATA)
                self.service.notice_action(FISHERY, record["id"], notice["id"], "confirm", {})
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
            self.assertEqual(record["state"], expected_state)
        timeline = self.service.timeline(Actor("creator", "noc_operator"), record["id"])
        self.assertEqual(len(timeline), len(FLOW) + 4)
        self.assertEqual(timeline[-2]["action"], FLOW[-1][0])
        self.assertEqual(timeline[-1]["action"], "notice_revoke")
        notice_actions = [event["action"] for event in timeline if event["action"].startswith("notice_")]
        self.assertEqual(notice_actions, ["notice_register", "notice_confirm", "notice_revoke"])
