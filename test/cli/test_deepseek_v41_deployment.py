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


@pytest.mark.parametrize(
    "node,prefix", [("192.0.2.1", "PREFILL"), ("192.0.2.2", "DECODE")]
)
def test_role_specific_limits_reach_the_engine(tmp_path, node, prefix):
    env = settings(tmp_path, "pd", "wheel", node)
    env.update(
        MAX_MODEL_LEN="256",
        MAX_TOTAL_TOKENS="512",
        CHUNKED_PREFILL_SIZE="128",
        PREFILL_MEM_FRACTION="0.6",
        DECODE_MEM_FRACTION="0.7",
        PREFILL_MAX_NUM_SEQS="2",
        DECODE_MAX_NUM_SEQS="3",
    )
    CONFIG.configure(env)
    args = launch(tmp_path, Path(env["PD_ROOT"]) / "runtime", "engine")
    for flag, value in [
        ("--max-model-len", "256"),
        ("--max-total-tokens", "512"),
        ("--chunked-prefill-size", "128"),
        ("--gpu-memory-utilization", env[prefix + "_MEM_FRACTION"]),
        ("--max-num-seqs", env[prefix + "_MAX_NUM_SEQS"]),
    ]:
        assert args[args.index(flag) + 1] == value


def front_run(
    tmp_path,
    action,
    mode="pd",
    fail_task="",
    pools="0,1,2,3,4,5,6,7",
    layout=None,
    extra_env=None,
):
    commands = tmp_path / "network"
    commands.mkdir()
    log = tmp_path / "remote.jsonl"
    script = (
        "#!/usr/bin/env python3\n"
        "import json,os,sys,shlex,pathlib\n"
        "kind=pathlib.Path(sys.argv[0]).name\n"
        "args=sys.argv[1:]\n"
        "tokens=shlex.split(args[-1]) if kind=='ssh' else []\n"
        "target=next((x for x in args if x.startswith('root@')), '')\n"
        "with open(os.environ['REMOTE_LOG'],'a') as f:f.write(json.dumps({'kind':kind,'target':target,'tokens':tokens})+'\\n')\n"
        "sys.exit(17 if tokens and tokens[-1]==os.environ.get('FAIL_TASK') else 0)\n"
    )
    for name in ["ssh", "scp"]:
        exe = commands / name
        exe.write_text(script)
        exe.chmod(0o755)
    password_runner = commands / "sshpass"
    password_runner.write_text(
        "#!/usr/bin/env python3\nimport os,sys\n"
        "assert sys.argv[1]=='-f'\n"
        "assert open(sys.argv[2]).read().strip()=='test-password'\n"
        "os.execvp(sys.argv[3], sys.argv[3:])\n"
    )
    password_runner.chmod(0o755)
    ips = "192.0.2.1\n192.0.2.2\n192.0.2.3" if mode == "pd" else "192.0.2.1"
    env = dict(
        os.environ,
        PATH=f"{commands}:{os.environ['PATH']}",
        REMOTE_LOG=str(log),
        FAIL_TASK=fail_task,
        SERVING_MODE=mode,
        CODE_MODE="wheel",
        DS_IPLIST=ips,
        RUN_TAG="case_a",
        TEST_ROOT=str(tmp_path / "runs"),
        LOCAL_LOG_ROOT=str(tmp_path / "logs"),
    )
    for key in [
        "PD_LAYOUT",
        "PD_LAYOUT_FILE",
        "GATEWAY_INSTANCE",
        "REQUIRE_FULL_NODE",
        "NPU_DEVICES_PER_HOST",
    ]:
        env.pop(key, None)
    if pools is not None:
        env["DEVICE_POOLS"] = pools
    if layout is not None:
        env.pop("DS_IPLIST", None)
        env.pop("DEVICE_POOLS", None)
        env["PD_LAYOUT"] = json.dumps(layout)
        env["REQUIRE_FULL_NODE"] = "1"
    env.update(extra_env or {})
    result = subprocess.run(
        ["bash", str(ROOT / "tools/deploy/deepseek_v41/deploy_pd.sh"), action],
        env=env,
        capture_output=True,
        text=True,
    )
    calls = (
        [json.loads(line) for line in log.read_text().splitlines()]
        if log.exists()
        else []
    )
    return result, calls


