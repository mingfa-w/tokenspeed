# Copyright (c) 2026 LightSeek Foundation
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""Validate instance placement and derive gateway/host-local ports."""

import hashlib
import ipaddress
import json
import os
import re
import shlex
import sys
from collections import Counter, defaultdict
from pathlib import Path

PORT_DEFAULTS = dict(
    HTTP_PORT=8010,
    ENGINE_PORT=8120,
    METRICS_PORT=8121,
    GATEWAY_METRICS_PORT=8122,
    HANDSHAKE_PORT=9010,
    DIST_PORT=9600,
    BOOTSTRAP_PORT=8998,
)


def parse_rows(text):
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        fields = shlex.split(line, comments=True)
        if not fields:
            continue
        if len(fields) != 4:
            raise ValueError(f"layout line {number}: expected ID ROLE IP DEVICES")
        name, role, node, devices = fields
        rows.append(dict(id=name, role=role, node=node, devices=devices))
    return rows


def normalize(settings, supplied=None):
    mode = settings["SERVING_MODE"]
    if mode not in ("pd", "merged"):
        raise ValueError("SERVING_MODE must be pd or merged")
    explicit = bool(settings.get("PD_LAYOUT") or settings.get("PD_LAYOUT_FILE"))
    if supplied is None:
        if settings.get("PD_LAYOUT") and settings.get("PD_LAYOUT_FILE"):
            raise ValueError("choose PD_LAYOUT or PD_LAYOUT_FILE, not both")
        if explicit:
            if settings.get("DS_IPLIST") or settings.get("DEVICE_POOLS"):
                raise ValueError(
                    "explicit layout cannot be combined with DS_IPLIST/DEVICE_POOLS"
                )
            text = (
                settings.get("PD_LAYOUT")
                or Path(settings["PD_LAYOUT_FILE"]).read_text()
            )
            supplied = (
                json.loads(text)
                if text.lstrip().startswith(("{", "["))
                else parse_rows(text)
            )
        else:
            nodes = settings.get("DS_IPLIST", "").split()
            if len(nodes) != (1 if mode == "merged" else 3) or len(set(nodes)) != len(
                nodes
            ):
                raise ValueError("set PD_LAYOUT, or use the legacy DS_IPLIST topology")
            pools = (
                settings.get("DEVICE_POOLS") or "0,1,2,3,4,5,6,7;8,9,10,11,12,13,14,15"
            )
            supplied = []
            counters = Counter()
            for devices in pools.split(";"):
                for index, node in enumerate(nodes):
                    role = (
                        "regular"
                        if mode == "merged"
                        else "prefill" if index == 0 else "decode"
                    )
                    prefix = {"regular": "m", "prefill": "p", "decode": "d"}[role]
                    supplied.append(
                        dict(
                            id=f"{prefix}{counters[role]}",
                            role=role,
                            node=node,
                            devices=devices,
                        )
                    )
                    counters[role] += 1
    raw = supplied if isinstance(supplied, dict) else dict(instances=supplied)
    if "mode" in raw and raw["mode"] != mode:
        raise ValueError("layout mode differs from SERVING_MODE")
    if not isinstance(raw.get("instances"), list):
        raise ValueError("layout must contain an instances list")
    capacity = int(
        settings.get("NPU_DEVICES_PER_HOST", raw.get("devices_per_host", 16))
    )
    if capacity < 8 or capacity % 8:
        raise ValueError("NPU_DEVICES_PER_HOST must be a positive multiple of 8")
    full = str(
        settings.get(
            "REQUIRE_FULL_NODE", raw.get("require_full_node", 1 if explicit else 0)
        )
    ).lower()
    if full not in ("0", "1", "false", "true"):
        raise ValueError("REQUIRE_FULL_NODE must be 0 or 1")
    require_full = full in ("1", "true")
    ports = {
        key: int(settings.get(key, raw.get("base_ports", {}).get(key, value)))
        for key, value in PORT_DEFAULTS.items()
    }
    stride = int(settings.get("POOL_PORT_STRIDE", raw.get("port_stride", 100)))
    if stride < 10:
        raise ValueError("POOL_PORT_STRIDE must be at least 10")
    rows, used, slots, ids = [], defaultdict(set), Counter(), set()
    aliases = dict(p="prefill", d="decode", m="regular", merged="regular")
    for entry in raw["instances"]:
        if not isinstance(entry, dict) or not {
            "id",
            "role",
            "node",
            "devices",
        }.issubset(entry):
            raise ValueError("each instance needs id, role, node and devices")
        if not isinstance(entry["role"], str) or not isinstance(entry["node"], str):
            raise ValueError("role and node must be strings")
        name = entry["id"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name)
            or name in ids
        ):
            raise ValueError(f"invalid or duplicate instance id: {name}")
        role = aliases.get(entry["role"], entry["role"])
        if role not in (("prefill", "decode") if mode == "pd" else ("regular",)):
            raise ValueError(f"role {role} does not match {mode}")
        address = ipaddress.IPv4Address(entry["node"])
        if address.is_unspecified or address.is_multicast:
            raise ValueError("node must be an actual unicast address")
        node = str(address)
        devices = entry["devices"]
        if not isinstance(devices, (str, list)):
            raise ValueError("devices must be a comma-separated string or list")
        values = devices.split(",") if isinstance(devices, str) else devices
        if any(isinstance(value, bool) or not str(value).isdigit() for value in values):
            raise ValueError(f"{name}: invalid device IDs")
        devices = [int(value) for value in values]
        if (
            len(devices) != 8
            or len(set(devices)) != 8
            or set(devices) not in [set(range(i, i + 8)) for i in range(0, capacity, 8)]
        ):
            raise ValueError(f"{name}: select one complete eight-device HCCS group")
        if used[node].intersection(devices):
            raise ValueError(f"{name}: device overlap on {node}")
        ids.add(name)
        used[node].update(devices)
        slot = slots[node]
        slots[node] += 1
        own_ports = {
            key: value
            + (0 if key in ("HTTP_PORT", "GATEWAY_METRICS_PORT") else slot * stride)
            for key, value in ports.items()
        }
        # Every worker's direct handshake binds on the single gateway host.
        own_ports["HANDSHAKE_PORT"] = ports["HANDSHAKE_PORT"] + len(rows)
        rows.append(
            dict(
                id=name,
                role=role,
                node=node,
                devices=devices,
                slot=slot,
                ports=own_ports,
            )
        )
    if not rows:
        raise ValueError("empty layout")
    counts = Counter(row["role"] for row in rows)
    if mode == "pd" and (not counts["prefill"] or not counts["decode"]):
        raise ValueError("PD requires at least one prefill and one decode instance")
    if require_full:
        for node, devices in used.items():
            if devices != set(range(capacity)):
                raise ValueError(f"{node}: not all {capacity} devices assigned")
    owner = (
        settings.get("GATEWAY_INSTANCE")
        or raw.get("gateway_instance")
        or next(row["id"] for row in rows if row["role"] != "decode")
    )
    if not isinstance(owner, str) or owner not in ids:
        raise ValueError("GATEWAY_INSTANCE is not in layout")
    gateway = next(row["node"] for row in rows if row["id"] == owner)
    occupied = {}

    def reserve(node, port, label):
        if not 1024 <= port <= 65535:
            raise ValueError(f"invalid port {port}: {label}")
        key = (node, port)
        if key in occupied:
            raise ValueError(
                f"port collision on {node}:{port}: {occupied[key]} and {label}"
            )
        occupied[key] = label

    for row in rows:
        p = row["ports"]
        local = [p["ENGINE_PORT"], p["METRICS_PORT"], p["DIST_PORT"]]
        local += [p["DIST_PORT"] + offset for offset in (1, 3, 4, 5, 6)]
        if row["role"] == "prefill":
            local.append(p["BOOTSTRAP_PORT"])
        for port in local:
            reserve(row["node"], port, row["id"])
        row["local_ports"] = local
    owner_row = next(row for row in rows if row["id"] == owner)
    for port in [ports["HTTP_PORT"], ports["GATEWAY_METRICS_PORT"]] + [
        row["ports"]["HANDSHAKE_PORT"] for row in rows
    ]:
        reserve(gateway, port, "gateway")
        owner_row["local_ports"].append(port)
    plan = dict(
        mode=mode,
        instances=rows,
        gateway_instance=owner,
        gateway_host=gateway,
        worker_counts=dict(counts),
        devices_per_host=capacity,
        require_full_node=require_full,
        base_ports=ports,
        port_stride=stride,
    )
    plan["fingerprint"] = hashlib.sha256(
        json.dumps(plan, sort_keys=True).encode()
    ).hexdigest()
    return plan


def summarize(plan):
    counts = plan["worker_counts"]
    print(
        f"mode={plan['mode']} P={counts.get('prefill',0)} D={counts.get('decode',0)} regular={counts.get('regular',0)} "
        f"P_cards={8*counts.get('prefill',0)} D_cards={8*counts.get('decode',0)} total_cards={8*len(plan['instances'])}"
    )
    print(
        f"gateway={plan['gateway_instance']} host={plan['gateway_host']} port={plan['base_ports']['HTTP_PORT']}"
    )
    for row in plan["instances"]:
        print(
            f"id={row['id']} role={row['role']} host={row['node']} devices={','.join(map(str,row['devices']))} "
            f"slot={row['slot']} engine={row['ports']['ENGINE_PORT']} handshake={row['ports']['HANDSHAKE_PORT']} TP8/EP8/PP1"
        )


if __name__ == "__main__":
    if len(sys.argv) == 1:
        print(json.dumps(normalize(os.environ), separators=(",", ":")))
    else:
        plan = json.loads(Path(sys.argv[2]).read_text())
        if sys.argv[1] == "rows":
            for row in plan["instances"]:
                print(
                    "\t".join(
                        [
                            row["id"],
                            row["role"],
                            row["node"],
                            ",".join(map(str, row["devices"])),
                            str(row["slot"]),
                        ]
                    )
                )
        elif sys.argv[1] == "summary":
            summarize(plan)
        else:
            raise SystemExit("unknown layout command")
