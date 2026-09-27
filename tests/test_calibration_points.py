import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


ADMIN = Actor("admin", "admin")
METRO = Actor("metro-1", "metrology")
QA = Actor("qa-1", "authorizer")
ANALYST = Actor("analyst-1", "analyst")


def _point(standard, indication, uncertainty=0.01, due="2099-01-01"):
    return {
        "standard_value": standard,
        "indicated_value": indication,
        "expanded_uncertainty": uncertainty,
        "due_at": due,
    }


class CalibrationPointsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())

    def tearDown(self):
        self.tmp.cleanup()

    def _create_instrument(self):
        return self.service.create(
            ADMIN, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )["id"]

    def _calibrate(self, instrument_id, points, performed="2026-01-02"):
        calibration = self.service.create(
            METRO,
            "calibration",
            {"instrument_id": instrument_id, "requested_at": "2026-01-01"},
        )
        self.service.transition(
            METRO,
            calibration["id"],
            "perform",
            {"result": "passed", "performed_at": performed, "points": points},
        )
        self.service.transition(
            QA, calibration["id"], "approve", {"authorized_by": "QA-1"}
        )
        return calibration["id"]

    def _validated_method(self, instrument_id, range_low=0, range_high=10):
        method = self.service.create(
            QA, "method", {"name": "Assay-A", "version": "v1"}
        )
        self.service.transition(
            QA,
            method["id"],
            "validate_method",
            {
                "parameters": {"range": [range_low, range_high]},
                "instrument_ids": [instrument_id],
            },
        )
        return method["id"]

    def _release(self, instrument_id, method_id, value, **extra):
        result = self.service.create(
            ANALYST, "result", {"sample_id": "S-1", "measurement": "initial"}
        )
        data = {"instrument_id": instrument_id, "method_id": method_id, "value": value, "unit": "mg/L"}
        data.update(extra)
        updated = self.service.transition(ANALYST, result["id"], "release", data)
        return result["id"], updated

    def test_interpolated_correction_between_two_points(self):
        instrument = self._create_instrument()
        self._calibrate(instrument, [_point(0.0, 0.0), _point(10.0, 10.2)])
        method = self._validated_method(instrument)
        _, released = self._release(instrument, method, 5.1)
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["raw_value"], 5.1)
        # indication bias is linear: correction = -0.1 at 5.1
        self.assertAlmostEqual(released["data"]["correction"], -0.1, places=6)
        self.assertAlmostEqual(released["data"]["corrected_value"], 5.0, places=6)
        self.assertEqual(len(released["data"]["calibration_points"]), 2)

    def test_perform_requires_registered_points(self):
        instrument = self._create_instrument()
        calibration = self.service.create(
            METRO,
            "calibration",
            {"instrument_id": instrument, "requested_at": "2026-01-01"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                METRO,
                calibration["id"],
                "perform",
                {"result": "passed", "performed_at": "2026-01-02"},
            )

    def test_duplicate_indicated_value_rejected(self):
        instrument = self._create_instrument()
        calibration = self.service.create(
            METRO,
            "calibration",
            {"instrument_id": instrument, "requested_at": "2026-01-01"},
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                METRO,
                calibration["id"],
                "perform",
                {
                    "result": "passed",
                    "performed_at": "2026-01-02",
                    "points": [_point(0, 0), _point(1, 0)],
                },
            )

    def test_single_point_keeps_result_pending(self):
        instrument = self._create_instrument()
        self._calibrate(instrument, [_point(5.0, 5.0)])
        method = self._validated_method(instrument)
        result_id, result = self._release(instrument, method, 5.0)
        self.assertEqual(result["status"], "pending")
        self.assertIn("insufficient", result["data"]["reason"])
        audit = self.service.audit_log(result_id)[-1]
        self.assertTrue(audit["detail"]["deferred"])

    def test_value_outside_coverage_keeps_result_pending(self):
        instrument = self._create_instrument()
        self._calibrate(instrument, [_point(0.0, 0.0), _point(10.0, 10.0)])
        method = self._validated_method(instrument)
        _, result = self._release(instrument, method, 12.0)
        self.assertEqual(result["status"], "pending")
        self.assertIn("outside the calibrated coverage interval", result["data"]["reason"])

    def test_expired_calibration_keeps_result_pending(self):
        instrument = self._create_instrument()
        self._calibrate(
            instrument,
            [_point(0.0, 0.0, due="2025-01-01"), _point(10.0, 10.0, due="2025-01-01")],
        )
        method = self._validated_method(instrument)
        _, result = self._release(
            instrument, method, 5.0, measured_at="2026-02-01"
        )
        self.assertEqual(result["status"], "pending")
        self.assertIn("calibration expired", result["data"]["reason"])

    def test_repeated_submission_keeps_old_points(self):
        instrument = self._create_instrument()
        first = self._calibrate(
            instrument, [_point(0.0, 0.0), _point(10.0, 10.2)], performed="2025-01-02"
        )
        second = self._calibrate(
            instrument, [_point(0.0, 0.0), _point(5.0, 5.05), _point(10.0, 10.0)],
            performed="2026-01-02",
        )
        points = self.service.get(instrument)["data"]["calibration_points"]
        self.assertEqual(len(points), 5)
        self.assertEqual({point["calibration_id"] for point in points}, {first, second})

        method = self._validated_method(instrument)
        # 5.1 is enclosed by the newest calibration's 5.05/10.0 indications
        _, released = self._release(instrument, method, 5.1)
        self.assertEqual(released["status"], "released")
        used = {point["calibration_id"] for point in released["data"]["calibration_points"]}
        self.assertEqual(used, {second})
        self.assertAlmostEqual(released["data"]["correction"], -0.049495, places=4)

    def test_expired_points_remain_visible_and_new_points_extend_coverage(self):
        instrument = self._create_instrument()
        self._calibrate(
            instrument,
            [_point(0.0, 0.0, due="2025-01-01"), _point(10.0, 10.0, due="2025-01-01")],
            performed="2024-01-02",
        )
        # repeat submission covers only the low range; expired points stay
        self._calibrate(instrument, [_point(0.0, 0.0), _point(5.0, 5.0)])
        points = self.service.get(instrument)["data"]["calibration_points"]
        self.assertEqual(len(points), 4)
        method = self._validated_method(instrument)
        _, result = self._release(
            instrument, method, 8.0, measured_at="2026-02-01"
        )
        self.assertEqual(result["status"], "pending")
        self.assertIn("outside the calibrated coverage interval", result["data"]["reason"])
        _, released = self._release(instrument, method, 2.5)
        self.assertEqual(released["status"], "released")
        self.assertAlmostEqual(released["data"]["correction"], 0.0, places=6)


if __name__ == "__main__":
    unittest.main()
