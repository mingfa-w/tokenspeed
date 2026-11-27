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

"""Platform execution interface for V4.1 projection and Engram pipelines."""

from typing import Protocol

import torch
from torch import nn


class V41ExecutionOps(Protocol):
    def attn_prologue(
        self, attention: nn.Module, x: torch.Tensor, positions: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]: ...
    def attn_epilogue(
        self,
        attention: nn.Module,
        output: torch.Tensor,
        positions: torch.Tensor,
        target: torch.Tensor | None,
    ) -> torch.Tensor: ...
    def indexer_qw(
        self,
        indexer: nn.Module,
        x: torch.Tensor,
        qr: torch.Tensor,
        qr_scale: torch.Tensor | None,
        positions: torch.Tensor,
        rotary: nn.Module,
    ) -> tuple[torch.Tensor, torch.Tensor]: ...
    def indexer_k(
        self,
        indexer: nn.Module,
        latent: torch.Tensor,
        positions: torch.Tensor,
        rotary: nn.Module,
    ) -> torch.Tensor: ...
    def engram_gate(
        self,
        hidden: torch.Tensor,
        kv: torch.Tensor,
        q_weight: torch.Tensor,
        k_weight: torch.Tensor,
        mask: torch.Tensor,
        eps: float,
    ) -> torch.Tensor: ...
