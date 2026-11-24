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

"""Shared layout validation and runtime rendering for the deployment launcher."""

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
    if (
        settings.get("DS_IPLIST")
        or settings.get("DEVICE_POOLS")
        or settings.get("DEPLOY_ENTRY")
    ):
        raise ValueError(
            "旧入口参数已移除，请使用HOSTS或PD_LAYOUT/PD_LAYOUT_FILE；所有动作统一通过run_deepseek_v41_flash.sh"
        )
    mode = settings["SERVING_MODE"]
    if mode not in ("pd", "merged"):
        raise ValueError("SERVING_MODE must be pd or merged")
    explicit = bool(settings.get("PD_LAYOUT") or settings.get("PD_LAYOUT_FILE"))
    if supplied is None:
        if settings.get("PD_LAYOUT") and settings.get("PD_LAYOUT_FILE"):
            raise ValueError("choose PD_LAYOUT or PD_LAYOUT_FILE, not both")
        if explicit:
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
            nodes = settings.get("HOSTS", "").split()
            if not nodes or len(set(nodes)) != len(nodes):
                raise ValueError("HOSTS must contain distinct node addresses")
            capacity = int(settings.get("NPU_DEVICES_PER_HOST", "16"))
            groups = [
                (node, list(range(i, i + 8)))
                for node in nodes
                for i in range(0, capacity, 8)
            ]
            if mode == "pd":
                values = [
                    settings.get(key, default)
                    for key, default in [
                        ("PREFILL_INSTANCES", "2"),
                        ("DECODE_INSTANCES", "4"),
                    ]
                ]
                if any(not str(value).isdigit() or int(value) <= 0 for value in values):
                    raise ValueError("P/D instance counts must be positive integers")
                p, d = map(int, values)
                if p + d != len(groups):
                    raise ValueError(
                        f"P+D must equal {len(groups)} eight-device groups; set HOSTS and instance counts"
                    )
            counters = Counter()
            supplied = []
            for index, (node, devices) in enumerate(groups):
                role = (
                    "regular"
                    if mode == "merged"
                    else "prefill" if index < p else "decode"
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
        settings.get("REQUIRE_FULL_NODE", raw.get("require_full_node", 1))
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


def configure(settings):
    """Write runtime files from explicit serving/code modes and node addresses."""
    mode, code = settings["SERVING_MODE"], settings["CODE_MODE"]
    if mode not in {"merged", "pd"} or code not in {"wheel", "source"}:
        raise ValueError("select SERVING_MODE=merged|pd and CODE_MODE=wheel|source")
    root = Path(settings["PD_ROOT"])
    if not root.is_absolute() or any(c.isspace() for c in str(root)):
        raise ValueError("PD_ROOT must be absolute and contain no whitespace")
    plan = normalize(settings, json.loads(settings["LAYOUT_JSON"]))
    matches = [
        entry for entry in plan["instances"] if entry["id"] == settings["INSTANCE_ID"]
    ]
    if len(matches) != 1:
        raise ValueError("INSTANCE_ID is not in layout")
    entry = matches[0]
    node, role = entry["node"], entry["role"]
    if node != settings["NODE_IP"] or entry["devices"] != [
        int(x) for x in settings["DEVICES"].split(",")
    ]:
        raise ValueError("instance node/devices differ from validated layout")
    prefill = plan["gateway_host"]
    decodes = list(
        dict.fromkeys(
            row["node"] for row in plan["instances"] if row["role"] == "decode"
        )
    )
    ports, base_ports = entry["ports"], plan["base_ports"]
    pool_index = entry["slot"]
    pool_count = sum(row["node"] == node for row in plan["instances"])
    owns_gateway = entry["id"] == plan["gateway_instance"]
    rpc = ports["HANDSHAKE_PORT"]
    local_ports = entry["local_ports"]
    devices = settings["DEVICES"].split(",")
    if (
        len(devices) != 8
        or len(set(devices)) != 8
        or not all(d.isdigit() for d in devices)
    ):
        raise ValueError(
            "this validated TP8 recipe requires eight distinct physical device IDs"
        )
    defaults = {
        "MAX_MODEL_LEN": "512",
        "MAX_TOTAL_TOKENS": "1024",
        "CHUNKED_PREFILL_SIZE": "512",
        "PREFILL_MEM_FRACTION": "0.85",
        "DECODE_MEM_FRACTION": "0.85",
        "PREFILL_MAX_NUM_SEQS": "1",
        "DECODE_MAX_NUM_SEQS": "1",
    }
    launch_config = {
        key: str(settings.get(key, value)) for key, value in defaults.items()
    }
    for key in [
        "MAX_MODEL_LEN",
        "MAX_TOTAL_TOKENS",
        "CHUNKED_PREFILL_SIZE",
        "PREFILL_MAX_NUM_SEQS",
        "DECODE_MAX_NUM_SEQS",
    ]:
        if not 0 < int(launch_config[key]) < 2**31:
            raise ValueError(f"invalid {key}")
    for key in ["PREFILL_MEM_FRACTION", "DECODE_MEM_FRACTION"]:
        if not 0 < float(launch_config[key]) < 1:
            raise ValueError(f"invalid {key}")
    if int(launch_config["MAX_TOTAL_TOKENS"]) < int(launch_config["MAX_MODEL_LEN"]):
        raise ValueError("MAX_TOTAL_TOKENS must cover MAX_MODEL_LEN")
    role_prefix = "DECODE" if role == "decode" else "PREFILL"
    runtime = root / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    q = shlex.quote
    base, model = settings["BASE_ROOT"], settings["MODEL_PATH"]
    env = f"""#!/usr/bin/env bash
set +u
source {q(base + '/runtime/env.sh')}
TS_BASE_PATH="$PATH"
source {q(str(root / 'venv/bin/activate'))}
export PATH={q(str(root / 'venv/bin'))}:"$TS_BASE_PATH"
unset TS_BASE_PATH
export TS_DEPLOY_ROOT={q(str(root))} TS_RUNTIME_DIR={q(str(runtime))}
export TS_MODEL_PATH={q(model)} TOKENSPEED_HOST_IP={q(node)}
export ASCEND_RT_VISIBLE_DEVICES={q(settings['DEVICES'])}
export ASCEND_CACHE_PATH={q(str(root / 'ascend-cache'))}
export ASCEND_PROCESS_LOG_PATH={q(str(root / 'ascend-log'))}
export NO_PROXY='*' no_proxy='*' MC_LOG_LEVEL=INFO
mkdir -p "$ASCEND_CACHE_PATH" "$ASCEND_PROCESS_LOG_PATH"
"""
    if code == "source":
        paths = [
            str(Path(settings["TS_SRC"]) / "python"),
            settings["PLUGIN_SRC"],
            str(Path(settings["UNI_SRC"]) / "src"),
        ]
        env += "export PYTHONPATH=" + q(":".join(paths)) + ':"${PYTHONPATH:-}"\n'
    (runtime / "env.sh").write_text(env)
    args = [
        "ts",
        "serve",
        "--headless",
        "--port",
        str(ports["ENGINE_PORT"]),
        "--data-parallel-address",
        prefill,
        "--data-parallel-rpc-port",
        str(rpc),
        "--zmq-engine-index",
        "0",
        "--metrics-port",
        str(ports["METRICS_PORT"]),
        "--dist-init-addr",
        f"{node if role != 'decode' else '127.0.0.1'}:{ports['DIST_PORT']}",
        "--trust-remote-code",
        "--served-model-name",
        "DeepSeek-V4.1-Flash",
        "--model-path",
        model,
        "--world-size",
        "8",
        "--tensor-parallel-size",
        "8",
        "--enable-expert-parallel",
        "--moe-backend",
        "auto",
        "--device",
        "npu",
        "--attention-backend",
        "deepseek_v41",
        "--enforce-eager",
        "--disable-prefill-graph",
        "--disable-pdl",
        "--max-model-len",
        launch_config["MAX_MODEL_LEN"],
        "--max-total-tokens",
        launch_config["MAX_TOTAL_TOKENS"],
        "--max-num-seqs",
        launch_config[role_prefix + "_MAX_NUM_SEQS"],
        "--chunked-prefill-size",
        launch_config["CHUNKED_PREFILL_SIZE"],
        "--disable-prefix-caching",
        "--disable-kvstore",
        "--language-model-only",
        "--engram-host-table",
        "--engram-host-table-layout",
        "sharded",
        "--numerics",
        "auto",
        "--sampling-backend",
        "greedy",
        "--gpu-memory-utilization",
        launch_config[role_prefix + "_MEM_FRACTION"],
        "--host",
        node,
    ]
    if mode == "pd":
        args += [
            "--disaggregation-mode",
            role,
            "--disaggregation-transfer-backend",
            "mooncake",
            "--disaggregation-layerwise-interval",
            "0",
        ]
        args += (
            ["--disaggregation-bootstrap-port", str(ports["BOOTSTRAP_PORT"])]
            if role == "prefill"
            else ["--retraction-snapshot-host-gb", "0.125"]
        )
    if code == "source":
        args += ["--allow-reload-forward"]
    gateway = [
        "python",
        "-m",
        "smg",
        "launch",
        "--backend",
        "tokenspeed",
        "--worker-wedge-secs",
        "120",
        "--host",
        prefill,
        "--port",
        str(ports["HTTP_PORT"]),
        "--prometheus-port",
        str(ports["GATEWAY_METRICS_PORT"]),
        "--model-path",
        model,
        "--tokenizer-path",
        model,
        "--reasoning-parser",
        "deepseek_v41",
        "--tool-call-parser",
        "deepseek_v41",
        "--disable-retries",
        "--disable-circuit-breaker",
        "--max-concurrent-requests",
        str(
            plan["worker_counts"].get("decode", plan["worker_counts"].get("regular", 1))
            * int(
                launch_config[
                    ("DECODE" if mode == "pd" else "PREFILL") + "_MAX_NUM_SEQS"
                ]
            )
        ),
        "--queue-size",
        "32",
        "--request-timeout-secs",
        "1800",
        "--queue-timeout-secs",
        "1800",
    ]
    if mode == "pd":
        gateway += ["--pd-disaggregation"]
        for worker in plan["instances"]:
            flag = "--prefill" if worker["role"] == "prefill" else "--decode"
            gateway += [
                flag,
                f"tcp://{prefill}:{worker['ports']['HANDSHAKE_PORT']}",
            ]
        gateway += [
            "--prefill-policy",
            "round_robin",
            "--decode-policy",
            "round_robin",
        ]
    else:
        gateway += ["--worker-urls"] + [
            f"tcp://{prefill}:{row['ports']['HANDSHAKE_PORT']}"
            for row in plan["instances"]
        ]
    if code == "source":
        gateway += ["--enable-rl"]
    for name, command in [("engine", args), ("gateway", gateway)]:
        (runtime / f"{name}.sh").write_text(
            "#!/usr/bin/env bash\nset -e\nsource "
            + q(str(runtime / "env.sh"))
            + "\nexec "
            + shlex.join(command)
            + "\n"
        )
    conf = f"""[unix_http_server]
file={root}/supervisor.sock
chmod=0600
[supervisord]
logfile={root}/logs/supervisord.log
pidfile={root}/supervisord.pid
childlogdir={root}/logs
user=root
[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface
[supervisorctl]
serverurl=unix://{root}/supervisor.sock
"""
    for name in ["engine"] + (["gateway"] if owns_gateway else []):
        conf += f"""[program:tokenspeed-{name}]
command=/bin/bash {runtime}/{name}.sh
directory={runtime}
autostart=true
autorestart=unexpected
startsecs=5
startretries=3
stopsignal=TERM
stopasgroup=true
killasgroup=true
stopwaitsecs=60
redirect_stderr=true
stdout_logfile={root}/logs/{name}.log
stdout_logfile_maxbytes=50MB
stdout_logfile_backups=3
"""
    (runtime / "supervisord.conf").write_text(conf)
    record = dict(
        node=node,
        prefill=prefill,
        decodes=decodes,
        role=role,
        mode=mode,
        code_mode=code,
        devices=settings["DEVICES"],
        model=model,
        gateway_host=prefill,
        http_port=ports["HTTP_PORT"],
        local_ports=local_ports,
        launch_config=launch_config,
        port_config={key: str(value) for key, value in base_ports.items()},
        pool_count=pool_count,
        pool_index=pool_index,
        owns_gateway=owns_gateway,
        worker_counts=plan["worker_counts"],
        instance_id=entry["id"],
        layout_fingerprint=plan["fingerprint"],
        gateway_instance=plan["gateway_instance"],
        deployment_layout=plan,
    )
    (runtime / "node.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def check_saved(settings):
    node = json.load(open(settings["PD_ROOT"] + "/runtime/node.json"))
    if (
        settings.get("LAYOUT_FINGERPRINT")
        and node.get("layout_fingerprint") != settings["LAYOUT_FINGERPRINT"]
    ):
        raise SystemExit(
            "deployment layout differs from saved config; select a new RUN_TAG/PD_ROOT"
        )
    for key, var in [
        ("mode", "SERVING_MODE"),
        ("code_mode", "CODE_MODE"),
        ("node", "NODE_IP"),
        ("devices", "DEVICES"),
        ("model", "MODEL_PATH"),
    ]:
        value = settings[var]
        if value and value != node[key]:
            raise SystemExit(
                f"{var} differs from saved config; select a new RUN_TAG/PD_ROOT"
            )
    checks = {**node.get("launch_config", {}), **node.get("port_config", {})}
    for key, value in checks.items():
        if key in settings and settings[key] != value:
            raise SystemExit(
                f"{key} differs from saved config; select a new RUN_TAG/PD_ROOT"
            )


def main():
    command = sys.argv[1]
    if command == "plan":
        print(json.dumps(normalize(os.environ), separators=(",", ":")))
    elif command == "render":
        configure(os.environ)
    elif command == "saved":
        check_saved(os.environ)
    elif command in ("rows", "summary"):
        plan = json.loads(Path(sys.argv[2]).read_text())
        if command == "summary":
            summarize(plan)
        else:
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
    else:
        raise SystemExit(f"unknown deployment command: {command}")


if __name__ == "__main__":
    main()
