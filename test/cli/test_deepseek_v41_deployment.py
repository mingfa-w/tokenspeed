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

"""Exercise generated launchers without requiring NPU devices or weights."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "dsv41_configure", ROOT / "tools/deploy/deepseek_v41/configure.py"
)
CONFIG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CONFIG)


def settings(tmp_path, mode, code, node):
    base = tmp_path / "base"
    (base / "runtime").mkdir(parents=True)
    compiler = tmp_path / "cann/bin"
    compiler.mkdir(parents=True)
    (compiler / "bisheng").write_text("#!/usr/bin/env bash\nexit 0\n")
    (compiler / "bisheng").chmod(0o755)
    (base / "runtime/env.sh").write_text(f'export PATH="{compiler}:$PATH"\n')
    root = tmp_path / "runtime"
    (root / "venv/bin").mkdir(parents=True)
    # Simulate activate restoring its previous PATH, as a real nested venv does.
    (root / "venv/bin/activate").write_text('export PATH="$ORIGINAL_PATH"\n')
    return dict(
        SERVING_MODE=mode,
        CODE_MODE=code,
        PD_ROOT=str(root),
        NODE_IP=node,
        P_IP="192.0.2.1",
        D1_IP="192.0.2.2",
        D2_IP="192.0.2.3",
        BASE_ROOT=str(base),
        MODEL_PATH="/models/DeepSeek-V4.1-Flash",
        DEVICES="0,1,2,3,4,5,6,7",
        HTTP_PORT="8010",
        ENGINE_PORT="30120",
        METRICS_PORT="29120",
        GATEWAY_METRICS_PORT="29121",
        HANDSHAKE_PORT="9010",
        DIST_PORT="9600",
        BOOTSTRAP_PORT="8998",
        TS_SRC="/src/tokenspeed",
        PLUGIN_SRC="/src/ts-plugin",
        UNI_SRC="/src/uni-ops",
    )


def launch(tmp_path, runtime, name):
    # Replace only unavailable executables; run the generated shell and env for real.
    commands = tmp_path / "commands"
    commands.mkdir(exist_ok=True)
    output = tmp_path / "argv.json"
    recorder = "#!/usr/bin/env python3\nimport json,os,sys,shutil\nassert shutil.which('bisheng'), 'CANN PATH lost during nested activation'\nopen(os.environ['CAPTURE'],'w').write(json.dumps(sys.argv[1:]))\n"
    for executable in ["ts", "python"]:
        p = commands / executable
        p.write_text(recorder)
        p.chmod(0o755)
    subprocess.run(
        ["bash", str(runtime / f"{name}.sh")],
        check=True,
        env=dict(
            os.environ,
            PATH=f"{commands}:{os.environ['PATH']}",
            CAPTURE=str(output),
            ORIGINAL_PATH=f"{commands}:{os.environ['PATH']}",
        ),
    )
    return json.loads(output.read_text())


@pytest.mark.parametrize("code", ["wheel", "source"])
@pytest.mark.parametrize(
    "mode,node,role,rpc",
    [
        ("merged", "192.0.2.1", "regular", "9010"),
        ("pd", "192.0.2.1", "prefill", "9010"),
        ("pd", "192.0.2.2", "decode", "9011"),
        ("pd", "192.0.2.3", "decode", "9012"),
    ],
)
def test_launchers_route_to_the_selected_role(tmp_path, code, mode, node, role, rpc):
    env = settings(tmp_path, mode, code, node)
    record = CONFIG.configure(env)
    runtime = Path(env["PD_ROOT"]) / "runtime"
    args = launch(tmp_path, runtime, "engine")
    assert args[args.index("--model-path") + 1] == env["MODEL_PATH"]
    assert args[args.index("--data-parallel-rpc-port") + 1] == rpc
    assert args[args.index("--tensor-parallel-size") + 1] == "8"
    assert ("--allow-reload-forward" in args) == (code == "source")
    if mode == "merged":
        assert "--disaggregation-mode" not in args
    else:
        assert args[args.index("--disaggregation-mode") + 1] == role
        assert args[args.index("--data-parallel-address") + 1] == "192.0.2.1"
    gateway = launch(tmp_path, runtime, "gateway")
    assert ("--pd-disaggregation" in gateway) == (mode == "pd")
    assert ("--enable-rl" in gateway) == (code == "source")
    assert record["role"] == role
    assert record["http_port"] == 8010
    if mode == "pd":
        assert gateway[gateway.index("--prefill") + 1] == "tcp://192.0.2.1:9010"
        assert [gateway[i + 1] for i, a in enumerate(gateway) if a == "--decode"] == [
            "tcp://192.0.2.1:9011",
            "tcp://192.0.2.1:9012",
        ]
    else:
        assert gateway[gateway.index("--worker-urls") + 1] == "tcp://192.0.2.1:9010"


@pytest.mark.parametrize(
    "changes",
    [
        {"SERVING_MODE": ""},
        {"CODE_MODE": ""},
        {"DEVICES": "0,1,2,3"},
        {"D1_IP": "192.0.2.1"},
        {"HTTP_PORT": "9011"},
        {"HTTP_PORT": "65535"},
        {"NODE_IP": "192.0.2.99"},
    ],
)
def test_invalid_deployments_are_rejected_before_runtime_is_written(tmp_path, changes):
    env = settings(tmp_path, "pd", "wheel", "192.0.2.1")
    env.update(changes)
    with pytest.raises(ValueError):
        CONFIG.configure(env)
    assert not (Path(env["PD_ROOT"]) / "runtime/node.json").exists()


@pytest.mark.parametrize("fail_at", [0, 2])
def test_cluster_start_orders_roles_and_stops_on_remote_failure(tmp_path, fail_at):
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "ssh.jsonl"
    ssh = commands / "ssh"
    ssh.write_text(
        "#!/usr/bin/env python3\n"
        "import json,os,sys,pathlib\n"
        "p=pathlib.Path(os.environ['SSH_LOG'])\n"
        "calls=p.read_text().splitlines() if p.exists() else []\n"
        "with p.open('a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "sys.exit(17 if int(os.environ['FAIL_AT'])==len(calls)+1 else 0)\n"
    )
    ssh.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{commands}:{os.environ['PATH']}",
        SSH_LOG=str(log),
        FAIL_AT=str(fail_at),
        SERVING_MODE="pd",
        CODE_MODE="wheel",
        P_IP="192.0.2.1",
        D1_IP="192.0.2.2",
        D2_IP="192.0.2.3",
    )
    result = subprocess.run(
        ["bash", str(ROOT / "tools/deploy/deepseek_v41/deploy_pd.sh"), "cluster-up"],
        env=env,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    if fail_at:
        assert result.returncode == 17
        assert len(calls) == fail_at
    else:
        assert result.returncode == 0
        assert [c[2].split("@")[1] for c in calls] == [
            "192.0.2.1",
            "192.0.2.2",
            "192.0.2.3",
        ] * 2 + ["192.0.2.1"]
        assert all(c[-1].rstrip().endswith("install") for c in calls[:3])
        assert all(c[-1].rstrip().endswith("start") for c in calls[3:6])
        assert calls[-1][-1].rstrip().endswith("smoke")
