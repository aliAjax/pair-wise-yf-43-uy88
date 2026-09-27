from datetime import date, datetime

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# ---------------------------------------------------------------------------
# Calibration points
# ---------------------------------------------------------------------------
# A passed calibration registers one row per calibrated test point:
#   standard_value / indicated_value / expanded_uncertainty / due_at
# Points are activated on the instrument when the calibration is approved and
# are never deleted, so repeated calibrations keep the old points.
POINT_FIELDS = ("standard_value", "indicated_value", "expanded_uncertainty", "due_at")
CALIBRATION_VALID_DAYS = 365
ROUND_DIGITS = 9


def calibration_current(due_at, as_of):
    return str(due_at) >= str(as_of)


def _as_float(value, field):
    if isinstance(value, bool):
        raise ValidationError(field + " must be a number")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be a number")


def _as_date(value, field):
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        raise ValidationError(field + " must be an ISO date (YYYY-MM-DD)")


def _normalize_points(data):
    raw_points = data.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        raise ValidationError("passed calibration requires at least one point")
    points = []
    seen_indications = set()
    for index, point in enumerate(raw_points):
        label = "points[%d]." % index
        if not isinstance(point, dict):
            raise ValidationError("points[%d] must be an object" % index)
        missing = [field for field in POINT_FIELDS if point.get(field) in (None, "", [], {})]
        if missing:
            raise ValidationError("missing required field: " + ", ".join(label + f for f in missing))
        standard = _as_float(point["standard_value"], label + "standard_value")
        indication = _as_float(point["indicated_value"], label + "indicated_value")
        uncertainty = _as_float(
            point["expanded_uncertainty"], label + "expanded_uncertainty"
        )
        if uncertainty < 0:
            raise ValidationError(label + "expanded_uncertainty must be non-negative")
        due = _as_date(point["due_at"], label + "due_at")
        indication_key = round(indication, ROUND_DIGITS)
        if indication_key in seen_indications:
            raise ValidationError("calibration points must have distinct indicated_value entries")
        seen_indications.add(indication_key)
        points.append(
            {
                "standard_value": standard,
                "indicated_value": indication,
                "expanded_uncertainty": uncertainty,
                "due_at": due.isoformat(),
            }
        )
    points.sort(key=lambda item: item["indicated_value"])
    return points


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_perform(actor, entity, data, lookup):
    result = data.get("result")
    if result not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if result != "passed":
        return
    # The calibration is accepted as a whole; each calibrated point is still
    # registered individually with its own standard value, indication,
    # expanded uncertainty and due date.
    points = _normalize_points(data)
    due_at = min(point["due_at"] for point in points)
    return {"points": points, "due_at": due_at}


