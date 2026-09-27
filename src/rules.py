from datetime import datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _today():
    return datetime.now().date().isoformat()


def _as_float(value, field):
    if isinstance(value, bool):
        raise ValidationError(field + " must be a number")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError(field + " must be a number")


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_perform(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")


def _validate_register_point(actor, entity, data, lookup):
    point_data = data.get("point")
    if not isinstance(point_data, dict):
        raise ValidationError("point must be an object with standard, indicated, uncertainty, due_at")
    point = {
        "standard": _as_float(point_data.get("standard"), "point.standard"),
        "indicated": _as_float(point_data.get("indicated"), "point.indicated"),
        "uncertainty": _as_float(point_data.get("uncertainty"), "point.uncertainty"),
        "due_at": str(point_data.get("due_at") or "").strip(),
    }
    if not point["due_at"]:
        raise ValidationError("point.due_at is required")
    if point["uncertainty"] < 0:
        raise ValidationError("point.uncertainty must not be negative")
    points = list(entity["data"].get("points", []))
    points.append(point)
    return {"_next_status": entity["status"], "_remove": ["point"], "points": points}


def calibration_current(due_at, as_of):
    return str(due_at) >= str(as_of)


def _collect_calibration_points(lookup, instrument_id):
    points = []
    if lookup is None:
        return points
    calibrations = lookup("calibration", "instrument_id", instrument_id) or []
    for calibration in calibrations:
        if calibration["status"] not in ("passed", "approved"):
            continue
        if calibration["data"].get("result") != "passed":
            continue
        for point in calibration["data"].get("points", []):
            enriched = dict(point)
            enriched["calibration_id"] = calibration["id"]
            points.append(enriched)
    return points


def _interpolated_correction(lower, upper, value):
    c_low = lower["standard"] - lower["indicated"]
    c_high = upper["standard"] - upper["indicated"]
    span = upper["standard"] - lower["standard"]
    if span <= 0:
        return round(c_low, 6)
    ratio = (value - lower["standard"]) / span
    return round(c_low + (c_high - c_low) * ratio, 6)


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

    def hold(reason):
        return {
            "_next_status": "pending",
            "release_decision": "held",
            "hold_reason": reason,
        }

    points = _collect_calibration_points(lookup, instrument["id"])
    if not points:
        return hold("校准点不足：仪器没有已登记的校准点")
    valid = [point for point in points if calibration_current(point["due_at"], _today())]
    if not valid:
        return hold("校准已过期：所有校准点已过到期日")
    if len(valid) < 2:
        return hold("校准点不足：有效校准点少于 2 个")
    valid.sort(key=lambda point: point["standard"])
    low, high = valid[0], valid[-1]
    if value < low["standard"] or value > high["standard"]:
        return hold("测得值超出校准覆盖区间 [%s, %s]" % (low["standard"], high["standard"]))
    lower = upper = None
    for index in range(len(valid) - 1):
        if valid[index]["standard"] <= value <= valid[index + 1]["standard"]:
            lower, upper = valid[index], valid[index + 1]
            break
    correction = _interpolated_correction(lower, upper, value)
    return {
        "released_by": actor.user_id,
        "release_decision": "released",
        "hold_reason": None,
        "original_value": value,
        "correction": correction,
        "corrected_value": round(value + correction, 6),
        "calibration_points": [lower, upper],
    }


CUSTOM_CREATE = {'calibration': _validate_calibration}
CUSTOM_TRANSITIONS = {('calibration', 'perform'): _validate_perform, ('calibration', 'register_point'): _validate_register_point, ('result', 'release'): _validate_result_release}


class RuleEngine:
    ALIASES = {'instruments': 'instrument', 'calibrations': 'calibration', 'methods': 'method', 'results': 'result'}
    INITIAL_STATUS = {'instrument': 'active', 'calibration': 'requested', 'method': 'draft', 'result': 'pending'}
    TRANSITIONS = {'instrument': {'send_calibration': (('active',), 'calibrating'), 'calibrate': (('calibrating',), 'active'), 'quarantine': (('active',), 'quarantined'), 'restore': (('quarantined',), 'active')}, 'calibration': {'perform': (('requested', 'failed'), 'passed'), 'approve': (('passed',), 'approved'), 'reject': (('failed',), 'rejected'), 'register_point': (('passed', 'approved'), 'passed')}, 'method': {'validate_method': (('draft',), 'validated'), 'revoke_method': (('validated',), 'revoked')}, 'result': {'release': (('pending',), 'released'), 'block': (('pending',), 'blocked'), 'reanalyze': (('blocked',), 'pending')}}
    CREATE_REQUIRED = {'instrument': ('name', 'serial'), 'calibration': ('instrument_id', 'requested_at'), 'method': ('name', 'version'), 'result': ('sample_id', 'measurement')}
    ACTION_REQUIRED = {('instrument', 'calibrate'): ('due_at', 'passed'), ('instrument', 'quarantine'): ('reason',), ('calibration', 'perform'): ('result', 'performed_at', 'uncertainty'), ('calibration', 'approve'): ('authorized_by',), ('calibration', 'reject'): ('reason',), ('calibration', 'register_point'): ('point',), ('method', 'validate_method'): ('parameters', 'instrument_ids'), ('method', 'revoke_method'): ('reason',), ('result', 'release'): ('instrument_id', 'method_id', 'value', 'unit'), ('result', 'block'): ('reason',), ('result', 'reanalyze'): ('reason',)}
    CREATE_ROLES = {'instrument': ('admin', 'technician'), 'calibration': ('admin', 'metrology'), 'method': ('admin', 'authorizer'), 'result': ('admin', 'analyst')}
    ROLE_ACTIONS = {'send_calibration': ('admin', 'technician'), 'calibrate': ('admin', 'metrology'), 'quarantine': ('admin', 'metrology'), 'restore': ('admin', 'metrology'), 'perform': ('admin', 'metrology'), 'approve': ('admin', 'authorizer'), 'reject': ('admin', 'authorizer'), 'register_point': ('admin', 'metrology'), 'validate_method': ('admin', 'authorizer'), 'revoke_method': ('admin', 'authorizer'), 'release': ('admin', 'analyst'), 'block': ('admin', 'analyst'), 'reanalyze': ('admin', 'analyst')}

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
        extra = dict(extra) if extra else {}
        next_status = extra.pop("_next_status", next_status)
        patch = dict(data)
        for key in extra.pop("_remove", ()):
            patch.pop(key, None)
        patch.update(extra)
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