@pytest.mark.parametrize("mode", ["merged", "pd"])
def test_front_start_prepares_then_starts_and_verifies(tmp_path, mode):
    result, calls = front_run(tmp_path, "start", mode)
    assert result.returncode == 0, result.stderr
    operations = [
        (c["target"], c["tokens"][-1])
        for c in calls
        if c["kind"] == "ssh" and c["tokens"][-1] in {"prepare", "start", "smoke"}
    ]
    targets = ["root@192.0.2.1"] + (
        ["root@192.0.2.2", "root@192.0.2.3"] if mode == "pd" else []
    )
    assert operations == [(n, "prepare") for n in targets] + [
        (n, "start") for n in targets
    ] + [(targets[0], "smoke")]
    assert any(c["kind"] == "scp" for c in calls)


@pytest.mark.parametrize(
    "action,targets",
    [
        ("startp", ["root@192.0.2.1"]),
        ("startd", ["root@192.0.2.2", "root@192.0.2.3"]),
        ("stopp", ["root@192.0.2.1"]),
        ("stopd", ["root@192.0.2.2", "root@192.0.2.3"]),
    ],
)
def test_front_role_actions_only_touch_selected_nodes(tmp_path, action, targets):
    result, calls = front_run(tmp_path, action)
    assert result.returncode == 0, result.stderr
    task = "start" if action.startswith("start") else "stop"
    assert [
        c["target"] for c in calls if c["kind"] == "ssh" and c["tokens"][-1] == task
    ] == (targets if task == "start" else list(reversed(targets)))
    assert all(c["target"].split(":")[0] in targets for c in calls)


def test_front_aborts_start_after_prepare_failure(tmp_path):
    result, calls = front_run(tmp_path, "start", fail_task="prepare")
    assert result.returncode == 17
    assert not any(c["tokens"] and c["tokens"][-1] == "start" for c in calls)


def test_front_plan_is_offline(tmp_path):
    result, calls = front_run(tmp_path, "plan")
    assert result.returncode == 0, result.stderr
    assert not calls
    assert "role=prefill" in result.stdout and result.stdout.count("role=decode") == 2
    assert "run_case_a" in result.stdout


@pytest.mark.parametrize(
    "index,node,role",
    [
        (0, "192.0.2.1", "prefill"),
        (1, "192.0.2.1", "prefill"),
        (0, "192.0.2.2", "decode"),
        (1, "192.0.2.3", "decode"),
    ],
)
def test_full_node_pools_share_one_2p4d_gateway(tmp_path, index, node, role):
    env = settings(tmp_path, "pd", "wheel", node)
    env.update(
        POOL_COUNT="2",
        POOL_INDEX=str(index),
        POOL_PORT_STRIDE="100",
        DEVICES="0,1,2,3,4,5,6,7" if index == 0 else "8,9,10,11,12,13,14,15",
    )
    record = CONFIG.configure(env)
    runtime = Path(env["PD_ROOT"]) / "runtime"
    args = launch(tmp_path, runtime, "engine")
    expected_rpc = (
        9010
        + index * 100
        + (0 if role == "prefill" else (1 if node == "192.0.2.2" else 2))
    )
    assert args[args.index("--data-parallel-rpc-port") + 1] == str(expected_rpc)
    assert record["worker_counts"] == {"prefill": 2, "decode": 4}
    assert record["owns_gateway"] == (role == "prefill" and index == 0)
    conf = (runtime / "supervisord.conf").read_text()
    assert ("[program:tokenspeed-gateway]" in conf) == record["owns_gateway"]
    gateway = launch(tmp_path, runtime, "gateway")
    assert [gateway[i + 1] for i, x in enumerate(gateway) if x == "--prefill"] == [
        "tcp://192.0.2.1:9010",
        "tcp://192.0.2.1:9110",
    ]
    assert [gateway[i + 1] for i, x in enumerate(gateway) if x == "--decode"] == [
        "tcp://192.0.2.1:9011",
        "tcp://192.0.2.1:9012",
        "tcp://192.0.2.1:9111",
        "tcp://192.0.2.1:9112",
    ]


