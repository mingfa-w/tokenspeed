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

"""V4.1 metadata must retain NPU placement and logical address semantics."""

import pytest
import torch

pytest.importorskip("torch_npu")
from tokenspeed_kernel.ops.attention import dsv41

pytestmark = pytest.mark.skipif(not torch.npu.is_available(), reason="requires Ascend")


def test_decode_rows_padding_and_width():
    outputs = [torch.empty(n, dtype=torch.int64, device="npu") for n in (6, 6, 3, 3)]
    dsv41.decode_rows(
        torch.tensor([3, 70], device="npu"),
        torch.tensor([4, 9], device="npu"),
        *outputs,
        2,
        0,
        2
    )
    expected = ([1, 2, 68, 69, -1, -1], [0, 0, 1, 1, -1, -1], [3, 70, 0], [4, 9, -1])
    for actual, reference in zip(outputs, expected, strict=True):
        torch.testing.assert_close(
            actual.cpu(), torch.tensor(reference), rtol=0, atol=0
        )


def test_sliding_window_addresses():
    positions = torch.tensor([-1, 0, 63, 64, 129], device="npu")
    requests = torch.tensor([-1, 0, 0, 0, 1], device="npu")
    table = torch.tensor([[1, 2], [3, 4]], device="npu", dtype=torch.int32)
    writes = torch.empty_like(positions)
    reads = torch.empty((5, 128), device="npu", dtype=torch.int64)
    lengths = torch.empty_like(positions)
    dsv41.decode_window(positions, requests, writes, reads, lengths, table, 5)

    def address(position, request):
        if position < 0 or request < 0 or position // 64 >= 2:
            return -1
        return int(table.cpu()[request, position // 64]) * 64 + position % 64

    expected = []
    for position, request in zip(
        positions.cpu().tolist(), requests.cpu().tolist(), strict=True
    ):
        begin = max(position - 127, 0)
        expected.append(
            [
                address(token, request) if token <= position else -1
                for token in range(begin, begin + 128)
            ]
        )
    torch.testing.assert_close(reads.cpu(), torch.tensor(expected), rtol=0, atol=0)
    torch.testing.assert_close(
        writes.cpu(), torch.tensor([-1, 64, 127, 128, -1]), rtol=0, atol=0
    )
    torch.testing.assert_close(
        lengths.cpu(), torch.tensor([0, 1, 64, 65, 128]), rtol=0, atol=0
    )


def test_selection_table_invalid_requests():
    table, visible = dsv41.selection_table(
        torch.tensor([3, 63, 65, -1], device="npu"),
        torch.tensor([0, 1, 7, -1], device="npu"),
        torch.tensor([[1, 0], [2, 3]], device="npu", dtype=torch.int32),
        4,
    )
    torch.testing.assert_close(
        table.cpu(),
        torch.tensor([[1, -1], [2, 3], [-1, -1], [-1, -1]], dtype=torch.int32),
        rtol=0,
        atol=0,
    )
    torch.testing.assert_close(
        visible.cpu(), torch.tensor([1, 16, 16, 0], dtype=torch.int32), rtol=0, atol=0
    )


def test_compressor_pairs_and_tail_addresses():
    positions = torch.tensor([0, 1, 2, 3, 0, 1, -1], device="npu")
    requests = torch.tensor([0, 0, 0, 0, 1, 1, -1], device="npu")
    active = torch.empty(7, device="npu", dtype=torch.bool)
    outputs = [torch.empty_like(positions) for _ in range(5)]
    dsv41.compressor_metadata(
        positions,
        requests,
        torch.tensor([[1, 2], [3, 4]], device="npu", dtype=torch.int32),
        5,
        active,
        *outputs
    )
    torch.testing.assert_close(
        active.cpu(), torch.tensor([False, True, False, True, False, True, False])
    )
    expected = (
        [-1, 0, -1, 2, -1, 0, -1],
        [-1, 0, -1, 0, -1, 1, -1],
        [-1, 0, -1, 2, -1, 4, -1],
        [-1, 2, -1, 4, -1, 6, -1],
        [2, 3, 4, 5, 6, 7, -1],
    )
    for actual, reference in zip(outputs, expected, strict=True):
        torch.testing.assert_close(
            actual.cpu(), torch.tensor(reference), rtol=0, atol=0
        )


def test_all_history_selection_skips_missing_pages():
    out = (
        torch.empty((3, 128), device="npu", dtype=torch.int32),
        torch.empty(3, device="npu", dtype=torch.int32),
        torch.empty((3, 16), device="npu", dtype=torch.int32),
        torch.empty(3, device="npu", dtype=torch.int32),
    )
    dsv41.index_select_all(
        torch.tensor([[1, 2], [3, 0], [0, 0]], device="npu", dtype=torch.int32),
        torch.tensor([70, 67, 0], device="npu"),
        4,
        8,
        out,
    )
    rows, lengths, blocks, block_lengths = (x.cpu() for x in out)
    assert lengths.tolist() == [70, 64, 0]
    assert block_lengths.tolist() == [9, 8, 0]
    for row, count in enumerate((70, 64, 0)):
        torch.testing.assert_close(
            rows[row, :count], torch.arange(count, dtype=torch.int32), rtol=0, atol=0
        )
        assert bool((rows[row, count:] == -1).all())
    for row, count in enumerate((9, 8, 0)):
        torch.testing.assert_close(
            blocks[row, :count], torch.arange(count, dtype=torch.int32), rtol=0, atol=0
        )
        assert bool((blocks[row, count:] == -1).all())
