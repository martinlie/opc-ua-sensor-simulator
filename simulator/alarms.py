"""Simulated alarms, warnings and messages published as OPC UA events.

Conditions (Part 9 style, retained so clients can ConditionRefresh):
  * limit alarms    LL/L/H/HH on sensor and loop PVs, with deadband (ExclusiveLimitAlarmType fields)
  * trip alarms     a status tag (pump/compressor running feedback) went to stopped
  * sensor alarms   a PV has bad quality
Plain events: warnings and operator messages (BaseEventType).

Process upsets push a random PV outside its normal band for a while, so the alarm follows from the
published value. Operator acknowledgement is simulated: acked a little after activation, there are no
Acknowledge/Confirm methods.

Events are only delivered to monitored items on the exact node that emits them, so every event is
emitted on the Server object and on the plant, area, unit and equipment above its source.
"""

import logging
import uuid
from datetime import datetime, timezone

import numpy as np
from asyncua import ua
from asyncua.common import event_objects

from .signals import BAD

_logger = logging.getLogger("simulator")

# severity 1..1000
SEV = {"HH": 800, "LL": 800, "H": 500, "L": 500, "trip": 700, "sensor": 300, "warning": 400, "message": 100}
LEVEL_NAME = {2: "HH", 1: "H", -1: "L", -2: "LL"}
LEVEL_TEXT = {2: "High high", 1: "High", -1: "Low", -2: "Low low"}

TYPE_FALLBACK = {ua.ObjectIds.UtcTime: ua.VariantType.DateTime, ua.ObjectIds.Duration: ua.VariantType.Double,
                 ua.ObjectIds.AudioDataType: ua.VariantType.ByteString}

WARNINGS = [
    "Strainer differential pressure trending high on {eq}",
    "Maintenance window starts in 30 minutes on {eq}",
    "{eq} approaching service interval",
    "Analyzer calibration overdue on {eq}",
    "Vibration trend increasing on {eq}",
]
MESSAGES = [
    "Operator took manual control of {eq}",
    "Lab sample received for {eq}",
    "{eq} returned to automatic control",
    "Shift handover completed in {unit}",
    "Setpoint change confirmed on {eq}",
]


def _utc(t):
    return datetime.fromtimestamp(t, timezone.utc)


def _text(s):
    return ua.LocalizedText(s)