def test_default_front_uses_all_16_devices_and_starts_both_pools(tmp_path):
    result, calls = front_run(tmp_path, "start", pools=None)
    assert result.returncode == 0, result.stderr
    starts = [c for c in calls if c["kind"] == "ssh" and c["tokens"][-1] == "start"]
    assert len(starts) == 6
    assert sum("POOL_INDEX=0" in c["tokens"] for c in starts) == 3
    assert sum("POOL_INDEX=1" in c["tokens"] for c in starts) == 3
    assert all(
        "DEVICES=0,1,2,3,4,5,6,7" in c["tokens"]
        for c in starts
        if "POOL_INDEX=0" in c["tokens"]
    )
    assert all(
        "DEVICES=8,9,10,11,12,13,14,15" in c["tokens"]
        for c in starts
        if "POOL_INDEX=1" in c["tokens"]
    )
    smoke = [c for c in calls if c["kind"] == "ssh" and c["tokens"][-1] == "smoke"]
    assert len(smoke) == 1 and "POOL_INDEX=0" in smoke[0]["tokens"]


def test_default_plan_assigns_all_six_instances(tmp_path):
    result, calls = front_run(tmp_path, "plan", pools=None)
    assert result.returncode == 0, result.stderr
    assert not calls
    assert result.stdout.count("role=prefill") == 2
    assert result.stdout.count("role=decode") == 4
    assert result.stdout.count("devices=8,9,10,11,12,13,14,15") == 3


LAYOUT_SPEC = importlib.util.spec_from_file_location(
    "dsv41_layout", ROOT / "tools/deploy/deepseek_v41/pd_layout.py"
)
LAYOUT = importlib.util.module_from_spec(LAYOUT_SPEC)
LAYOUT_SPEC.loader.exec_module(LAYOUT)


def placement(roles):
    counts = {"p": 0, "d": 0, "m": 0}
    entries = []
    for index, role in enumerate(roles):
        entries.append(
            dict(
                id=f"{role}{counts[role]}",
                role=role,
                node=f"192.0.2.{index//2+1}",
                devices=list(range((index % 2) * 8, (index % 2 + 1) * 8)),
            )
        )
        counts[role] += 1
    return entries


@pytest.mark.parametrize("roles", ["pddddd", "ppdddd", "pdpdpd", "mmdddd"])
def test_layout_rejects_mode_mismatch_or_counts_all_devices(roles):
    if "m" in roles:
        with pytest.raises(ValueError):
            LAYOUT.normalize(
                dict(SERVING_MODE="pd", PD_LAYOUT=json.dumps(placement(roles)))
            )
        return
    plan = LAYOUT.normalize(
        dict(SERVING_MODE="pd", PD_LAYOUT=json.dumps(placement(roles)))
    )
    assert plan["worker_counts"] == {
        "prefill": roles.count("p"),
        "decode": roles.count("d"),
    }
    assert sum(len(x["devices"]) for x in plan["instances"]) == 48
    for host in ["192.0.2.1", "192.0.2.2", "192.0.2.3"]:
        assert {
            d for x in plan["instances"] if x["node"] == host for d in x["devices"]
        } == set(range(16))
    assert (
        LAYOUT.normalize(dict(SERVING_MODE="pd"), plan)["fingerprint"]
        == plan["fingerprint"]
    )


@pytest.mark.parametrize("roles", ["pddddd", "ppdddd", "pdpdpd"])
def test_flexible_front_starts_gateway_then_prefill_then_decode(tmp_path, roles):
    rows = placement(roles)
    result, calls = front_run(tmp_path, "start", layout=rows)
    assert result.returncode == 0, result.stderr
    starts = [c for c in calls if c["kind"] == "ssh" and c["tokens"][-1] == "start"]
    got = [
        next(t.split("=", 1)[1] for t in c["tokens"] if t.startswith("INSTANCE_ID="))
        for c in starts
    ]
    assert got == [x["id"] for x in rows if x["role"] == "p"] + [
        x["id"] for x in rows if x["role"] == "d"
    ]
    for call in starts:
        plan = json.loads(
            next(
                t.split("=", 1)[1]
                for t in call["tokens"]
                if t.startswith("LAYOUT_JSON=")
            )
        )
        assert plan["worker_counts"]["prefill"] == roles.count("p")
        assert plan["worker_counts"]["decode"] == roles.count("d")


