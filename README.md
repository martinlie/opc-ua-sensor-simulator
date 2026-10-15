# OPC UA Plant Simulation Server

An OPC UA server, written in Python, that simulates a large made-up refinery: the **Nordvik Refinery (NVR)**. It is built for stress testing OPC UA clients. The plant has a realistic ISA-95 style asset tree and thousands of tags that behave like real process data: slowly drifting sensors with noise, PI control loops, calculated values, hourly lab results, equipment trips and failing sensors.

The plant size can be scaled from about 1,000 tags to over 100,000.

This is a fork of [flopach/opc-ua-sensor-simulator](https://github.com/flopach/opc-ua-sensor-simulator). The original replayed 3 signals from a Kaggle pump data set; that CSV replay has been replaced by a simulated process.

## Installation

Python 3.9 or newer is required.

```bash
git clone https://github.com/martinlie/opc-ua-sensor-simulator
cd opc-ua-sensor-simulator
pip install -r requirements.txt
python opc-ua-server.py
```

The server listens on `opc.tcp://127.0.0.1:4840/opcua/` (no security). Use `--endpoint opc.tcp://0.0.0.0:4840/opcua/` to accept connections from other machines.

## Asset tree

```
Objects
└── NVR                          Nordvik Refinery            (Plant)
    ├── A10                      Crude                       (Area)
    │   ├── U11                  Crude Distillation          (Unit)
    │   │   ├── 11-H101          Fired heater                (Equipment)
    │   │   │   ├── 11-TIC011_PV Coil outlet temperature     (Tag)
    │   │   │   ├── 11-TIC011_SP
    │   │   │   ├── 11-TIC011_OP
    │   │   │   ├── 11-TI012_PV  Stack temperature
    │   │   │   ├── 11-AI013_PV  Flue gas oxygen
    │   │   │   └── 11-UI016_CV  Thermal efficiency
    │   │   ├── 11-C101          Distillation column
    │   │   ├── 11-P101 … P103   Centrifugal pumps
    │   │   └── 11-S101 … S103   Product sample points (lab values)
    │   ├── U12                  Vacuum Distillation
    │   └── U13                  Desalter
    ├── A20  Conversion          U21 FCC, U22 Hydrocracker, U23 Alkylation
    ├── A30  Treating            U31 Naphtha HDT, U32 Diesel HDT, U33 Sulfur Recovery
    ├── A40  Reforming           U41 Catalytic Reformer, U42 Aromatics Extraction
    ├── A50  Utilities           U51 Steam, U52 Cooling Water, U53 Instrument Air, U54 Flare, U55 Power
    ├── A60  Offsites            U61 Tank Farm, U62 Blending, U63 Loading
    └── A70  Environment         U71 Wastewater Treatment, U72 Emissions Monitoring
```

NodeIds are fixed strings built from the path, for example:

```
ns=2;s=NVR.A10.U11.11-H101.11-TIC011_PV
```

They stay the same across restarts. Unit and equipment base values are seeded per unit, so they don't change when you scale the plant up with `--trains`. Client configurations built against a small plant still work against a large one.

Every variable has a `Description` (with the engineering unit), and by default also the `EURange` and `EngineeringUnits` (UNECE codes) properties of an `AnalogItemType`. Status tags are `TwoStateDiscreteType` with `TrueState`/`FalseState`.

## Tag naming

Tags follow an ISA-5.1 style: `<unit>-<letters><number>_<postfix>`, e.g. `11-FIC036_SP`.

| Postfix | Meaning | Behaviour |
|---|---|---|
| `_PV` | Process value | Sensors: slow random drift around the operating point, white noise, optional 24 h cycle (e.g. cooling water temperature). Status tags (`XS`) are Boolean `_PV`s. |
| `_SP` | Set point | Steady most of the time; an "operator" occasionally steps it. **Writable**: clients can write a new setpoint and the loop follows. |
| `_OP` | Controller output (pådrag), % | Output of a PI controller that drives the loop's `_PV` towards the `_SP` through a first-order process with disturbances. |
| `_CV` | Calculated value | Computed from other tags on the same equipment: hydraulic power, heat duty, LMTD, column ΔP, efficiency, surge margin, bed ΔT, power factor and more. |
| `_LV` | Lab value | Sample-and-hold, updated once per lab interval (default 1 h), staggered across the plant. `SourceTimestamp` is the time the sample was taken, 15–45 min before the result is published. Some lab values follow an online analyzer, e.g. lab density versus online density. |

## Equipment templates

| Template | Prefix | Tags |
|---|---|---|
| Pump | `P` | running status, discharge pressure, motor current, vibration, bearing temperature, flow loop, hydraulic power |
| Compressor | `K` | running status, suction/discharge pressure, discharge temperature, vibration, speed loop, compression ratio, surge margin |
| Column | `C` | top/bottom temperature and pressure, bottom level loop, reflux flow loop, ΔP |
| Heat exchanger | `E` | four temperatures, flow, duty, LMTD |
| Fired heater | `H` | coil outlet temperature loop, stack temperature, O₂, fuel gas, draft, efficiency |
| Drum | `V` | level loop, pressure loop, temperature |
| Reactor | `R` | bed temperatures, ΔP, pressure loop, bed ΔT, WABT |
| Tank | `T` | level, temperature (slow), in/out flow, inventory |
| Sample point | `S` | product flow, online density, lab density, sulfur, flash point |
| Emissions analyzer | `Q` | NOx, SO₂, CO, O₂, corrected NOx (slow) |
| Generator | `G` | breaker status, MW, MVAr, voltage, frequency, current, power factor |

When a pump, compressor or generator trips, its running status goes `False` and its gated signals (current, vibration, discharge pressure, power) drop to zero until it restarts.

Everything lives in [`simulator/plant.yaml`](simulator/plant.yaml): areas, units, the equipment mix per unit, tag templates, ranges, noise, drift and time constants. Edit it to build your own plant.

## Sensor faults

By default 1 % of sensor and lab tags are in a fault at any time (`--faults`). The faults start at random times, last on average 10 min (`--fault-duration`), and are of these types:

| Fault | Published as |
|---|---|
| Sensor failure | `BadSensorFailure`, value null |
| Stuck/frozen | `Good`, value stops changing |
| Inaccurate | `UncertainSensorNotAccurate`, extra noise |
| Spike | `Good`, a single outlier sample |

Calculated values inherit faulty inputs, just as they would in a real control system.

## Scaling and stress testing

`--trains N` duplicates every unit as parallel trains (`U11`, `U11B`, `U11C`, …):

| `--trains` | Tags | Equipment | Startup |
|---|---|---|---|
| 1 (default) | 1,046 | 140 | < 1 s |
| 10 | 10,460 | 1,400 | ~6 s |
| 100 | 104,600 | 14,000 | ~60 s, ~3.5 GB RAM |

Update rates:

* **fast** tags (most `_PV`, `_OP`, `_CV`) are written every `--fast-ms` (default 1000 ms)
* **slow** tags (tank levels and temperatures, emissions analyzers) every `--slow-s` (default 10 s)
* **lab** tags every `--lab-s` (default 3600 s; use e.g. `--lab-s 60` to test them quickly)
* `_SP` and status tags are written only when they change

Only changed values are written. Every 10 s the server logs its throughput: writes/s, tick time, load, overruns, active faults and client setpoint writes. This helps you tell whether the server or the client is the bottleneck. Measured on an Apple Silicon laptop:

* about 75k writes/s with no subscribers (100k tags at 1 s, about 50 % load)
* one client subscribed to all 10k tags at `--trains 10` (about 8k notifications/s) put the server at about 40 % load, so expect a ceiling of roughly 20k notifications/s per server process

To go beyond that, increase `--fast-ms`, use `--no-metadata`, or run several server instances on different ports.

Export the full tag list (NodeId, tag, kind, data type, unit, range, rate, description) for your client configuration:

```bash
python opc-ua-server.py --trains 10 --export-tags tags.csv
```

## Options

```
--endpoint URL        server endpoint (default opc.tcp://127.0.0.1:4840/opcua/)
--config YAML         plant definition (default simulator/plant.yaml)
--trains N            parallel trains per unit (default 1, ~1k tags)
--fast-ms MS          fast tag update period (default 1000)
--slow-s S            slow tag update period (default 10)
--lab-s S             lab sample interval (default 3600)
--faults SHARE        share of sensor/lab tags in a fault (default 0.01, 0 disables)
--fault-duration S    mean fault duration (default 600)
--seed N              random seed (default 42)
--export-tags CSV     write the tag list to a CSV file
--no-metadata         skip EURange/EngineeringUnits properties
--stats-s S           throughput log interval (default 10)
--log-level LEVEL     logging level (default INFO)
```

## Testing

Any OPC UA client works, for example [UaExpert](https://www.unified-automation.com/products/development-tools/uaexpert.html) or the [FreeOpcUa client GUI](https://github.com/FreeOpcUa/opcua-client-gui).

## License

This project is licensed under the LGPL-3.0 License, see [LICENSE.md](LICENSE.md). It is based on the original OPC UA simulation server by Flo Pachinger (Cisco Systems), which is itself based on the [FreeOpcUa](https://github.com/FreeOpcUa) server examples.
