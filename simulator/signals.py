"""Vectorised process simulation: sensors, PI loops, calculations, lab samples, status and faults.

Raw model values live in `values`, indexed by Tag.index. Faults and calculations are applied on a
copy, `out`, which together with `quality` is what gets published.
"""

import math
from collections import defaultdict

import numpy as np

GOOD, UNCERTAIN, BAD = 0, 1, 2

# Fault types
NONE, F_BAD, F_STUCK, F_UNCERTAIN, F_SPIKE = 0, 1, 2, 3, 4
FAULT_NAMES = {F_BAD: "bad", F_STUCK: "stuck", F_UNCERTAIN: "uncertain", F_SPIKE: "spike"}
FAULT_WEIGHTS = [0.35, 0.25, 0.25, 0.15]

# Loop process model in normalised units: u_ss = bias + GAIN * op + disturbance,
# with bias chosen per loop so the output sits near 50 % at the normal operating point
GAIN = 0.6

CALC_FUNCS = {
    "clip": np.clip, "sqrt": np.sqrt, "log": np.log, "exp": np.exp, "abs": np.abs,
    "minimum": np.minimum, "maximum": np.maximum, "where": np.where,
}


def _arr(tags, name, default=0.0):
    return np.array([float(t.params.get(name, default)) for t in tags])


