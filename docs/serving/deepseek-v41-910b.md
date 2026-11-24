# DeepSeek V4.1 Flash development on Ascend 910B

The [deployment entry point](../../tools/deploy/deepseek_v41/deploy_pd.sh) supports
both a merged prefill/decode engine (`SERVING_MODE=merged`) and three-node 1P2D
(`SERVING_MODE=pd`). Both use the normal headless engine and SMG serving path.
This is a TP8/EP8, eager, text-only development recipe, not a bare-machine installer
or a claim that disaggregation improves throughput.

## Prerequisites

Each node needs Linux x86_64, Python 3.11, eight free 64 GiB 910B devices, the
complete local model including Engram tables/tokenizer, and a validated Ascend
runtime. Set `BASE_ROOT` to that runtime, containing:

- `venv` with matching Torch/TorchNPU, Tokenspeed engine/kernel/scheduler, SMG,
  Supervisor, and the Ascend Mooncake package;
- `runtime/env.sh`, which activates that venv and sets CANN/driver/SHMEM library paths;
- `runtime/guard/sitecustomize.py`, rejecting legacy operator imports;
- the SHMEM libraries referenced by `env.sh` and a matching CANN installation.

The reference software combination is CANN 9.0, Torch 2.9.0, TorchNPU 2.9.0,
Tokenspeed 0.1.1, engine 0.1.0, scheduler 0.1.57, kernel-npu 0.1.0, SMG
1.16.0, and Mooncake NPU 0.3.13. The **exact** versions of the build environment
are recorded in `build-info.json` and checked during installation. A generic
CUDA Mooncake wheel is not a replacement. Native wheels must match Python,
architecture, Torch ABI and CANN.

Installation creates a new venv. A `.pth` file reads dependencies from the base
venv; locally installed wheels take precedence. Do not remove, move or upgrade
that base in place. This deliberately avoids copying a venv with stale shebangs.
A fully independent runtime export remains a separate packaging task.

## Build and publish wheels

Run on a matching Ascend build host. All dependency installation must stay in a
venv. Existing compiled dav-2201 operators can be reused for Python-only changes;
select `NATIVE_MODE=rebuild` to compile native operators in isolated staging.

```bash
source /path/to/validated-runtime/runtime/env.sh
export BASE_ROOT=/path/to/validated-runtime
export TS_SRC=/path/to/tokenspeed
export UNI_SRC=/path/to/uni-ops
export PLUGIN_SRC=/path/to/ts-plugin
export RELEASE_ID=deepseek-v41-dev
export BUILD_ROOT=/workdir/build-$RELEASE_ID
export RELEASE_ROOT=/workdir/releases/$RELEASE_ID
NATIVE_MODE=reuse bash "$TS_SRC/tools/deploy/deepseek_v41/deploy_pd.sh" build
bash "$TS_SRC/tools/deploy/deepseek_v41/deploy_pd.sh" publish
```

The source trees must have no tracked modifications. Build requires `build`,
`wheel` and setuptools >=77; rebuilding native operators also needs the CANN SDK,
CMake and compiler. The script does not download or upgrade these automatically.

The release contains `mt-uni-ops`, `tokenspeed-longcat` (the ts-plugin distribution)
and the validated Tokenspeed Python wheel, plus source commits, native-file hashes,
SHA256 manifests, scripts and regression tests. Dependency wheels get
`.post0+g<commit>` versions without modifying the checkout. Tokenspeed's cached
Python wheel is compared with the checkout before reuse; its native dependencies
remain supplied by the base runtime.

`publish` creates a new checksummed file release and refuses to overwrite an
existing directory. Copy the **whole** release to each node and check it there:

```bash
RELEASE_ROOT=/path/to/release bash tools/deploy/deepseek_v41/deploy_pd.sh check-release
```

Optionally set `TWINE_REPOSITORY_URL` during the first `publish` to upload only
the two dependency wheels to an explicitly configured package index. Credentials
must come from the environment or a credential provider. The index must support
PEP440 local versions. A failed upload leaves the file release intact; retry with
`python -m twine upload`, not by overwriting the release. No public package upload
is implied by file publication.

## One-command merged startup

Use a root shell on a prepared test node. Save these settings in a local file
outside Git, adjusting paths and selecting the node's real address:

```bash
export BASE_ROOT=/path/to/validated-runtime
export RELEASE_ROOT=/path/to/release
export MODEL_PATH=/models/DeepSeek-V4.1-Flash
export PD_ROOT=/workdir/deepseek-v41-merged-dev
export NODE_IP=192.0.2.10  # documentation address: replace with this node's IP
export SERVING_MODE=merged
export CODE_MODE=wheel
export DEVICES=0,1,2,3,4,5,6,7
```

Then run from the checkout:

```bash
bash tools/deploy/deepseek_v41/deploy_pd.sh up
```

`up` installs a fresh environment if needed, starts the engine and gateway, waits
for one regular worker ready/healthy, and checks a real `1+1` generation. It does
not return success merely because Supervisor says RUNNING. Installation is also
available separately with `install`. A readiness failure retains logs and leaves
the process available for diagnosis; it does not stop another deployment.

Default HTTP port is 8010. To coexist with another instance, use a new `PD_ROOT`,
free devices, and distinct `HTTP_PORT`, `ENGINE_PORT`, `METRICS_PORT`,
`GATEWAY_METRICS_PORT`, `HANDSHAKE_PORT` and `DIST_PORT` **before installation**.
Generated configuration is persistent: change it by stopping the old environment
and installing into a new directory. `up` does not rewrite existing settings.
Start refuses occupied devices or local TCP ports, including non-LISTEN connections.