@pytest.mark.parametrize(
    "action,role", [("startp", "p"), ("startd", "d"), ("stopp", "p"), ("stopd", "d")]
)
def test_mixed_host_role_actions_do_not_operate_other_role(tmp_path, action, role):
    rows = placement("pdpdpd")
    result, calls = front_run(tmp_path, action, layout=rows)
    assert result.returncode == 0, result.stderr
    task = "start" if action.startswith("start") else "stop"
    operations = [c for c in calls if c["kind"] == "ssh" and c["tokens"][-1] == task]
    ids = [
        next(t.split("=", 1)[1] for t in c["tokens"] if t.startswith("INSTANCE_ID="))
        for c in operations
    ]
    expected = [x["id"] for x in rows if x["role"] == role]
    assert ids == (expected if task == "start" else list(reversed(expected)))


@pytest.mark.parametrize(
    "roles,index,owner",
    [
        ("pddddd", 0, "p0"),
        ("pddddd", 1, "p0"),
        ("pdpdpd", 2, "p0"),
        ("ppdddd", 3, "d1"),
    ],
)
def test_flexible_renderer_registers_entire_layout_with_one_gateway(
    tmp_path, roles, index, owner
):
    rows = placement(roles)
    row = rows[index]
    env = settings(tmp_path, "pd", "wheel", row["node"])
    env.update(
        INSTANCE_ID=row["id"],
        DEVICES=",".join(map(str, row["devices"])),
        LAYOUT_JSON=json.dumps(
            dict(instances=rows, gateway_instance=owner, require_full_node=True)
        ),
    )
    # Run the real script so its sibling-module imports match deployment.
    subprocess.run(
        ["python", str(ROOT / "tools/deploy/deepseek_v41/configure.py")],
        env=dict(os.environ, **env),
        check=True,
    )
    runtime = Path(env["PD_ROOT"]) / "runtime"
    record = json.loads((runtime / "node.json").read_text())
    assert record["role"] == {"p": "prefill", "d": "decode"}[row["role"]]
    assert record["owns_gateway"] == (row["id"] == owner)
    assert (
        "[program:tokenspeed-gateway]" in (runtime / "supervisord.conf").read_text()
    ) == (row["id"] == owner)
    args = launch(tmp_path, runtime, "gateway")
    assert args.count("--prefill") == roles.count("p")
    assert args.count("--decode") == roles.count("d")
    owner_host = next(x["node"] for x in rows if x["id"] == owner)
    assert args[args.index("--host") + 1] == owner_host
    engine = launch(tmp_path, runtime, "engine")
    assert engine[engine.index("--data-parallel-address") + 1] == owner_host
    assert engine[engine.index("--data-parallel-rpc-port") + 1] == str(9010 + index)
    assert record["worker_counts"] == {
        "prefill": roles.count("p"),
        "decode": roles.count("d"),
    }


@pytest.mark.parametrize(
    "problem",
    [
        "overlap",
        "duplicate_id",
        "missing_decode",
        "incomplete",
        "bad_group",
        "bad_port",
    ],
)
def test_invalid_layout_is_rejected(problem):
    rows = placement("pdpdpd")
    env = dict(SERVING_MODE="pd")
    if problem == "overlap":
        rows[1]["devices"] = rows[0]["devices"]
    if problem == "duplicate_id":
        rows[1]["id"] = rows[0]["id"]
    if problem == "missing_decode":
        for x in rows:
            x["role"] = "p"
    if problem == "incomplete":
        rows = rows[:-1]
    if problem == "bad_group":
        rows[0]["devices"] = [0, 1, 2, 3, 8, 9, 10, 11]
    if problem == "bad_port":
        env["HTTP_PORT"] = "8120"
    env["PD_LAYOUT"] = json.dumps(rows)
    with pytest.raises(ValueError):
        LAYOUT.normalize(env)