class Simulation:
    def __init__(self, plant, dt, lab_interval=3600.0, fault_share=0.01, fault_duration=600.0,
                 seed=42, now=0.0):
        self.plant = plant
        self.dt = dt
        self.rng = np.random.default_rng(seed)
        tags = plant.tags
        n = len(tags)
        self.values = np.array([t.base for t in tags], dtype=float)
        self.quality = np.zeros(n, dtype=np.int8)
        self.lo = np.array([t.lo for t in tags])
        self.hi = np.array([t.hi for t in tags])
        self.span = np.maximum(self.hi - self.lo, 1e-9)

        loop_pv = {pv.index for pv, _, _ in plant.loops}
        self._init_sensors([t for t in tags if t.kind == "pv" and t.index not in loop_pv])
        self._init_loops(plant.loops)
        self._init_status([t for t in tags if t.kind == "status"])
        self._init_gates(plant.equipment)
        self._init_calcs(plant.equipment)
        self._init_labs([t for t in tags if t.kind == "lv"], plant.equipment, lab_interval, now)
        self._init_faults(tags, fault_share, fault_duration, now)
        self.out = self.values.copy()
        self.calc()

    # ---------------------------------------------------------------- sensors
    def _init_sensors(self, tags):
        self.s_idx = np.array([t.index for t in tags], dtype=int)
        span = self.span[self.s_idx]
        self.s_base = self.values[self.s_idx].copy()
        self.s_noise = _arr(tags, "noise") * span
        self.s_drift = _arr(tags, "drift") * span
        self.s_tau = np.maximum(_arr(tags, "tau", 900), self.dt)
        self.s_daily = _arr(tags, "daily") * span
        self.s_phase = self.rng.uniform(0, 2 * np.pi, len(tags))
        self.s_state = self.rng.normal(0, 1, len(tags)) * self.s_drift

    def _step_sensors(self, t_wall):
        n = len(self.s_idx)
        a = self.dt / self.s_tau
        self.s_state += -self.s_state * a + self.s_drift * np.sqrt(2 * a) * self.rng.standard_normal(n)
        day = 2 * np.pi * (t_wall % 86400) / 86400
        v = (self.s_base + self.s_state + self.s_daily * np.sin(day + self.s_phase)
             + self.s_noise * self.rng.standard_normal(n))
        self.values[self.s_idx] = np.clip(v, self.lo[self.s_idx], self.hi[self.s_idx])

    # ---------------------------------------------------------------- PI loops
    def _init_loops(self, loops):
        pvs = [pv for pv, _, _ in loops]
        self.l_pv = np.array([pv.index for pv, _, _ in loops], dtype=int)
        self.l_sp = np.array([sp.index for _, sp, _ in loops], dtype=int)
        self.l_op = np.array([op.index for _, _, op in loops], dtype=int)
        m = len(loops)
        self.l_lo, self.l_span = self.lo[self.l_pv], self.span[self.l_pv]
        self.l_ubase = (self.values[self.l_pv] - self.l_lo) / self.l_span
        self.l_usp = self.l_ubase.copy()
        self.l_u = self.l_usp.copy()
        self.l_noise = _arr(pvs, "noise")
        self.l_drift = _arr(pvs, "drift")
        self.l_tau = np.maximum(_arr(pvs, "tau", 600), self.dt)
        self.l_taup = np.maximum(_arr(pvs, "tau_p", 60), 2 * self.dt)
        self.l_steprate = _arr(pvs, "sp_step_rate", 0.5) / 3600.0
        self.l_dist = np.zeros(m)
        self.l_bias = self.l_ubase - GAIN * 0.5
        # IMC tuning with closed loop time constant tau_p / 2
        self.l_kc = 2.0 / GAIN
        self.l_ti = self.l_taup
        op = np.clip((self.l_usp - self.l_bias) / GAIN, 0, 1)
        self.l_int = op * self.l_ti / self.l_kc
        self.values[self.l_op] = op * 100

    def set_external_sp(self, positions, sp_values):
        """Setpoints written by an OPC UA client (positions index into the loop arrays)."""
        u = (np.asarray(sp_values) - self.l_lo[positions]) / self.l_span[positions]
        self.l_usp[positions] = np.clip(u, 0, 1)

    def _step_loops(self):
        m = len(self.l_pv)
        if m == 0:
            return
        dt = self.dt
        # operator setpoint changes
        step = self.rng.random(m) < self.l_steprate * dt
        if step.any():
            k = np.flatnonzero(step)
            delta = self.rng.choice([-1, 1], len(k)) * self.rng.uniform(0.02, 0.08, len(k))
            lo = np.maximum(self.l_ubase[k] - 0.15, 0.05)
            hi = np.minimum(self.l_ubase[k] + 0.15, 0.95)
            self.l_usp[k] = np.clip(self.l_usp[k] + delta, lo, hi)
        # unmeasured disturbance
        a = dt / self.l_tau
        self.l_dist += -self.l_dist * a + self.l_drift * np.sqrt(2 * a) * self.rng.standard_normal(m)
        meas = self.l_u + self.l_noise * self.rng.standard_normal(m)
        e = self.l_usp - meas
        self.l_int += e * dt
        raw = self.l_kc * (e + self.l_int / self.l_ti)
        op = np.clip(raw, 0, 1)
        sat = raw != op
        self.l_int[sat] -= e[sat] * dt  # anti-windup: stop integrating while saturated
        self.l_u += np.minimum(dt / self.l_taup, 0.5) * (self.l_bias + GAIN * op + self.l_dist - self.l_u)
        self.values[self.l_pv] = self.l_lo + self.l_span * np.clip(meas, 0, 1)
        self.values[self.l_sp] = self.l_lo + self.l_span * self.l_usp
        self.values[self.l_op] = op * 100

    # ---------------------------------------------------------------- status
    def _init_status(self, tags):
        self.st_idx = np.array([t.index for t in tags], dtype=int)
        self.st_trip = _arr(tags, "trip_rate", 0.02) / 3600.0
        self.st_down = _arr(tags, "down_time", 900)
        self.st_restart = np.zeros(len(tags))

    def _step_status(self, t):
        if len(self.st_idx) == 0:
            return
        running = self.values[self.st_idx] > 0.5
        trip = running & (self.rng.random(len(running)) < self.st_trip * self.dt)
        if trip.any():
            k = np.flatnonzero(trip)
            self.st_restart[k] = t + self.rng.exponential(self.st_down[k])
        restart = ~running & (t >= self.st_restart)
        self.values[self.st_idx[trip]] = 0.0
        self.values[self.st_idx[restart]] = 1.0

    def _init_gates(self, equipment):
        # gated signals read zero (or the nearest range limit) while their equipment is stopped
        g, s = [], []
        for eq in equipment:
            for t in eq.tags:
                key = t.params.get("gate")
                if key and t.kind == "pv":
                    g.append(t.index)
                    s.append(eq.keys[key].index)
        self.g_idx = np.array(g, dtype=int)
        self.g_status = np.array(s, dtype=int)

    def _apply_gates(self):
        if len(self.g_idx):
            stopped = self.values[self.g_status] < 0.5
            g = self.g_idx[stopped]
            self.values[g] = np.clip(0.0, self.lo[g], self.hi[g])

    # ---------------------------------------------------------------- calculations
    def _init_calcs(self, equipment):
        # Group by (template, key) so each expression is evaluated once over all instances
        groups = defaultdict(lambda: {"out": [], "ns": defaultdict(list)})
        order = []
        for eq in equipment:
            for t in eq.tags:
                if t.kind != "cv":
                    continue
                gk = (eq.template, t.params["key"])
                if gk not in groups:
                    order.append(gk)
                grp = groups[gk]
                grp["expr"] = t.params["expr"]
                grp["out"].append(t.index)
                for k, kt in eq.keys.items():
                    grp["ns"][k].append(kt.index)
        self.calcs = []
        for gk in order:
            grp = groups[gk]
            code = compile(grp["expr"], f"<calc {gk[0]}.{gk[1]}>", "eval")
            names = [k for k in grp["ns"] if k in code.co_names]
            self.calcs.append((code, np.array(grp["out"], dtype=int),
                               {k: np.array(grp["ns"][k], dtype=int) for k in names}))

    def calc(self):
        with np.errstate(all="ignore"):
            for code, out, ns in self.calcs:
                env = {k: self.out[idx] for k, idx in ns.items()}
                v = np.asarray(eval(code, {"__builtins__": {}, **CALC_FUNCS}, env), dtype=float)
                v = np.nan_to_num(v, nan=0.0, posinf=self.hi[out], neginf=self.lo[out])
                self.out[out] = np.clip(v, self.lo[out], self.hi[out])

    # ---------------------------------------------------------------- lab values
    def _init_labs(self, tags, equipment, interval, now):
        self.lab_interval = float(interval)
        self.lb_idx = np.array([t.index for t in tags], dtype=int)
        # lab noise and drift are relative to the value (analysis repeatability), not the range
        scale = np.maximum(np.abs(self.values[self.lb_idx]), 1e-6)
        follow = {}
        for eq in equipment:
            for t in eq.tags:
                f = t.params.get("follows")
                if t.kind == "lv" and f:
                    follow[t.index] = eq.keys[f].index
        self.lb_follow = np.array([follow.get(t.index, -1) for t in tags], dtype=int)
        self.lb_base = self.values[self.lb_idx].copy()
        self.lb_noise = _arr(tags, "noise") * scale
        self.lb_drift = _arr(tags, "drift") * scale
        self.lb_tau = np.maximum(_arr(tags, "tau", 21600), 1.0)
        self.lb_state = self.rng.normal(0, 1, len(tags)) * self.lb_drift
        # Samples are staggered over the interval; first results arrive within one interval
        self.lb_due = now + self.rng.uniform(0, self.lab_interval, len(tags))
        self.lb_sample_time = np.full(len(tags), now)
        self._lab_sample(np.ones(len(tags), dtype=bool), now)

    def _lab_sample(self, mask, now):
        k = np.flatnonzero(mask)
        if len(k) == 0:
            return k
        a = np.minimum(self.lab_interval / self.lb_tau[k], 1.0)
        self.lb_state[k] += -self.lb_state[k] * a + self.lb_drift[k] * np.sqrt(2 * a) * self.rng.standard_normal(len(k))
        f = self.lb_follow[k]
        ref = np.where(f >= 0, self.values[np.maximum(f, 0)], self.lb_base[k] + self.lb_state[k])
        v = ref + self.lb_noise[k] * self.rng.standard_normal(len(k))
        idx = self.lb_idx[k]
        self.values[idx] = np.clip(v, self.lo[idx], self.hi[idx])
        # result is reported 15-45 min (max 3/4 interval) after the sample was taken
        delay = self.rng.uniform(0.25, 0.75, len(k)) * min(self.lab_interval, 3600.0)
        self.lb_sample_time[k] = now - delay
        return idx

    def _step_labs(self, now):
        due = now >= self.lb_due
        if not due.any():
            return np.empty(0, dtype=int)
        self.lb_due[due] += self.lab_interval * np.ceil((now - self.lb_due[due] + 1e-9) / self.lab_interval)
        return self._lab_sample(due, now)

    def lab_source_time(self, tag_indices):
        """Sample timestamps (epoch seconds) for the given lab tag indices."""
        pos = np.searchsorted(self.lb_idx, tag_indices)
        return self.lb_sample_time[pos]

    # ---------------------------------------------------------------- faults
    def _init_faults(self, tags, share, duration, now):
        eligible = [t.index for t in tags if t.kind in ("pv", "lv")]
        self.f_idx = np.array(eligible, dtype=int)
        self.f_type = np.zeros(len(eligible), dtype=np.int8)
        self.f_end = np.zeros(len(eligible))
        self.f_value = np.zeros(len(eligible))
        self.f_duration = duration
        share = min(max(share, 0.0), 0.5)
        self.f_rate = share / (duration * (1 - share)) if share > 0 else 0.0
        self.fault_starts = 0
        # start at the steady state share instead of building up over the first fault durations
        self._start_faults(np.flatnonzero(self.rng.random(len(eligible)) < share), now)

    def _start_faults(self, k, t):
        if len(k) == 0:
            return
        self.fault_starts += len(k)
        types = self.rng.choice([F_BAD, F_STUCK, F_UNCERTAIN, F_SPIKE], len(k), p=FAULT_WEIGHTS)
        self.f_type[k] = types
        dur = self.rng.exponential(self.f_duration, len(k))
        self.f_end[k] = t + np.where(types == F_SPIKE, self.dt * 0.5, dur)
        self.f_value[k] = self.values[self.f_idx[k]]

    def _step_faults(self, t):
        if len(self.f_idx) == 0:
            return
        n = len(self.f_idx)
        active = self.f_type != NONE
        ended = active & (t >= self.f_end)
        self.f_type[ended] = NONE
        if self.f_rate > 0:
            start = (self.f_type == NONE) & (self.rng.random(n) < self.f_rate * self.dt)
            self._start_faults(np.flatnonzero(start), t)
        idx = self.f_idx
        ft = self.f_type
        out = self.out
        q = np.zeros(n, dtype=np.int8)
        stuck = (ft == F_STUCK) | (ft == F_BAD)
        out[idx[stuck]] = self.f_value[stuck]
        q[ft == F_BAD] = BAD
        unc = ft == F_UNCERTAIN
        if unc.any():
            q[unc] = UNCERTAIN
            j = idx[unc]
            out[j] = np.clip(out[j] + 0.03 * self.span[j] * self.rng.standard_normal(len(j)),
                                     self.lo[j], self.hi[j])
        spike = ft == F_SPIKE
        if spike.any():
            j = idx[spike]
            sign = self.rng.choice([-1, 1], len(j))
            out[j] = np.clip(out[j] + sign * 0.3 * self.span[j], self.lo[j], self.hi[j])
        self.quality[idx] = q

    def fault_counts(self):
        return {name: int(np.count_nonzero(self.f_type == code)) for code, name in FAULT_NAMES.items()}

    # ---------------------------------------------------------------- tick
    def step(self, t):
        """Advance one tick. Returns indices of lab tags that got a new sample."""
        self._step_status(t)
        self._step_sensors(t)
        self._step_loops()
        self._apply_gates()
        lab = self._step_labs(t)
        # faults hit the published signal before calculations, so CVs inherit stuck or spiking inputs
        np.copyto(self.out, self.values)
        self._step_faults(t)
        self.calc()
        return lab
