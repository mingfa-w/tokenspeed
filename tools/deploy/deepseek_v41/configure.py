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

"""Render a merged or 1P2D development deployment without starting it."""

import ipaddress
import json
import os
import shlex
from pathlib import Path


def configure(settings):
    """Write runtime files from explicit serving/code modes and node addresses."""
    mode, code = settings["SERVING_MODE"], settings["CODE_MODE"]
    if mode not in {"merged", "pd"} or code not in {"wheel", "source"}:
        raise ValueError("select SERVING_MODE=merged|pd and CODE_MODE=wheel|source")
    root = Path(settings["PD_ROOT"])
    if not root.is_absolute() or any(c.isspace() for c in str(root)):
        raise ValueError("PD_ROOT must be absolute and contain no whitespace")
    flexible = bool(settings.get("LAYOUT_JSON"))
    if flexible:
        from pd_layout import normalize

        plan = normalize(settings, json.loads(settings["LAYOUT_JSON"]))
        matches = [
            entry
            for entry in plan["instances"]
            if entry["id"] == settings["INSTANCE_ID"]
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
    else:
        node = str(ipaddress.IPv4Address(settings["NODE_IP"]))
        prefill = (
            node if mode == "merged" else str(ipaddress.IPv4Address(settings["P_IP"]))
        )
        decodes = (
            []
            if mode == "merged"
            else [str(ipaddress.IPv4Address(settings[k])) for k in ("D1_IP", "D2_IP")]
        )
        if mode == "pd" and (
            len(set([prefill, *decodes])) != 3 or node not in [prefill, *decodes]
        ):
            raise ValueError("PD requires three distinct node addresses")
        role = (
            "regular"
            if mode == "merged"
            else "prefill" if node == prefill else "decode"
        )
        names = [
            "HTTP_PORT",
            "ENGINE_PORT",
            "METRICS_PORT",
            "GATEWAY_METRICS_PORT",
            "HANDSHAKE_PORT",
            "DIST_PORT",
            "BOOTSTRAP_PORT",
        ]
        ports = {key: int(settings[key]) for key in names}
        if any(not 1024 <= value <= 65533 for value in ports.values()):
            raise ValueError(
                "ports must be in [1024,65533]; handshake reserves three ports"
            )
        pool_count = int(settings.get("POOL_COUNT", "1"))
        pool_index = int(settings.get("POOL_INDEX", "0"))
        stride = int(settings.get("POOL_PORT_STRIDE", "100"))
        if pool_count not in (1, 2) or not 0 <= pool_index < pool_count or stride < 10:
            raise ValueError("invalid device-pool configuration")
        base_ports = ports.copy()
        ports = {
            key: value
            + (
                0
                if key in ("HTTP_PORT", "GATEWAY_METRICS_PORT")
                else pool_index * stride
            )
            for key, value in base_ports.items()
        }
        if (
            max(
                value + (pool_count - 1 - pool_index) * stride
                for key, value in ports.items()
                if key not in ("HTTP_PORT", "GATEWAY_METRICS_PORT")
            )
            > 65533
        ):
            raise ValueError("device-pool port overflow")
        owns_gateway = role != "decode" and pool_index == 0
        rpc = ports["HANDSHAKE_PORT"] + (
            0 if role != "decode" else decodes.index(node) + 1
        )
        local_ports = [ports["ENGINE_PORT"], ports["METRICS_PORT"], ports["DIST_PORT"]]
        if mode == "pd" and role == "prefill":
            local_ports += [ports["BOOTSTRAP_PORT"]]
        if owns_gateway:
            local_ports += [ports["HTTP_PORT"], ports["GATEWAY_METRICS_PORT"]]
            for pool in range(pool_count):
                start = base_ports["HANDSHAKE_PORT"] + pool * stride
                local_ports += (
                    [start, start + 1, start + 2] if mode == "pd" else [start]
                )
        # The engine derives its control-plane sockets from the rendezvous base.
        local_ports += [ports["DIST_PORT"] + offset for offset in (1, 3, 4, 5, 6)]
        if len(local_ports) != len(set(local_ports)):
            raise ValueError("local service ports must be distinct")
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
            (
                plan["worker_counts"].get(
                    "decode", plan["worker_counts"].get("regular", 1)
                )
                * int(
                    launch_config[
                        ("DECODE" if mode == "pd" else "PREFILL") + "_MAX_NUM_SEQS"
                    ]
                )
            )
            if flexible
            else 2 * pool_count
        ),
        "--queue-size",
        "32",
        "--request-timeout-secs",
        "1800",
        "--queue-timeout-secs",
        "1800",
    ]
    if flexible:
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
    else:
        if mode == "pd":
            gateway += ["--pd-disaggregation"]
            for pool in range(pool_count):
                start = base_ports["HANDSHAKE_PORT"] + pool * stride
                gateway += [
                    "--prefill",
                    f"tcp://{prefill}:{start}",
                    "--decode",
                    f"tcp://{prefill}:{start + 1}",
                    "--decode",
                    f"tcp://{prefill}:{start + 2}",
                ]
            gateway += [
                "--prefill-policy",
                "round_robin",
                "--decode-policy",
                "round_robin",
            ]
        else:
            gateway += ["--worker-urls"] + [
                f"tcp://{prefill}:{base_ports['HANDSHAKE_PORT'] + pool * stride}"
                for pool in range(pool_count)
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
        worker_counts=(
            plan["worker_counts"]
            if flexible
            else (
                {"prefill": pool_count, "decode": 2 * pool_count}
                if mode == "pd"
                else {"regular": pool_count}
            )
        ),
    )
    if flexible:
        record.update(
            instance_id=entry["id"],
            layout_fingerprint=plan["fingerprint"],
            gateway_instance=plan["gateway_instance"],
            deployment_layout=plan,
        )
    (runtime / "node.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


if __name__ == "__main__":
    configure(os.environ)
