"""OPC UA server: builds the address space from the plant model and publishes the simulation."""

import asyncio
import csv
import logging
import time
from datetime import datetime, timezone

import numpy as np
from asyncua import Server, ua

from .signals import BAD, GOOD, UNCERTAIN, Simulation

# asyncua 1.x calls this DataValue field StatusCode_, 2.x StatusCode
_STATUS_KW = "StatusCode_" if "StatusCode_" in ua.DataValue.__dataclass_fields__ else "StatusCode"

_logger = logging.getLogger("simulator")

NAMESPACE_URI = "urn:opc-ua-sensor-simulator:nordvik-refinery"

# UNECE Recommendation 20 common codes for EUInformation
UNECE = {
    "degC": "CEL", "barg": "BAR", "bar": "BAR", "mbar": "MBR", "m3/h": "MQH", "Nm3/h": "MQH", "A": "AMP",
    "mm/s": "C16", "um": "4H", "rpm": "RPM", "%": "P1", "-": "C62", "kW": "KWT", "MW": "MAW",
    "kg/m3": "KMQ", "ppm": "59", "t/h": "E18", "m3": "MTQ", "mg/Nm3": "GP", "MVAr": "MAR",
    "kV": "KVT", "Hz": "HTZ",
}

QUALITY = {
    GOOD: ua.StatusCode(ua.StatusCodes.Good),
    UNCERTAIN: ua.StatusCode(ua.StatusCodes.UncertainSensorNotAccurate),
    BAD: ua.StatusCode(ua.StatusCodes.BadSensorFailure),
}

REF_COMPONENT = ua.NodeId(ua.ObjectIds.HasComponent)
REF_PROPERTY = ua.NodeId(ua.ObjectIds.HasProperty)
REF_ORGANIZES = ua.NodeId(ua.ObjectIds.Organizes)


def _eu_info(eu):
    code = UNECE.get(eu)
    unit_id = -1
    if code:
        unit_id = 0
        for c in code:
            unit_id = (unit_id << 8) | ord(c)
    return ua.EUInformation(NamespaceUri="http://www.opcfoundation.org/UA/units/un/cefact",
                            UnitId=unit_id, DisplayName=ua.LocalizedText(eu), Description=ua.LocalizedText(eu))


def _object_item(nodeid, parent, bname, display, desc, ref=REF_COMPONENT):
    item = ua.AddNodesItem()
    item.RequestedNewNodeId = nodeid
    item.BrowseName = bname
    item.ParentNodeId = parent
    item.ReferenceTypeId = ref
    item.NodeClass = ua.NodeClass.Object
    item.TypeDefinition = ua.NodeId(ua.ObjectIds.BaseObjectType)
    attrs = ua.ObjectAttributes()
    attrs.EventNotifier = 0
    attrs.DisplayName = ua.LocalizedText(display)
    attrs.Description = ua.LocalizedText(desc)
    item.NodeAttributes = attrs
    return item


def _variable_item(nodeid, parent, bname, display, desc, variant, datatype, typedef,
                   ref=REF_COMPONENT, writable=False):
    item = ua.AddNodesItem()
    item.RequestedNewNodeId = nodeid
    item.BrowseName = bname
    item.NodeClass = ua.NodeClass.Variable
    item.ParentNodeId = parent
    item.ReferenceTypeId = ref
    item.TypeDefinition = ua.NodeId(typedef)
    attrs = ua.VariableAttributes()
    attrs.DisplayName = ua.LocalizedText(display)
    attrs.Description = ua.LocalizedText(desc)
    attrs.DataType = ua.NodeId(datatype)
    attrs.Value = variant
    attrs.ValueRank = ua.ValueRank.Scalar
    access = ua.AccessLevel.CurrentRead.mask
    if writable:
        access |= ua.AccessLevel.CurrentWrite.mask
    attrs.AccessLevel = access
    attrs.UserAccessLevel = access
    item.NodeAttributes = attrs
    return item


def _property_item(parent, ns, name, variant, datatype):
    return _variable_item(ua.NodeId(f"{parent.Identifier}.{name}", ns), parent, ua.QualifiedName(name, 0),
                          name, "", variant, datatype, ua.ObjectIds.PropertyType, ref=REF_PROPERTY)


