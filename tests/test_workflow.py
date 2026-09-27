import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


CREATE_DATA = {'cable': 'SEA-1', 'segment': 'S3', 'start_km': 120.0, 'end_km': 135.0, 'depth_m': 1800.0, 'sea_state': 3, 'vessel_available': True, 'spare_length_km': 20.0, 'permit_valid': True, 'capacity_gbps': 400}
FLOW = [('approve', 'repair_manager', {'repair_manager': 'RM-2'}, 'approved'), ('mobilize', 'vessel_master', {'weather_window_hours': 40, 'available_spare_km': 18, 'vessel_name': 'CS-1'}, 'mobilized'), ('survey', 'cable_engineer', {'survey_complete': True, 'fault_location_km': 128}, 'surveyed'), ('splice', 'cable_engineer', {'splice_loss_db': 0.12, 'spare_used_km': 16}, 'spliced'), ('test', 'noc_operator', {'end_to_end_loss_db': 0.3}, 'tested'), ('restore', 'noc_operator', {'traffic_restored': True, 'restore_capacity_gbps': 400}, 'restored')]


def notice_window(days=3):
    start = datetime.now(timezone.utc) + timedelta(hours=1)
    end = start + timedelta(days=days)
    return start.isoformat(), end.isoformat()


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_workflow_and_audit(self):
        record = self.service.create(Actor("creator", "noc_operator"), "CABLE-30001", CREATE_DATA)
        self.assertEqual(record["state"], "detected")
        audit_count = 1
        for action, role, data, expected_state in FLOW:
            if action == "survey":
                start_at, end_at = notice_window()
                notice = self.service.register_notice(
                    Actor("disp1", "dispatcher"), record["id"],
                    {"sea_area": "A3海区", "contact_no": "NT-001", "start_at": start_at, "end_at": end_at},
                )
                audit_count += 1
                self.assertEqual(notice["state"], "pending")
                notice = self.service.notice_action(
                    Actor("fish1", "fisheries_officer"), notice["id"], notice["version"],
                    "approve_notice", {"receipt_no": "RZ-9001"},
                )
                audit_count += 1
                self.assertEqual(notice["state"], "approved")
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
            audit_count += 1
            self.assertEqual(record["state"], expected_state)
        record = self.service.get_record(Actor("creator", "noc_operator"), record["id"])
        self.assertTrue(record["navigational_notice"]["valid_receipt"])
        timeline = self.service.timeline(Actor("creator", "noc_operator"), record["id"])
        self.assertEqual(len(timeline), audit_count)
        self.assertEqual(timeline[-1]["action"], FLOW[-1][0])
