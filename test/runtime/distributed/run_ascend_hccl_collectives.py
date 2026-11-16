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

"""Run with torchrun --standalone --nproc-per-node=2 on two idle Ascend cards."""

import argparse
import os

import torch
import torch_npu  # noqa: F401

from tokenspeed.runtime.distributed import Mapping
from tokenspeed.runtime.distributed.comm_backend.hccl import HcclBackend
from tokenspeed.runtime.distributed.process_group_manager import process_group_manager


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-device", type=int, required=True)
    args = parser.parse_args()
    rank = int(os.environ["RANK"])
    world = int(os.environ["WORLD_SIZE"])
    assert world == 2
    device = torch.device(f"npu:{args.base_device+rank}")
    torch.npu.set_device(device)
    mapping = Mapping(rank=rank, world_size=world)
    process_group_manager.init_distributed(
        mapping, backend="hccl", timeout=120, device_id=device
    )
    group = tuple(range(world))
    process_group_manager.init_process_group(group, backend="hccl")
    backend = HcclBackend()
    local = torch.full((2, 3), rank + 1, dtype=torch.bfloat16, device=device)
    gathered = backend.all_gather(local, group, dim=1)
    expected = torch.tensor(
        [[1, 1, 1, 2, 2, 2], [1, 1, 1, 2, 2, 2]], dtype=torch.bfloat16
    )
    torch.testing.assert_close(gathered.cpu(), expected, rtol=0, atol=0)
    reduced = backend.reduce_scatter(
        torch.full((4, 3), rank + 1.0, device=device), group
    )
    torch.testing.assert_close(reduced.cpu(), torch.full((2, 3), 3.0), rtol=0, atol=0)
    keys = torch.tensor(
        [2**60 + rank, -(2**60) + rank], device=device, dtype=torch.int64
    )
    backend.all_reduce(keys, group, op=torch.distributed.ReduceOp.MAX)
    torch.testing.assert_close(
        keys.cpu(), torch.tensor([2**60 + 1, -(2**60) + 1]), rtol=0, atol=0
    )
    counts = [1, 2]
    tokens = torch.full((3, 2), rank + 1.0, device=device)
    actual = backend.token_reduce_scatter(tokens, group, counts)
    torch.testing.assert_close(
        actual.cpu(), torch.full((counts[rank], 2), 3.0), rtol=0, atol=0
    )
    torch.distributed.barrier()
    if rank == 0:
        print("PASS: gather, reduce-scatter, packed INT64 maximum, unequal token rows")
    torch.distributed.destroy_process_group()


if __name__ == "__main__":
    main()
