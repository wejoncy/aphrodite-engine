from typing import Type

import pytest
import torch

from aphrodite.modeling.layers.activation import (FastGELU, GeluAndMul,
                                                  NewGELU, QuickGELU,
                                                  SiluAndMul)
from tests.kernels.utils import opcheck

from .allclose_default import get_default_atol, get_default_rtol

DTYPES = [torch.half, torch.bfloat16, torch.float]
NUM_TOKENS = [7, 83, 2048]  # Arbitrary values for testing
D = [512, 4096, 5120, 13824]  # Arbitrary values for testing
SEEDS = [0]
CUDA_DEVICES = [
    f"cuda:{i}" for i in range(1 if torch.cuda.device_count() == 1 else 2)
]


@pytest.mark.parametrize("activation", ["silu", "gelu", "gelu_tanh"])
@pytest.mark.parametrize("num_tokens", NUM_TOKENS)
@pytest.mark.parametrize("d", D)
@pytest.mark.parametrize("dtype", DTYPES)
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("device", CUDA_DEVICES)
@torch.inference_mode()
def test_act_and_mul(
    activation: str,
    num_tokens: int,
    d: int,
    dtype: torch.dtype,
    seed: int,
    device: str,
) -> None:
    torch.random.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
    torch.set_default_device(device)
    x = torch.randn(num_tokens, 2 * d, dtype=dtype)
    if activation == "silu":
        layer = SiluAndMul()
        fn = torch.ops._C.silu_and_mul
    elif activation == "gelu":
        layer = GeluAndMul(approximate="none")
        fn = torch.ops._C.gelu_and_mul
    elif activation == "gelu_tanh":
        layer = GeluAndMul(approximate="tanh")
        fn = torch.ops._C.gelu_tanh_and_mul
    out = layer(x)
    ref_out = layer.forward_native(x)
    # The SiLU and GELU implementations are equivalent to the native PyTorch
    # implementations, so we can do exact comparison.
    torch.testing.assert_close(out, ref_out, atol=0.0, rtol=0.0)


    d = x.shape[-1] // 2
    output_shape = (x.shape[:-1] + (d, ))
    out = torch.empty(output_shape, dtype=x.dtype, device=x.device)
    opcheck(fn, (out, x))

@pytest.mark.parametrize("activation", [(FastGELU, torch.ops._C.gelu_fast),
                                        (NewGELU, torch.ops._C.gelu_new),
                                        (QuickGELU, torch.ops._C.gelu_quick)])
@pytest.mark.parametrize("num_tokens", NUM_TOKENS)
@pytest.mark.parametrize("d", D)
@pytest.mark.parametrize("dtype", DTYPES)
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("device", CUDA_DEVICES)
@torch.inference_mode()
def test_activation(
    activation: Type[torch.nn.Module],
    num_tokens: int,
    d: int,
    dtype: torch.dtype,
    seed: int,
    device: str,
) -> None:
    torch.random.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
    torch.set_default_device(device)
    x = torch.randn(num_tokens, d, dtype=dtype)
    layer = activation[0]()
    fn = activation[1]
    out = layer(x)
    ref_out = layer.forward_native(x)
    torch.testing.assert_close(out,
                               ref_out,
                               atol=get_default_atol(out),
                               rtol=get_default_rtol(out))
    out = torch.empty_like(x)
    opcheck(fn, (out, x))


@pytest.mark.parametrize("activation_cls, kwargs", [
    (SiluAndMul, {}),
    (GeluAndMul, {"approximate": "none"}),
    (GeluAndMul, {"approximate": "tanh"}),
])
@pytest.mark.parametrize("num_tokens", NUM_TOKENS)
@pytest.mark.parametrize("d", D)
@pytest.mark.parametrize("dtype", DTYPES)
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("device", CUDA_DEVICES)
@torch.inference_mode()
def test_activation_triton(
    activation_cls, kwargs, num_tokens, d, dtype, seed, device):
    torch.random.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
    torch.set_default_device(device)

    activation = activation_cls(**kwargs).to(device=device, dtype=dtype)
    # Input shape is (num_tokens, 2*d) for these activations.
    x = torch.randn(num_tokens, 2 * d, dtype=dtype, device=device)

    native_out = activation.forward_native(x)
    triton_out = activation.forward_triton(x)

    torch.testing.assert_close(triton_out, native_out, atol=1e-2, rtol=1e-2)