def _validate_approve(actor, entity, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", entity["data"].get("instrument_id"))
    if not instrument:
        raise ValidationError("calibration instrument no longer exists")
    points = entity["data"].get("points")
    if not points:
        raise ValidationError("calibration has no registered points")
    effect = {
        "type": "register_calibration_points",
        "instrument_id": instrument["id"],
        "calibration_id": entity["id"],
        "points": points,
    }
    return {"_effects": [effect]}


# ---------------------------------------------------------------------------
# Result release: release only inside the calibrated coverage interval
# ---------------------------------------------------------------------------
def _enclosing_pair(sorted_points, value):
    """Return the two adjacent points whose indications enclose the value."""
    for current, following in zip(sorted_points, sorted_points[1:]):
        if current["indicated_value"] <= value <= following["indicated_value"]:
            return current, following
    return None


def _effective_points(points, as_of):
    """De-duplicate by indicated_value, preferring current then newest points."""
    chosen = {}
    # Current points rank before expired ones; for the same indication the
    # newest registration (repeated submission) is used.
    ordered = sorted(
        points,
        key=lambda item: (
            calibration_current(item["due_at"], as_of),
            str(item.get("registered_at") or ""),
        ),
        reverse=True,
    )
    for point in ordered:
        chosen.setdefault(round(point["indicated_value"], ROUND_DIGITS), point)
    result = [chosen[key] for key in sorted(chosen)]
    for point in result:
        point["current"] = calibration_current(point["due_at"], as_of)
    return result


def _release_reason(points, value, as_of):
    if not points:
        return "no calibrated points registered for this instrument"
    effective = _effective_points(points, as_of)
    current_points = [point for point in effective if point["current"]]
    if len(current_points) < 2:
        return "insufficient calibrated points: %d point registered, at least 2 are required" % len(
            current_points
        )
    pair = _enclosing_pair(current_points, value)
    if pair is None:
        return (
            "measured value %s is outside the calibrated coverage interval [%s, %s]"
            % (value, current_points[0]["indicated_value"], current_points[-1]["indicated_value"])
        )
    return None


def _interpolate(pair, value):
    low, high = pair
    if low is high or low["indicated_value"] == high["indicated_value"]:
        correction = round(low["standard_value"] - low["indicated_value"], ROUND_DIGITS)
        uncertainty = low["expanded_uncertainty"]
    else:
        span = high["indicated_value"] - low["indicated_value"]
        ratio = (value - low["indicated_value"]) / span
        standard = low["standard_value"] + ratio * (high["standard_value"] - low["standard_value"])
        correction = round(standard - value, ROUND_DIGITS)
        uncertainty = max(low["expanded_uncertainty"], high["expanded_uncertainty"])
    corrected = round(value + correction, ROUND_DIGITS)
    return correction, corrected, uncertainty


def _validate_result_release(actor, entity, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not instrument or instrument["status"] != "active":
        raise ValidationError("result requires an active instrument")
    if not method or method["status"] != "validated":
        raise ValidationError("result requires a validated method")
    if data.get("instrument_id") not in method["data"].get("instrument_ids", []):
        raise ValidationError("method is not validated for this instrument")
    value = _as_float(data.get("value"), "value")
    measured_at = data.get("measured_at") or date.today().isoformat()
    as_of = _as_date(measured_at, "measured_at").isoformat()

    all_points = instrument["data"].get("calibration_points", [])
    effective = _effective_points([dict(point) for point in all_points], as_of)
    current_points = [point for point in effective if point["current"]]
    pair = _enclosing_pair(current_points, value)

    if pair is None:
        # Distinguish "expired" from "insufficient / outside coverage": a
        # bracketing pair that exists without the expiry filter means the
        # points were valid once but are expired at measurement time.
        unfiltered_pair = _enclosing_pair(effective, value)
        expired_pair = (
            unfiltered_pair
            and not calibration_current(unfiltered_pair[0]["due_at"], as_of)
            and not calibration_current(unfiltered_pair[1]["due_at"], as_of)
        )
        if expired_pair:
            reason = (
                "calibration expired for measured value %s: points valid until %s/%s"
                % (
                    value,
                    unfiltered_pair[0]["due_at"],
                    unfiltered_pair[1]["due_at"],
                )
            )
        else:
            reason = _release_reason(all_points, value, as_of)
        return {
            "_deferred": {
                "reason": reason,
                "release_checked_at": as_of,
                "release_measured_value": value,
            }
        }

    correction, corrected, uncertainty = _interpolate(pair, value)
    return {
        "released_by": actor.user_id,
        "release_checked_at": as_of,
        "raw_value": value,
        "correction": correction,
        "corrected_value": corrected,
        "expanded_uncertainty": uncertainty,
        "calibration_points": [
            {
                "calibration_id": point.get("calibration_id"),
                "standard_value": point["standard_value"],
                "indicated_value": point["indicated_value"],
                "expanded_uncertainty": point["expanded_uncertainty"],
                "due_at": point["due_at"],
            }
            for point in pair
        ],
    }


CUSTOM_CREATE = {'calibration': _validate_calibration}
CUSTOM_TRANSITIONS = {
    ('calibration', 'perform'): _validate_perform,
    ('calibration', 'approve'): _validate_approve,
    ('result', 'release'): _validate_result_release,
}


class RuleEngine:
    ALIASES = {'instruments': 'instrument', 'calibrations': 'calibration', 'methods': 'method', 'results': 'result'}
    INITIAL_STATUS = {'instrument': 'active', 'calibration': 'requested', 'method': 'draft', 'result': 'pending'}
    TRANSITIONS = {'instrument': {'send_calibration': (('active',), 'calibrating'), 'calibrate': (('calibrating',), 'active'), 'quarantine': (('active',), 'quarantined'), 'restore': (('quarantined',), 'active')}, 'calibration': {'perform': (('requested', 'failed'), 'passed'), 'approve': (('passed',), 'approved'), 'reject': (('failed',), 'rejected')}, 'method': {'validate_method': (('draft',), 'validated'), 'revoke_method': (('validated',), 'revoked')}, 'result': {'release': (('pending',), 'released'), 'block': (('pending',), 'blocked'), 'reanalyze': (('blocked',), 'pending')}}
    CREATE_REQUIRED = {'instrument': ('name', 'serial'), 'calibration': ('instrument_id', 'requested_at'), 'method': ('name', 'version'), 'result': ('sample_id', 'measurement')}
    ACTION_REQUIRED = {('instrument', 'calibrate'): ('due_at', 'passed'), ('instrument', 'quarantine'): ('reason',), ('calibration', 'perform'): ('result', 'performed_at'), ('calibration', 'approve'): ('authorized_by',), ('calibration', 'reject'): ('reason',), ('method', 'validate_method'): ('parameters', 'instrument_ids'), ('method', 'revoke_method'): ('reason',), ('result', 'release'): ('instrument_id', 'method_id', 'value', 'unit'), ('result', 'block'): ('reason',), ('result', 'reanalyze'): ('reason',)}
    CREATE_ROLES = {'instrument': ('admin', 'technician'), 'calibration': ('admin', 'metrology'), 'method': ('admin', 'authorizer'), 'result': ('admin', 'analyst')}
    ROLE_ACTIONS = {'send_calibration': ('admin', 'technician'), 'calibrate': ('admin', 'metrology'), 'quarantine': ('admin', 'metrology'), 'restore': ('admin', 'metrology'), 'perform': ('admin', 'metrology'), 'approve': ('admin', 'authorizer'), 'reject': ('admin', 'authorizer'), 'validate_method': ('admin', 'authorizer'), 'revoke_method': ('admin', 'authorizer'), 'release': ('admin', 'analyst'), 'block': ('admin', 'analyst'), 'reanalyze': ('admin', 'analyst')}

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        extra = extra or {}
        effects = extra.pop("_effects", None)
        deferred = extra.pop("_deferred", None)
        patch = dict(data)
        patch.update(extra)
        if deferred is not None:
            next_status = entity["status"]
            patch.update(deferred)
        return next_status, patch, {"effects": effects, "deferred": deferred}


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