async def build_address_space(server, ns, plant, metadata=True, batch=5000):
    """Adds Plant/Area/Unit/Equipment objects and tag variables. Returns NodeIds by tag index."""
    items = []
    session = server.iserver.isession

    async def flush(force=False):
        if items and (force or len(items) >= batch):
            for r in await session.add_nodes(items):
                r.StatusCode.check()
            items.clear()

    def nid(path):
        return ua.NodeId(path, ns)

    def qn(name):
        return ua.QualifiedName(name, ns)

    root = nid(plant.id)
    items.append(_object_item(root, ua.NodeId(ua.ObjectIds.ObjectsFolder), qn(plant.id),
                              plant.name, plant.name, ref=REF_ORGANIZES))
    tag_nodes = [None] * len(plant.tags)
    for area in plant.areas:
        items.append(_object_item(nid(area.path), root, qn(area.id), f"{area.id} {area.name}", area.name))
        for unit in area.units:
            items.append(_object_item(nid(unit.path), nid(area.path), qn(unit.id),
                                      f"{unit.id} {unit.name}", unit.name))
            for eq in unit.equipment:
                eq_node = nid(eq.path)
                items.append(_object_item(eq_node, nid(unit.path), qn(eq.id), eq.id, eq.name))
                for t in eq.tags:
                    node = nid(t.path)
                    tag_nodes[t.index] = node
                    if t.vtype == "Boolean":
                        variant = ua.Variant(bool(t.base), ua.VariantType.Boolean)
                        dtype = ua.ObjectIds.Boolean
                        typedef = ua.ObjectIds.TwoStateDiscreteType if metadata else ua.ObjectIds.BaseDataVariableType
                    else:
                        variant = ua.Variant(float(t.base), ua.VariantType.Double)
                        dtype = ua.ObjectIds.Double
                        typedef = ua.ObjectIds.AnalogItemType if metadata else ua.ObjectIds.BaseDataVariableType
                    desc = f"{t.desc} [{t.eu}]" if t.eu else t.desc
                    items.append(_variable_item(node, eq_node, qn(t.name), t.name, desc, variant, dtype, typedef,
                                                writable=(t.kind == "sp")))
                    if metadata:
                        if t.vtype == "Boolean":
                            for name, text in (("TrueState", "Running"), ("FalseState", "Stopped")):
                                items.append(_property_item(node, ns, name, ua.Variant(ua.LocalizedText(text)),
                                                            ua.ObjectIds.LocalizedText))
                        else:
                            items.append(_property_item(node, ns, "EURange",
                                                        ua.Variant(ua.Range(Low=t.lo, High=t.hi)),
                                                        ua.ObjectIds.Range))
                            items.append(_property_item(node, ns, "EngineeringUnits", ua.Variant(_eu_info(t.eu)),
                                                        ua.ObjectIds.EUInformation))
                    await flush()
    await flush(force=True)
    return tag_nodes


def export_tags(path, plant, ns):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["nodeid", "tag", "kind", "datatype", "eu", "lo", "hi", "rate", "description", "path"])
        for t in plant.tags:
            w.writerow([f"ns={ns};s={t.path}", t.name, t.kind, t.vtype, t.eu, t.lo, t.hi, t.rate, t.desc,
                        t.path.replace(".", "/")])
    _logger.info("Exported %d tags to %s", len(plant.tags), path)


