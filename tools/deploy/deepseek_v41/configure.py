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
    node = str(ipaddress.IPv4Address(settings["NODE_IP"]))
    prefill = node if mode == "merged" else str(ipaddress.IPv4Address(settings["P_IP"]))
    decodes = (
        []
        if mode == "merged"
        else [str(ipaddress.IPv4Address(settings[k])) for k in ("D1_IP", "D2_IP")]
    )
    if mode == "pd" and (
        len(set([prefill, *decodes])) != 3 or node not in [prefill, *decodes]
    ):
        raise ValueError("PD requires three distinct node addresses")
    role = "regular" if mode == "merged" else "prefill" if node == prefill else "decode"
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
    rpc = ports["HANDSHAKE_PORT"] + (0 if role != "decode" else decodes.index(node) + 1)
    local_ports = [ports["ENGINE_PORT"], ports["METRICS_PORT"], ports["DIST_PORT"]]
    if role != "decode":
        local_ports += [
            ports["HTTP_PORT"],
            ports["GATEWAY_METRICS_PORT"],
            ports["HANDSHAKE_PORT"],
        ]
        if mode == "pd":
            local_ports += [
                ports["HANDSHAKE_PORT"] + 1,
                ports["HANDSHAKE_PORT"] + 2,
                ports["BOOTSTRAP_PORT"],
            ]
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
export NO_PROXY='*' no_proxy='*' MC_LOG_LEVEL=INFO
mkdir -p "$ASCEND_CACHE_PATH"
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
        "512",
        "--max-total-tokens",
        "1024",
        "--max-num-seqs",
        "1",
        "--chunked-prefill-size",
        "512",
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
        "0.85",
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
        "2",
        "--queue-size",
        "32",
        "--request-timeout-secs",
        "1800",
        "--queue-timeout-secs",
        "1800",
    ]
    if mode == "pd":
        gateway += [
            "--pd-disaggregation",
            "--prefill",
            f"tcp://{prefill}:{ports['HANDSHAKE_PORT']}",
            "--decode",
            f"tcp://{prefill}:{ports['HANDSHAKE_PORT'] + 1}",
            "--decode",
            f"tcp://{prefill}:{ports['HANDSHAKE_PORT'] + 2}",
            "--prefill-policy",
            "round_robin",
            "--decode-policy",
            "round_robin",
        ]
    else:
        gateway += ["--worker-urls", f"tcp://{prefill}:{rpc}"]
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
    for name in ["engine"] + (["gateway"] if role != "decode" else []):
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
    )
    (runtime / "node.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


if __name__ == "__main__":
    configure(os.environ)
