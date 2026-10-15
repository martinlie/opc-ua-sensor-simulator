"""Builds the plant asset tree (Plant/Area/Unit/Equipment/Tag) from plant.yaml."""

import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import yaml

DEFAULT_CONFIG = Path(__file__).with_name("plant.yaml")

# Postfix per published signal
POSTFIX = {"pv": "PV", "sp": "SP", "op": "OP", "cv": "CV", "lv": "LV", "status": "PV"}


@dataclass
class Tag:
    index: int
    name: str            # e.g. 11-PT012_PV
    path: str            # e.g. NVR.A10.U11.11-P101.11-PT012_PV (also the string NodeId)
    kind: str            # pv | sp | op | cv | lv | status
    vtype: str           # Double | Boolean
    desc: str
    eu: str
    lo: float
    hi: float
    rate: str            # fast | slow | lab
    params: dict = field(repr=False, default_factory=dict)
    base: float = 0.0


@dataclass
class Equipment:
    id: str
    name: str
    path: str
    template: str
    tags: List[Tag] = field(default_factory=list)
    # local key -> tag (loops register key, key_sp and key_op)
    keys: Dict[str, Tag] = field(default_factory=dict, repr=False)


@dataclass
class Unit:
    id: str
    name: str
    path: str
    equipment: List[Equipment] = field(default_factory=list)


@dataclass
class Area:
    id: str
    name: str
    path: str
    units: List[Unit] = field(default_factory=list)


@dataclass
class Plant:
    id: str
    name: str
    areas: List[Area]
    tags: List[Tag]
    equipment: List[Equipment]
    loops: List[tuple]   # (pv, sp, op) tags


def train_suffix(n: int) -> str:
    """Train 0 has no suffix so tag names stay stable when scaling up; then B, C, ... Z, AA, AB ..."""
    if n == 0:
        return ""
    s = ""
    while n > 0:
        n, r = divmod(n, 26)
        s = chr(ord("A") + r) + s
    return s


def _merge(defaults: dict, tdef: dict) -> dict:
    kind = tdef.get("kind", "pv")
    p = dict(defaults.get("loop" if kind == "loop" else kind, {}))
    p.update(tdef)
    p["kind"] = kind
    return p


def _base(p: dict, rng: np.random.Generator) -> Optional[float]:
    b = p.get("base")
    if b is None:
        return None
    if isinstance(b, (list, tuple)):
        return float(rng.uniform(b[0], b[1]))
    return float(b)


def load_plant(config_path=DEFAULT_CONFIG, trains: int = 1, seed: int = 42) -> Plant:
    cfg = yaml.safe_load(Path(config_path).read_text())
    defaults = cfg.get("defaults", {})
    templates = cfg["templates"]
    pid = cfg["plant"]["id"]
    tags: List[Tag] = []
    all_eq: List[Equipment] = []
    loops: List[tuple] = []
    areas: List[Area] = []

    def new_tag(eq, name, kind, vtype, p, rate, base):
        lo, hi = p.get("range", [0, 1])
        t = Tag(len(tags), name, f"{eq.path}.{name}", kind, vtype, p.get("desc", ""), p.get("eu", ""),
                float(lo), float(hi), rate, p, base if base is not None else (lo + hi) / 2)
        tags.append(t)
        eq.tags.append(t)
        return t

    for a in cfg["areas"]:
        area = Area(a["id"], a["name"], f"{pid}.{a['id']}")
        areas.append(area)
        for train in range(trains):
            for u in a["units"]:
                code = f"{u['id']}{train_suffix(train)}"
                unit = Unit(f"U{code}", u["name"] + (f" Train {train_suffix(train)}" if train else ""),
                            f"{area.path}.U{code}")
                area.units.append(unit)
                # Per-unit RNG so base values do not change when --trains changes
                rng = np.random.default_rng([seed, zlib.crc32(code.encode())])
                seq = 0
                for tname, count in u["equipment"].items():
                    tpl = templates[tname]
                    for k in range(count):
                        seq += 1
                        eq_id = f"{code}-{tpl['prefix']}{101 + k}"
                        eq = Equipment(eq_id, tpl.get("desc", tname), f"{unit.path}.{eq_id}", tname)
                        unit.equipment.append(eq)
                        all_eq.append(eq)
                        if len(tpl["tags"]) > 9:
                            raise ValueError(f"template {tname}: max 9 tag definitions")
                        for i, tdef in enumerate(tpl["tags"], start=1):
                            p = _merge(defaults, tdef)
                            kind = p["kind"]
                            loop_no = f"{seq:02d}{i}"
                            stem = f"{code}-{p['isa']}{loop_no}"
                            rate = p.get("rate", "fast")
                            base = _base(p, rng)
                            if kind == "loop":
                                pv = new_tag(eq, f"{stem}_PV", "pv", "Double", p, rate, base)
                                sp = new_tag(eq, f"{stem}_SP", "sp", "Double", p, rate, pv.base)
                                op_p = dict(p, range=[0, 100], eu="%", desc=p.get("desc", "") + " output")
                                op = new_tag(eq, f"{stem}_OP", "op", "Double", op_p, rate, 50.0)
                                loops.append((pv, sp, op))
                                eq.keys[p["key"]] = pv
                                eq.keys[p["key"] + "_sp"] = sp
                                eq.keys[p["key"] + "_op"] = op
                            elif kind == "status":
                                t = new_tag(eq, f"{stem}_PV", "status", "Boolean",
                                            dict(p, range=[0, 1], eu=""), rate, 1.0)
                                eq.keys[p["key"]] = t
                            else:
                                if kind == "lv":
                                    rate = "lab"
                                t = new_tag(eq, f"{stem}_{POSTFIX[kind]}", kind, "Double", p, rate, base)
                                eq.keys[p["key"]] = t
                        # lab tags that follow an online PV take its base value
                        for t in eq.tags:
                            f = t.params.get("follows")
                            if t.kind == "lv" and f:
                                t.base = eq.keys[f].base
    return Plant(pid, cfg["plant"]["name"], areas, tags, all_eq, loops)