class AlarmEngine:
    def __init__(self, server, ns, plant, sim, args, now):
        self.sub = server.iserver.isession.subscription_service
        self.ns = ns
        self.plant = plant
        self.sim = sim
        self.rng = np.random.default_rng(args.seed + 1)
        self.events = 0
        self.conditions = {}   # key -> dict(active, acked, level, ack_at)
        self._build_sources()
        tags = plant.tags
        kinds = np.array([t.kind for t in tags])
        loop_pv = {pv.index for pv, _, _ in plant.loops}

        # limit alarms on sensor and loop PVs
        self.a_idx = np.array([t.index for t in tags if t.kind == "pv" or t.index in loop_pv], dtype=int)
        base = np.array([tags[i].base for i in self.a_idx])
        span = sim.span[self.a_idx]
        lo, hi = sim.lo[self.a_idx], sim.hi[self.a_idx]
        # a limit that would sit on (or beyond) the range edge is disabled
        up, down = base + np.array([[0.18], [0.10]]) * span, base - np.array([[0.18], [0.10]]) * span
        self.lim = {
            2: np.where(up[0] < hi, up[0], np.inf), 1: np.where(up[1] < hi, up[1], np.inf),
            -1: np.where(down[1] > lo, down[1], -np.inf), -2: np.where(down[0] > lo, down[0], -np.inf),
        }
        self.deadband = 0.01 * span
        self.a_state = np.zeros(len(self.a_idx), dtype=np.int8)
        self.a_bad = np.zeros(len(self.a_idx), dtype=bool)

        # trip alarms on status tags
        self.t_idx = np.flatnonzero(kinds == "status")
        self.t_state = np.zeros(len(self.t_idx), dtype=bool)

        # process upsets and plain events, rates scale with plant size
        kilo = len(self.a_idx) / 1000.0
        self.upset_rate = args.upsets_per_hour * kilo / 3600.0
        self.msg_rate = args.messages_per_hour * kilo / 3600.0
        self.upsets = []       # dict(pos, offset, t0, ramp, hold)
        self.a_pos_of = {int(i): p for p, i in enumerate(self.a_idx)}
        self.next_tick = now
        self.dt = sim.dt
        self.active_count = 0
        _logger.info("Alarms: %d limit-alarm tags, %d trip tags, ~%.1f upsets/h, ~%.1f messages/h",
                     len(self.a_idx), len(self.t_idx), self.upset_rate * 3600, self.msg_rate * 3600)

    # ------------------------------------------------------------------ sources
    def _build_sources(self):
        """For each tag: (equipment, unit, area) objects, to find the emitting nodes."""
        ns = self.ns
        self.root = ua.NodeId(self.plant.id, ns)
        self.eq_of = {}
        self.eqs = self.plant.equipment
        for area in self.plant.areas:
            for unit in area.units:
                for eq in unit.equipment:
                    chain = [ua.NodeId(eq.path, ns), ua.NodeId(unit.path, ns), ua.NodeId(area.path, ns)]
                    eq._chain = chain
                    eq._unit = unit
                    for t in eq.tags:
                        self.eq_of[t.index] = eq

    # ------------------------------------------------------------------ emit
    async def _emit(self, ev, chain):
        ev.EventId = ua.Variant(uuid.uuid4().hex.encode("utf-8"), ua.VariantType.ByteString)
        now = datetime.now(timezone.utc)
        ev.Time = now
        ev.ReceiveTime = now
        ev.LocalTime = ua.uaprotocol_auto.TimeZoneDataType()
        ev.LocalTime.Offset = 0
        ev.LocalTime.DaylightSavingInOffset = False
        self.events += 1
        for node in [*chain, self.root, ua.NodeId(ua.ObjectIds.Server)]:
            ev.emitting_node = node
            await self.sub.trigger_event(ev)

    def _condition(self, cls, tag, eq, key, name, message, severity, active, acked, retain, state_text, now):
        ev = cls(sourcenode=ua.NodeId(tag.path, self.ns), message=message, severity=severity)
        # asyncua declares some fields by data type NodeId, which Variant cannot encode
        for k, t in ev.data_types.items():
            if isinstance(t, ua.NodeId):
                ev.data_types[k] = TYPE_FALLBACK.get(t.Identifier, ua.VariantType.Variant)
        ev.SourceName = tag.name
        ev.ConditionName = name
        ev.add_property("NodeId", ua.NodeId(f"{tag.path}.{key}", self.ns), ua.VariantType.NodeId)
        ev.Retain = retain
        ev.add_property("Quality", ua.StatusCode(ua.StatusCodes.Good), ua.VariantType.StatusCode)
        setattr(ev, "EnabledState/Id", True)
        ev.EnabledState = _text("Enabled")
        setattr(ev, "ActiveState/Id", active)
        ev.ActiveState = _text("Active" if active else "Inactive")
        setattr(ev, "ActiveState/TransitionTime", _utc(now))
        setattr(ev, "AckedState/Id", acked)
        ev.AckedState = _text("Acknowledged" if acked else "Unacknowledged")
        setattr(ev, "ConfirmedState/Id", True)
        ev.ConfirmedState = _text("Confirmed")
        ev.SuppressedOrShelved = False
        ev.add_property("LimitState/CurrentState", _text(state_text), ua.VariantType.LocalizedText)
        return ev

    async def _condition_event(self, tag, key, name, message, severity, state_text, now, active, new=False):
        eq = self.eq_of[tag.index]
        c = self.conditions.setdefault((tag.index, key), {"active": False, "acked": True})
        if new:
            c["acked"] = False
            c["ack_at"] = now + float(self.rng.uniform(5, 180))
        c["active"] = active
        retain = c["active"] or not c["acked"]
        cls = event_objects.ExclusiveLimitAlarm if key == "limit" else event_objects.AlarmCondition
        ev = self._condition(cls, tag, eq, key, name, message, severity, active, c["acked"], retain,
                             state_text, now)
        c["last"] = (tag, key, name, message, severity, state_text)
        await self._emit(ev, eq._chain)
        if not retain:
            self.conditions.pop((tag.index, key), None)

    async def _acks(self, now):
        for k, c in list(self.conditions.items()):
            if not c["acked"] and now >= c["ack_at"]:
                c["acked"] = True
                tag, key, name, message, severity, state_text = c["last"]
                await self._condition_event(tag, key, name, message, severity, state_text, now, c["active"])

    async def _plain(self, text, severity, eq, now):
        ev = event_objects.BaseEvent(sourcenode=ua.NodeId(eq.path, self.ns), message=text, severity=severity)
        ev.SourceName = eq.id
        await self._emit(ev, eq._chain)

    # ------------------------------------------------------------------ upsets
    def _start_upsets(self, now):
        if self.upset_rate <= 0 or len(self.a_idx) == 0:
            return
        for _ in range(self.rng.poisson(self.upset_rate * self.dt)):
            pos = int(self.rng.integers(len(self.a_idx)))
            span = self.sim.span[self.a_idx[pos]]
            sign = self.rng.choice([-1.0, 1.0])
            self.upsets.append({"pos": pos, "offset": sign * float(self.rng.uniform(0.12, 0.25)) * span,
                                "t0": now, "ramp": float(self.rng.uniform(20, 90)),
                                "hold": float(self.rng.uniform(60, 600))})

    def apply_upsets(self, now):
        """Called after sim.step and before publishing: shifts the published PV values."""
        self._start_upsets(now)
        if not self.upsets:
            return
        out, sim = self.sim.out, self.sim
        keep = []
        for u in self.upsets:
            age = now - u["t0"]
            total = 2 * u["ramp"] + u["hold"]
            if age >= total:
                continue
            keep.append(u)
            f = min(age / u["ramp"], 1.0, (total - age) / u["ramp"])
            i = self.a_idx[u["pos"]]
            if sim.quality[i] != BAD:
                out[i] = np.clip(out[i] + f * u["offset"], sim.lo[i], sim.hi[i])
        self.upsets = keep
        sim.calc()  # calculated values follow the shifted inputs

    # ------------------------------------------------------------------ evaluate
    async def step(self, now):
        sim = self.sim
        tags = self.plant.tags
        out = sim.out

        # limit alarms with deadband, evaluated on the published value
        v = out[self.a_idx]
        st = self.a_state
        db = self.deadband
        lim = self.lim
        level = np.where(v >= lim[2] - db * (st == 2), 2,
                np.where(v >= lim[1] - db * (st >= 1), 1,
                np.where(v <= lim[-2] + db * (st == -2), -2,
                np.where(v <= lim[-1] + db * (st <= -1), -1, 0)))).astype(np.int8)
        bad = sim.quality[self.a_idx] == BAD
        level[bad] = st[bad]   # no limit alarm decisions on a failed signal
        for p in np.flatnonzero(level != st):
            await self._limit_change(int(p), int(st[p]), int(level[p]), float(v[p]), now)
        self.a_state = level

        # sensor failure
        for p in np.flatnonzero(bad != self.a_bad):
            tag = tags[self.a_idx[p]]
            if bad[p]:
                await self._condition_event(tag, "sensor", "Sensor failure", f"{tag.name} sensor failure",
                                            SEV["sensor"], "Bad", now, True, new=True)
            else:
                await self._condition_event(tag, "sensor", "Sensor failure", f"{tag.name} sensor restored",
                                            SEV["sensor"], "Normal", now, False)
        self.a_bad = bad

        # trips
        stopped = out[self.t_idx] < 0.5
        for p in np.flatnonzero(stopped != self.t_state):
            tag = tags[self.t_idx[p]]
            eq = self.eq_of[tag.index]
            if stopped[p]:
                await self._condition_event(tag, "trip", "Equipment stopped", f"{eq.id} {eq.name} stopped",
                                            SEV["trip"], "Stopped", now, True, new=True)
            else:
                await self._condition_event(tag, "trip", "Equipment stopped", f"{eq.id} {eq.name} running",
                                            SEV["trip"], "Running", now, False)
        self.t_state = stopped

        # warnings and operator messages
        if self.msg_rate > 0 and self.eqs:
            for _ in range(self.rng.poisson(self.msg_rate * self.dt)):
                eq = self.eqs[int(self.rng.integers(len(self.eqs)))]
                warn = self.rng.random() < 0.4
                text = self.rng.choice(WARNINGS if warn else MESSAGES).format(eq=eq.id, unit=eq._unit.id)
                await self._plain(text, SEV["warning"] if warn else SEV["message"], eq, now)

        await self._acks(now)
        self.active_count = sum(1 for c in self.conditions.values() if c["active"])

    async def _limit_change(self, p, old, new, value, now):
        tag = self.plant.tags[self.a_idx[p]]
        if new == 0:
            await self._condition_event(tag, "limit", "Limit alarm", f"{tag.name} back to normal ({value:.4g} {tag.eu})",
                                        SEV[LEVEL_NAME[old]], "Normal", now, False)
            return
        name = LEVEL_NAME[new]
        limit = float(self.lim[new][p])
        await self._condition_event(
            tag, "limit", "Limit alarm", f"{tag.name} {LEVEL_TEXT[new].lower()} alarm: {value:.4g} {tag.eu} "
            f"(limit {limit:.4g})", SEV[name], LEVEL_TEXT[new], now, True, new=(old == 0 or abs(new) > abs(old)))
