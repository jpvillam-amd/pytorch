# Owner(s): ["oncall: jit"]

import unittest

from torch._lazy.ts_backend import init as init_ts_backend
init_ts_backend()
from torch._lazy import config
from torch._lazy.extract_compiled_graph import extract_compiled_graph
import torch
from torch import nn
import dis
import inspect
from torch import fx
import re
from contextlib import contextmanager

class ModuleConstScale(nn.Module):
    def __init__(self):
        super(ModuleConstScale, self).__init__()

    def forward(self, a):
        return a * 2

class ModuleSub(nn.Module):
    def __init__(self):
        super(ModuleSub, self).__init__()

    def forward(self, a, b):
        return a - b

class ModuleAddcmul(nn.Module):
    """
    addcmul function takes a at::Scalar which results in a special TSData containing a Scalar rather than a Tensor.
    """
    def __init__(self):
        super(ModuleAddcmul, self).__init__()

    def forward(self, a, b, c):
        return torch.addcmul(a, b, c, value=5)

class ModuleReturnMulti(nn.Module):
    def __init__(self):
        super(ModuleReturnMulti, self).__init__()

    def forward(self, a, b):
        return (b + 1, a - 1)

# The default fx tracer will convert torch.randn to a constant.. We may need
# a custom tracer.
# class ModuleEagerTensor(nn.Module):
#     def __init__(self):
#         super(ModuleEagerTensor, self).__init__()
#
#     def forward(self, a):
#         b = torch.randn(2, 3, device="cpu") # eager device
#         return a + b

# The module was planned to cover the case that a Fx graph return an eager
# tensor on the default device. It's harder than ModuleEagerTensor because
# we can not just override the device argument to Lazy since there is no
# explicit device argument.
#
# Unfortunately, the default fx tracer convert the return value of the forward
# method to a constant.. Comment out for now
# class ModuleReturnEagerTensorOnDefaultDevice(nn.Module):
#     def __init__(self):
#         super(ModuleReturnEagerTensorOnDefaultDevice, self).__init__()
#
#     def forward(self):
#         return torch.tensor((2, 3), dtype=torch.float32)

class ModuleReturnDupTensor(nn.Module):
    """
    Handle the corner case that the same tensor appears multiple times in the
    returned tuple. torchbench like drq will hit this corner case when running
    thru torchdynamo..
    """
    def __init__(self):
        super(ModuleReturnDupTensor, self).__init__()

    def forward(self, a, b):
        c = a + b
        return a - b, c, a + 1, c

@contextmanager
def force_fallback_ctx_mgr(fallback_op):
    oldconfig = config.get_force_fallback()
    config.set_force_fallback(fallback_op)
    try:
        yield None
    finally:
        config.set_force_fallback(oldconfig)

@contextmanager
def nop_ctx_mgr():
    try:
        yield None
    finally:
        pass

def gen_rand_args(mod):
    args = []
    for _ in range(len(inspect.signature(mod.forward).parameters)):
        args.append(torch.randn(2, 3))
    return args

def allclose(expected, actual):
    def unwrap(cont):
        if isinstance(cont, (list, tuple)) and len(cont) == 1:
            return cont[0]
        return cont
    expected = unwrap(expected)
    actual = unwrap(actual)

    if isinstance(expected, torch.Tensor) and isinstance(actual, torch.Tensor):
        return torch.allclose(expected, actual)
    elif isinstance(expected, (tuple, list)) and isinstance(actual, (tuple, list)):
        return len(expected) == len(actual) and all(torch.allclose(a, b) for a, b in zip(expected, actual))
    else:
        raise RuntimeError("Unexpected types")

def verify_reusing_compiled_graph(mod, exception_msg_pattern, ncase=10):
    args = gen_rand_args(mod)
    out = mod(*args)

    dis.dis(mod.forward)

    try:
        optimized_mod = extract_compiled_graph(fx.symbolic_trace(mod), args)
    except RuntimeError as e:
        if exception_msg_pattern is None:
            raise e  # reraise the exception
        exception_message = str(e)
        if not re.search(exception_msg_pattern, exception_message):
            raise RuntimeError(f"Expection message does not match the required pattern: {exception_message}")
        else:
            # We are done for the test case that expects an exception
            return

    if exception_msg_pattern is not None:
        raise RuntimeError(f"Expect an exception matching pattern {exception_msg_pattern}")
    print("return value of optimized_mod", optimized_mod(*args))

    # check correctness
    failed_index = []
    for i in range(ncase):
        rand_args = gen_rand_args(mod)
        expected = mod(*rand_args)
        actual = optimized_mod(*rand_args)

        if not allclose(expected, actual):
            print(f"Incorrect results. expected {expected}, actual {actual}")
            failed_index.append(i)

    if len(failed_index) > 0:
        raise RuntimeError(f"Failed {len(failed_index)}/{ncase} cases")

def maketest(module_cls, exception_msg_pattern=None, ctxmgr=None):
    def wrapper(self):
        nonlocal ctxmgr
        if not ctxmgr:
            ctxmgr = nop_ctx_mgr()
        with ctxmgr:
            verify_reusing_compiled_graph(module_cls(), exception_msg_pattern)

    return wrapper

class OptimizeTest(unittest.TestCase):
    test_sub = maketest(ModuleSub)
    # Same as test_sub but force aten::sub to fallback
    # We expect an exception caught because of LTC fallabck.
    test_ltc_fallback = maketest(ModuleSub, exception_msg_pattern="fallback.*aten::sub", ctxmgr=force_fallback_ctx_mgr("aten::sub"))
    test_const_scale = maketest(ModuleConstScale)
    test_addcmul = maketest(ModuleAddcmul)
    test_return_multi = maketest(ModuleReturnMulti)
    test_return_dup_tensor = maketest(ModuleReturnDupTensor)