def test_layout_file_and_partial_allocation_are_explicit(tmp_path):
    p = tmp_path / "small.layout"
    p.write_text(
        "p0 prefill 192.0.2.1 0,1,2,3,4,5,6,7\nd0 decode 192.0.2.2 0,1,2,3,4,5,6,7\n"
    )
    env = dict(SERVING_MODE="pd", PD_LAYOUT_FILE=str(p))
    with pytest.raises(ValueError):
        LAYOUT.normalize(env)
    env["REQUIRE_FULL_NODE"] = "0"
    assert LAYOUT.normalize(env)["worker_counts"] == {"prefill": 1, "decode": 1}
    env["PD_LAYOUT"] = "p0 p 192.0.2.1 0,1,2,3,4,5,6,7"
    with pytest.raises(ValueError):
        LAYOUT.normalize(env)


@pytest.mark.parametrize("changed", [False, True])
def test_prepare_refuses_changed_layout_before_start(tmp_path, changed):
    rows = placement("ppdddd")
    env = settings(tmp_path, "pd", "wheel", "192.0.2.1")
    plan = LAYOUT.normalize(env, dict(instances=rows, require_full_node=True))
    env.update(
        INSTANCE_ID="p0", DEVICES="0,1,2,3,4,5,6,7", LAYOUT_JSON=json.dumps(plan)
    )
    subprocess.run(
        ["python", str(ROOT / "tools/deploy/deepseek_v41/configure.py")],
        env=dict(os.environ, **env),
        check=True,
    )
    runtime = Path(env["PD_ROOT"]) / "runtime"
    (runtime / "install-complete").touch()
    if changed:
        plan = LAYOUT.normalize(
            env, dict(instances=placement("pdpdpd"), require_full_node=True)
        )
    env.update(LAYOUT_JSON=json.dumps(plan), LAYOUT_FINGERPRINT=plan["fingerprint"])
    result = subprocess.run(
        ["bash", str(ROOT / "tools/deploy/deepseek_v41/deploy_runtime.sh"), "prepare"],
        env=dict(os.environ, **env),
        capture_output=True,
        text=True,
    )
    assert result.returncode == (1 if changed else 0), result.stderr
    if changed:
        assert "layout differs" in result.stderr


def test_flexible_merged_layout_and_json_schema_validation():
    rows = placement("mmmmmm")
    env = dict(SERVING_MODE="merged", PD_LAYOUT=json.dumps(rows))
    plan = LAYOUT.normalize(env)
    assert plan["worker_counts"] == {"regular": 6}
    assert plan["gateway_instance"] == "m0"
    for malformed in [{}, {"instances": "bad"}, {"instances": [{"id": "x"}]}]:
        with pytest.raises(ValueError):
            LAYOUT.normalize(dict(SERVING_MODE="pd"), malformed)


@pytest.mark.parametrize("action", ["preflight", "shell", "start"])
def test_password_auth_coordinates_all_configured_hosts(tmp_path, action):
    password = tmp_path / "password with spaces.txt"
    password.write_text("test-password\n")
    result, calls = front_run(
        tmp_path,
        action,
        extra_env=dict(SSH_AUTH_MODE="password", SSH_PASSWORD_FILE=str(password)),
    )
    assert result.returncode == 0, result.stderr
    assert {c["target"].split(":")[0] for c in calls} == {
        "root@192.0.2.1",
        "root@192.0.2.2",
        "root@192.0.2.3",
    }
    assert "test-password" not in result.stdout + result.stderr
    if action == "preflight":
        assert not any(c["kind"] == "scp" for c in calls)
    else:
        writes = [
            c for c in calls if c["tokens"] and c["tokens"][-1].endswith("/abc.txt")
        ]
        assert len(writes) == 3


def test_missing_password_aborts_before_remote_operations(tmp_path):
    result, calls = front_run(
        tmp_path,
        "start",
        extra_env=dict(
            SSH_AUTH_MODE="password", SSH_PASSWORD_FILE=str(tmp_path / "absent")
        ),
    )
    assert result.returncode != 0
    assert not calls


def test_key_mode_ignores_password_file(tmp_path):
    result, calls = front_run(
        tmp_path,
        "shell",
        extra_env=dict(SSH_AUTH_MODE="key", SSH_PASSWORD_FILE=str(tmp_path / "absent")),
    )
    assert result.returncode == 0, result.stderr
    assert not any(c["tokens"] and c["tokens"][-1].endswith("/abc.txt") for c in calls)