## Source development and forward reload

For a development environment, explicitly set `CODE_MODE=source` and set `TS_SRC`,
`PLUGIN_SRC` and `UNI_SRC` to the local checkouts before `up`. The environment uses
the same wheels for packaging/entry points and prepends all three source trees to
`PYTHONPATH`. Wheel mode instead imports the installed packages. Source mode adds
`--allow-reload-forward` and SMG's `--enable-rl`; use it only on a development network.

```bash
source "$PD_ROOT/runtime/env.sh"
# Edit the model/forward code, then reload through the gateway.
ts reload-forward --url "http://$NODE_IP:${HTTP_PORT:-8010}"
bash "$TS_SRC/tools/deploy/deepseek_v41/deploy_pd.sh" smoke
```

See [forward reload](../guides/forward-reload.md) for the supported module scope.
Do not assume arbitrary plugin/native-library edits are hot-reloadable: verify
reload's module report, and rebuild/restart for changes outside its supported scope.
After iteration, validate on a freshly started instance. In PD source mode, keep
all three nodes' sources consistent and reload through the P gateway.

## Three-node 1P2D startup

Each role uses TP8/EP8, so this topology needs 24 devices. Start with merged mode
for low-load development; select PD to study phase interference and concurrency.
Evaluate it against equal-device merged replicas before choosing it for capacity.

Set the same paths, devices and ports on all three nodes; paths must exist locally.
Use explicit topology addresses (the following are documentation addresses):

```bash
export SERVING_MODE=pd CODE_MODE=wheel
export P_IP=192.0.2.10 D1_IP=192.0.2.11 D2_IP=192.0.2.12
export PD_ROOT=/workdir/deepseek-v41-pd-dev
export NODE_IP="$P_IP"  # choose D1_IP or D2_IP on the corresponding node
```

For a fresh deployment, a controller with key-based SSH access can start the
whole topology with one command:

```bash
export SSH_USER=root
# The checkout/script, release, base environment and model paths must exist
# at the same locations on every node. Override REMOTE_SCRIPT if necessary.
bash tools/deploy/deepseek_v41/deploy_pd.sh cluster-up
```

This installs all three nodes, starts P before D1/D2, and checks a real generation
through P. It uses BatchMode SSH and stops on the first error, retaining partial
installations and logs for diagnosis. It does not copy weights, checkouts or
releases, configure credentials, or stop existing services. Use a fresh PD_ROOT;
for an already installed cluster use the individual start commands below.

Alternatively, run on P first:

```bash
bash tools/deploy/deepseek_v41/deploy_pd.sh install
bash tools/deploy/deepseek_v41/deploy_pd.sh start
```

On D1 and D2, run `bash tools/deploy/deepseek_v41/deploy_pd.sh up`.
Then on P run `bash tools/deploy/deepseek_v41/deploy_pd.sh smoke` or `verify`.
P is started separately because waiting for all workers before starting D would
block a sequential deployment. No SSH passwords are embedded; orchestration,
model distribution and remote authentication remain the caller's responsibility.

P binds `HANDSHAKE_PORT` and its next two ports for P/D1/D2; **all three endpoints
belong to P**, not the decode hosts. Its routable `DIST_PORT` rendezvous publishes
the correct bootstrap address. Each D has a loopback rendezvous at its own
`DIST_PORT`, and sets snapshot host space to 0.125 GiB for this small-cache recipe.

Defaults: HTTP 8010, engine 8120, engine metrics 8121, gateway metrics 8122,
handshakes 9010–9012, rendezvous 9600 and P bootstrap 8998. Mooncake and ZMQ also
use data-plane connections; ensure node-to-node connectivity beyond the named
ports and keep the channels on a trusted network. See
[disaggregated serving](disaggregation.md) for handshake and transfer semantics.

## Validation and lifecycle

```bash
bash tools/deploy/deepseek_v41/deploy_pd.sh status
bash tools/deploy/deepseek_v41/deploy_pd.sh smoke
bash tools/deploy/deepseek_v41/deploy_pd.sh verify
bash tools/deploy/deepseek_v41/deploy_pd.sh evidence
bash tools/deploy/deepseek_v41/deploy_pd.sh stop
```

`smoke` checks one real generation. `verify` requires one regular worker in
merged mode or one prefill/two decode workers in PD, then sends six sequential
and two concurrent questions. It checks HTTP 200, expected text and `stop` finish
reasons and saves raw requests/responses under `validation/`. Workers must be
ready/healthy; these are functional checks, not sustained-throughput measurements.

For PD, also align the current requests' timestamps/IDs with both Decode logs,
`ts_decode_kv_transfer_success` on all eight ranks, `ts_request_finished`, and
`ascend_direct` initialization. `evidence` collects log events but does not
mistake historical successes for current acceptance. Verify from an external
client as well if the gateway is intended to be remote-accessible.

The recipe keeps context 512, engine concurrency 1, gateway concurrency 2,
`numerics=auto`, greedy sampling and Engram host tables. Larger contexts, sustained
load, other numerical envelopes and a different phase ratio need their own tests.
The arithmetic FP4 decode uses about 450 MiB more temporary tensor memory in the
measured MoE cases; check end-to-end memory headroom before increasing concurrency.

`stop` stops this deployment and shuts down its Supervisor. It never stops an old
instance or deletes weights/releases. To roll back, stop the new deployment,
confirm ports/devices are released, then start the retained old environment.
Never install candidate wheels back into the base venv as a rollback step.

To run launcher regression tests without NPU hardware:

```bash
source .venv/bin/activate
python -m pytest -q test/cli/test_deepseek_v41_deployment.py
```
