import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


GOOD_POINTS = [
    {"standard": 0, "indicated": 0.0, "uncertainty": 0.01, "due_at": "2099-01-01"},
    {"standard": 10, "indicated": 10.1, "uncertainty": 0.01, "due_at": "2099-01-01"},
]


class ReleaseCorrectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.metrology = Actor("met-1", "metrology")

    def tearDown(self):
        self.tmp.cleanup()

    def _calibrate_instrument(self, points=None, due_at="2099-01-01"):
        instrument = self.service.create(
            self.admin, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )
        self.service.transition(self.admin, instrument["id"], "send_calibration", {})
        self.service.transition(
            self.admin,
            instrument["id"],
            "calibrate",
            {"due_at": due_at, "passed": True},
        )
        calibration = self.service.create(
            self.admin,
            "calibration",
            {"instrument_id": instrument["id"], "requested_at": "2026-01-01"},
        )
        self.service.transition(
            self.admin,
            calibration["id"],
            "perform",
            {
                "result": "passed",
                "performed_at": "2026-01-02",
                "uncertainty": 0.01,
                "due_at": due_at,
            },
        )
        self.service.transition(
            self.admin, calibration["id"], "approve", {"authorized_by": "QA-1"}
        )
        for point in points or []:
            self.service.transition(
                self.admin, calibration["id"], "register_point", {"point": point}
            )
        return instrument, calibration

    def _validate_method(self, instrument):
        method = self.service.create(
            self.admin, "method", {"name": "Assay-A", "version": "v1"}
        )
        self.service.transition(
            self.admin,
            method["id"],
            "validate_method",
            {"parameters": {"range": [0, 100]}, "instrument_ids": [instrument["id"]]},
        )
        return method

    def _release(self, result, instrument, method, value):
        return self.service.transition(
            self.admin,
            result["id"],
            "release",
            {
                "instrument_id": instrument["id"],
                "method_id": method["id"],
                "value": value,
                "unit": "mg/L",
            },
        )

    def _new_result(self):
        return self.service.create(
            self.admin, "result", {"sample_id": "S-1", "measurement": "initial"}
        )

    def test_register_point_appends_and_keeps_status(self):
        _, calibration = self._calibrate_instrument(points=[])
        updated = self.service.transition(
            self.metrology,
            calibration["id"],
            "register_point",
            {"point": GOOD_POINTS[0]},
        )
        self.assertEqual(updated["status"], "approved")
        self.assertEqual(len(updated["data"]["points"]), 1)
        point = updated["data"]["points"][0]
        self.assertEqual(point["standard"], 0.0)
        self.assertEqual(point["indicated"], 0.0)
        self.assertEqual(point["uncertainty"], 0.01)
        self.assertEqual(point["due_at"], "2099-01-01")
        self.assertNotIn("point", updated["data"])
        updated = self.service.transition(
            self.metrology,
            calibration["id"],
            "register_point",
            {"point": GOOD_POINTS[1]},
        )
        self.assertEqual(updated["status"], "approved")
        self.assertEqual(len(updated["data"]["points"]), 2)

    def test_register_point_validation(self):
        instrument, calibration = self._calibrate_instrument(points=[])
        pending = self.service.create(
            self.admin,
            "calibration",
            {"instrument_id": instrument["id"], "requested_at": "2026-02-01"},
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin, pending["id"], "register_point", {"point": GOOD_POINTS[0]}
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("analyst-1", "analyst"),
                calibration["id"],
                "register_point",
                {"point": GOOD_POINTS[0]},
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                calibration["id"],
                "register_point",
                {"point": {"standard": "x", "indicated": 0, "uncertainty": 0.01, "due_at": "2099-01-01"}},
            )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.admin,
                calibration["id"],
                "register_point",
                {"point": {"standard": 0, "indicated": 0, "uncertainty": 0.01}},
            )

    def test_release_applies_interpolated_correction(self):
        instrument, calibration = self._calibrate_instrument(points=GOOD_POINTS)
        method = self._validate_method(instrument)
        result = self._new_result()
        released = self._release(result, instrument, method, 4.2)
        self.assertEqual(released["status"], "released")
        data = released["data"]
        self.assertEqual(data["release_decision"], "released")
        self.assertIsNone(data["hold_reason"])
        self.assertEqual(data["original_value"], 4.2)
        self.assertAlmostEqual(data["correction"], -0.042, places=6)
        self.assertAlmostEqual(data["corrected_value"], 4.158, places=6)
        adopted = data["calibration_points"]
        self.assertEqual(len(adopted), 2)
        self.assertEqual(adopted[0]["standard"], 0.0)
        self.assertEqual(adopted[1]["standard"], 10.0)
        self.assertEqual(adopted[0]["calibration_id"], calibration["id"])

    def test_release_held_without_enough_points(self):
        instrument, _ = self._calibrate_instrument(points=[])
        method = self._validate_method(instrument)
        held = self._release(self._new_result(), instrument, method, 4.2)
        self.assertEqual(held["status"], "pending")
        self.assertEqual(held["data"]["release_decision"], "held")
        self.assertIn("校准点不足", held["data"]["hold_reason"])
        self.assertNotIn("corrected_value", held["data"])

        self.service.transition(
            self.admin,
            self.service.list("calibration", status="approved")[0]["id"],
            "register_point",
            {"point": GOOD_POINTS[0]},
        )
        held = self._release(self._new_result(), instrument, method, 4.2)
        self.assertEqual(held["status"], "pending")
        self.assertIn("校准点不足", held["data"]["hold_reason"])

    def test_release_held_out_of_coverage(self):
        instrument, _ = self._calibrate_instrument(points=GOOD_POINTS)
        method = self._validate_method(instrument)
        held = self._release(self._new_result(), instrument, method, 99)
        self.assertEqual(held["status"], "pending")
        self.assertIn("超出校准覆盖区间", held["data"]["hold_reason"])
        held = self._release(self._new_result(), instrument, method, -1)
        self.assertEqual(held["status"], "pending")
        self.assertIn("超出校准覆盖区间", held["data"]["hold_reason"])

    def test_release_held_when_calibration_expired(self):
        expired = [
            {"standard": 0, "indicated": 0.0, "uncertainty": 0.01, "due_at": "2020-01-01"},
            {"standard": 10, "indicated": 10.1, "uncertainty": 0.01, "due_at": "2020-01-01"},
        ]
        instrument, _ = self._calibrate_instrument(points=expired)
        method = self._validate_method(instrument)
        held = self._release(self._new_result(), instrument, method, 4.2)
        self.assertEqual(held["status"], "pending")
        self.assertIn("校准已过期", held["data"]["hold_reason"])

    def test_recalibration_keeps_old_points(self):
        instrument, first = self._calibrate_instrument(points=GOOD_POINTS)
        self.service.transition(self.admin, instrument["id"], "send_calibration", {})
        self.service.transition(
            self.admin,
            instrument["id"],
            "calibrate",
            {"due_at": "2099-01-01", "passed": True},
        )
        second = self.service.create(
            self.admin,
            "calibration",
            {"instrument_id": instrument["id"], "requested_at": "2026-03-01"},
        )
        self.service.transition(
            self.admin,
            second["id"],
            "perform",
            {
                "result": "passed",
                "performed_at": "2026-03-02",
                "uncertainty": 0.02,
                "due_at": "2099-06-01",
            },
        )
        self.service.transition(
            self.admin, second["id"], "approve", {"authorized_by": "QA-2"}
        )
        for point in (
            {"standard": 20, "indicated": 20.2, "uncertainty": 0.02, "due_at": "2099-06-01"},
            {"standard": 30, "indicated": 30.3, "uncertainty": 0.02, "due_at": "2099-06-01"},
        ):
            self.service.transition(
                self.admin, second["id"], "register_point", {"point": point}
            )

        old = self.service.get(first["id"])
        self.assertEqual(len(old["data"]["points"]), 2)
        method = self._validate_method(instrument)

        released = self._release(self._new_result(), instrument, method, 15)
        self.assertEqual(released["status"], "released")
        adopted = released["data"]["calibration_points"]
        self.assertEqual(adopted[0]["calibration_id"], first["id"])
        self.assertEqual(adopted[1]["calibration_id"], second["id"])
        self.assertAlmostEqual(released["data"]["correction"], -0.15, places=6)
        self.assertAlmostEqual(released["data"]["corrected_value"], 14.85, places=6)

        released = self._release(self._new_result(), instrument, method, 25)
        adopted = released["data"]["calibration_points"]
        self.assertEqual(adopted[0]["calibration_id"], second["id"])
        self.assertAlmostEqual(released["data"]["corrected_value"], 24.75, places=6)

    def test_hold_cleared_after_successful_release(self):
        instrument, calibration = self._calibrate_instrument(points=[])
        method = self._validate_method(instrument)
        result = self._new_result()
        held = self._release(result, instrument, method, 4.2)
        self.assertEqual(held["status"], "pending")
        for point in GOOD_POINTS:
            self.service.transition(
                self.admin, calibration["id"], "register_point", {"point": point}
            )
        released = self._release(result, instrument, method, 4.2)
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["data"]["release_decision"], "released")
        self.assertIsNone(released["data"]["hold_reason"])
        self.assertIn("corrected_value", released["data"])


if __name__ == "__main__":
    unittest.main()