class Publisher:
    """Runs the simulation on a fixed tick and writes changed values into the address space."""

    def __init__(self, server, plant, nodes, args):
        self.server = server
        self.aspace = server.iserver.aspace
        self.plant = plant
        self.nodes = nodes
        self.period = args.fast_ms / 1000.0
        self.slow_every = max(1, round(args.slow_s / self.period))
        self.stats_s = args.stats_s
        self.sim = Simulation(plant, dt=self.period, lab_interval=args.lab_s, fault_share=args.faults,
                              fault_duration=args.fault_duration, seed=args.seed, now=time.time())
        kinds = np.array([t.kind for t in plant.tags])
        rates = np.array([t.rate for t in plant.tags])
        self.is_bool = np.array([t.vtype == "Boolean" for t in plant.tags])
        # sp and status are only written when they change; lab tags only on a new sample
        continuous = (kinds != "sp") & (kinds != "status") & (rates != "lab")
        self.fast_mask = continuous & (rates == "fast")
        self.slow_mask = continuous & (rates == "slow")
        self.event_mask = (kinds == "sp") | (kinds == "status")
        self.lab_mask_all = rates == "lab"
        self.last_value = np.full(len(plant.tags), np.nan)
        self.last_quality = np.full(len(plant.tags), -1, dtype=np.int8)
        self.sp_pos = np.arange(len(self.sim.l_sp))
        self.sp_nodes = [nodes[i] for i in self.sim.l_sp]
        self.writes = 0
        self.sp_client_writes = 0

    def _read_client_setpoints(self):
        """Pick up setpoints that clients wrote since our last write."""
        changed_pos, changed_val = [], []
        for pos, node in zip(self.sp_pos, self.sp_nodes):
            dv = self.aspace.read_attribute_value(node, ua.AttributeIds.Value)
            v = dv.Value.Value if dv.Value is not None else None
            last = self.last_value[self.sim.l_sp[pos]]
            if isinstance(v, float) and not np.isnan(last) and v != last:
                changed_pos.append(pos)
                changed_val.append(v)
        if changed_pos:
            self.sp_client_writes += len(changed_pos)
            self.sim.set_external_sp(np.array(changed_pos), changed_val)

    async def _write(self, indices, now_dt, source_times=None):
        sim = self.sim
        out, qual = sim.out, sim.quality
        aspace = self.aspace
        value_attr = ua.AttributeIds.Value
        for n, i in enumerate(indices):
            if self.is_bool[i]:
                variant = ua.Variant(bool(out[i] > 0.5), ua.VariantType.Boolean)
            else:
                variant = ua.Variant(float(out[i]), ua.VariantType.Double)
            src = now_dt if source_times is None else source_times[n]
            dv = ua.DataValue(variant, **{_STATUS_KW: QUALITY[int(qual[i])]}, SourceTimestamp=src,
                              ServerTimestamp=now_dt)
            await aspace.write_attribute_value(self.nodes[i], value_attr, dv)
            if n % 2000 == 1999:
                await asyncio.sleep(0)  # let the server serve clients during large bursts
        self.last_value[indices] = out[indices]
        self.last_quality[indices] = qual[indices]
        self.writes += len(indices)

    def _lab_times(self, indices):
        return [datetime.fromtimestamp(s, timezone.utc) for s in self.sim.lab_source_time(indices)]

    async def run(self):
        sim = self.sim
        tick, ticks = 0, 0
        # initial image of every tag, lab values with their sample timestamps
        now_dt = datetime.now(timezone.utc)
        lab_idx = np.flatnonzero(self.lab_mask_all)
        await self._write(np.flatnonzero(~self.lab_mask_all), now_dt)
        await self._write(lab_idx, now_dt, self._lab_times(lab_idx))
        next_t = time.monotonic()
        stats_t0, stats_writes, busy, worst, overruns = time.monotonic(), 0, 0.0, 0.0, 0
        _logger.info("Publishing %d tags every %.0f ms (slow every %d ticks, lab every %.0f s, faults %.1f%%)",
                     len(self.plant.tags), self.period * 1000, self.slow_every, sim.lab_interval,
                     100 * sim.f_rate * sim.f_duration / (1 + sim.f_rate * sim.f_duration))
        while True:
            t0 = time.monotonic()
            now = time.time()
            now_dt = datetime.fromtimestamp(now, timezone.utc)
            self._read_client_setpoints()
            lab = sim.step(now)

            due = self.fast_mask | self.event_mask
            if tick % self.slow_every == 0:
                due = due | self.slow_mask
            changed = (sim.out != self.last_value) | (sim.quality != self.last_quality)
            idx = np.flatnonzero(due & changed)
            await self._write(idx, now_dt)
            if len(lab):
                await self._write(lab, now_dt, self._lab_times(lab))
            # lab tags whose fault state changed between samples
            lab_q = np.flatnonzero(self.lab_mask_all & (sim.quality != self.last_quality))
            if len(lab_q):
                await self._write(lab_q, now_dt, self._lab_times(lab_q))

            elapsed = time.monotonic() - t0
            busy += elapsed
            worst = max(worst, elapsed)
            tick += 1
            ticks += 1
            next_t += self.period
            delay = next_t - time.monotonic()
            if delay < 0:
                overruns += 1
                next_t = time.monotonic()
                delay = 0
            span = time.monotonic() - stats_t0
            if span >= self.stats_s:
                w = self.writes - stats_writes
                _logger.info("%.0f writes/s | tick avg %.1f ms max %.1f ms | load %.0f%% | overruns %d | "
                             "faults %s | client SP writes %d",
                             w / span, 1000 * busy / max(ticks, 1), 1000 * worst, 100 * busy / span, overruns,
                             sim.fault_counts(), self.sp_client_writes)
                stats_t0, stats_writes, busy, worst, overruns, ticks = time.monotonic(), self.writes, 0.0, 0.0, 0, 0
            await asyncio.sleep(delay)


async def serve(plant, args):
    server = Server()
    await server.init()
    server.set_endpoint(args.endpoint)
    server.set_server_name(f"{plant.name} OPC UA Simulator")
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
    ns = await server.register_namespace(NAMESPACE_URI)

    t0 = time.monotonic()
    nodes = await build_address_space(server, ns, plant, metadata=not args.no_metadata)
    _logger.info("Built address space: %d tags, %d equipment in %.1f s",
                 len(plant.tags), len(plant.equipment), time.monotonic() - t0)
    if args.export_tags:
        export_tags(args.export_tags, plant, ns)

    publisher = Publisher(server, plant, nodes, args)
    async with server:
        _logger.info("Server listening on %s", args.endpoint)
        await publisher.run()
