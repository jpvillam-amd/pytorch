from collections import namedtuple
from typing import List, Dict, Any
import operator
import torch
from .observation_type import ObservationType
import torch.nn.functional as F
import torch.nn.intrinsic as nni
import torch.nn.intrinsic.qat as nniqat
import torch.nn.qat as nnqat
import torch.nn.quantized._reference as nnqr
from ...observer import (
    default_affine_fixed_qparams_observer,
    default_symmetric_fixed_qparams_observer,
)
from ...fake_quantize import FixedQParamsFakeQuantize
from ...fuser_method_mappings import reverse_sequential_wrapper2

_ConvMetadata = namedtuple("_ConvMetadata", ["root", "reference", "qat", "relu", "relu_qat", "bn_qat", "bn_relu_qat", "func"])
_Conv1dMetadata = _ConvMetadata(torch.nn.Conv1d, nnqr.Conv1d, nnqat.Conv1d, nni.ConvReLU1d,
                                nniqat.ConvReLU1d, nniqat.ConvBn1d, nniqat.ConvBnReLU1d, F.conv1d)
_Conv2dMetadata = _ConvMetadata(torch.nn.Conv2d, nnqr.Conv2d, nnqat.Conv2d, nni.ConvReLU2d,
                                nniqat.ConvReLU2d, nniqat.ConvBn2d, nniqat.ConvBnReLU2d, F.conv2d)
_Conv3dMetadata = _ConvMetadata(torch.nn.Conv3d, nnqr.Conv3d, nnqat.Conv3d, nni.ConvReLU3d,
                                nniqat.ConvReLU3d, nniqat.ConvBn3d, nniqat.ConvBnReLU3d, F.conv3d)

# ===================
# |  DTYPE CONFIGS  |
# ===================

# weighted op int8 dtype config
# this is config for ops that has quantized weights, like linear, conv
weighted_op_int8_dtype_config = {
    # optional, input activation dtype
    "input_dtype": torch.quint8,
    # optional, weight dtype
    "weight_dtype": torch.qint8,
    # optional, bias dtype
    "bias_dtype": torch.float,
    # optional, output activation dtype
    "output_dtype": torch.quint8
}

default_op_quint8_dtype_config = {
    # optional, input activation dtype
    "input_dtype": torch.quint8,
    # optional, output activation dtype
    "output_dtype": torch.quint8,
}

default_op_fp16_dtype_config = {
    # optional, input activation dtype
    "input_dtype": torch.float16,
    # optional, weight dtype
    "weight_dtype": torch.float16,
    # optional, output activation dtype
    "output_dtype": torch.float16,
}

# ======================
# |  OPERATOR CONFIGS  |
# ======================

def _get_default_op_backend_config(op, dtype_configs):
    return {
        "pattern": op,
        "observation_type": ObservationType.OUTPUT_USE_DIFFERENT_OBSERVER_AS_INPUT,
        "dtype_configs": dtype_configs,
    }

_DEFAULT_OP_INT8_CONFIGS = [
    _get_default_op_backend_config(op, [default_op_quint8_dtype_config]) for op in [
        torch.nn.ConvTranspose1d,
        torch.nn.ConvTranspose2d,
        torch.nn.ELU,
        torch.nn.LeakyReLU,
        torch.nn.Hardswish,
        torch.nn.InstanceNorm1d,
        torch.nn.InstanceNorm2d,
        torch.nn.InstanceNorm3d,
        torch.nn.LayerNorm,
        torch.nn.Dropout,
        torch.nn.functional.elu,
        torch.nn.functional.hardswish,
        torch.nn.functional.instance_norm,
        torch.nn.functional.leaky_relu,
        torch.nn.functional.dropout,
        torch.nn.functional.layer_norm,
    ]]

def _get_linear_configs():
    """
    Return all configs related to linear modules and ops.
    """
    observation_type = ObservationType.OUTPUT_USE_DIFFERENT_OBSERVER_AS_INPUT
    dtype_configs = [weighted_op_int8_dtype_config]
    linear_configs = []

    # (1) Single linear modules/functions
    # -------------------------------------
    # linear module
    linear_configs.append({
        # Please see README under this folder for pattern format
        "pattern": torch.nn.Linear,
        "observation_type": observation_type,
        "dtype_configs": dtype_configs,
        # the root module for the pattern, used to query the reference quantized module
        # e.g. for a (torch.nn.ReLU, torch.nn.Linear) pattern, the root will be torch.nn.Linear
        "root_module": torch.nn.Linear,
        # the corresponding reference quantized module for the root module
        "reference_quantized_module_for_root": nnqr.Linear,
        "qat_module": nnqat.Linear,
    })
    # linear qat module
    linear_configs.append({
        "pattern": nnqat.Linear,
        "observation_type": observation_type,
        "dtype_configs": dtype_configs,
        "root_module": torch.nn.Linear,
        "reference_quantized_module_for_root": nnqr.Linear,
    })
    # functional linear
    linear_configs.append({
        "pattern": torch.nn.functional.linear,
        "observation_type": observation_type,
        "dtype_configs": dtype_configs,
    })

    # (2) Linear + relu
    # -------------------
    # linear relu, fused module
    linear_configs.append({
        "pattern": nni.LinearReLU,
        "observation_type": observation_type,
        "dtype_configs": dtype_configs ,
        "root_module": torch.nn.Linear,
        "reference_quantized_module_for_root": nnqr.Linear,
        "qat_module": nniqat.LinearReLU,
    })
    # linear relu, qat fused module
    linear_configs.append({
        "pattern": nniqat.LinearReLU,
        "observation_type": observation_type,
        "dtype_configs": dtype_configs,
        "root_module": torch.nn.Linear,
        "reference_quantized_module_for_root": nnqr.Linear,
    })
    # linear relu, linear module + relu module
    linear_configs.append({
        "pattern": (torch.nn.ReLU, torch.nn.Linear),
        "observation_type": observation_type,
        "dtype_configs": dtype_configs,
        "fuser_method": reverse_sequential_wrapper2(nni.LinearReLU),
    })
    # linear relu, linear module + functional relu
    linear_configs.append({
        "pattern": (F.relu, torch.nn.Linear),
        "observation_type": observation_type,
        "dtype_configs": dtype_configs,
        "fuser_method": reverse_sequential_wrapper2(nni.LinearReLU),
    })
    # linear relu, functional linear + relu module
    linear_configs.append({
        "pattern": (torch.nn.ReLU, F.linear),
        "observation_type": observation_type,
        "dtype_configs": dtype_configs,
    })
    # linear relu, functional linear + functional relu
    linear_configs.append({
        "pattern": (F.relu, F.linear),
        "observation_type": observation_type,
        "dtype_configs": dtype_configs,
    })

    # (3) Linear + batchnorm
    # ------------------------
    # linear bn, fused module
    linear_configs.append({
        "pattern": nni.LinearBn1d,
        "observation_type": observation_type,
        "dtype_configs": dtype_configs,
        "root_module": torch.nn.Linear,
        "reference_quantized_module_for_root": nnqr.Linear,
        "qat_module": nniqat.LinearBn1d,
    })
    # linear bn, qat fused module
    linear_configs.append({
        "pattern": nniqat.LinearBn1d,
        "observation_type": observation_type,
        "dtype_configs": dtype_configs,
        "root_module": torch.nn.Linear,
        "reference_quantized_module_for_root": nnqr.Linear,
    })
    return linear_configs

def _get_conv_configs():
    """
    Return all configs related to conv modules and ops.
    """
    conv_configs = []
    observation_type = ObservationType.OUTPUT_USE_DIFFERENT_OBSERVER_AS_INPUT
    dtype_configs = [weighted_op_int8_dtype_config]
    for convs in [_Conv1dMetadata, _Conv2dMetadata, _Conv3dMetadata]:

        # (1) Single conv modules/functions
        # -----------------------------------
        # conv module
        conv_configs.append({
            "pattern": convs.root,
            "observation_type": observation_type,
            "dtype_configs": dtype_configs,
            "root_module": convs.root,
            "reference_quantized_module_for_root": convs.reference,
            "qat_module": convs.qat,
        })
        # conv qat module
        conv_configs.append({
            "pattern": convs.qat,
            "observation_type": observation_type,
            "dtype_configs": dtype_configs,
            "root_module": convs.root,
            "reference_quantized_module_for_root": convs.reference,
        })
        # functional conv
        conv_configs.append({
            "pattern": convs.func,
            "observation_type": observation_type,
            "dtype_configs": dtype_configs,
        })

        # (2) Conv + relu
        # -----------------
        # conv relu, fused module
        conv_configs.append({
            "pattern": convs.relu,
            "observation_type": observation_type,
            "dtype_configs": dtype_configs,
            "root_module": convs.root,
            "reference_quantized_module_for_root": convs.reference,
            "qat_module": convs.relu_qat,
        })
        # conv relu, qat fused module
        conv_configs.append({
            "pattern": convs.relu_qat,
            "observation_type": observation_type,
            "dtype_configs": dtype_configs,
            "root_module": convs.root,
            "reference_quantized_module_for_root": convs.reference,
        })
        # conv relu, conv module + relu module
        conv_configs.append({
            "pattern": (torch.nn.ReLU, convs.root),
            "observation_type": observation_type,
            "dtype_configs": dtype_configs,
            "fuser_method": reverse_sequential_wrapper2(convs.relu),
        })
        # conv relu, conv module + functional relu
        conv_configs.append({
            "pattern": (F.relu, convs.root),
            "observation_type": observation_type,
            "dtype_configs": dtype_configs,
            "fuser_method": reverse_sequential_wrapper2(convs.relu),
        })
        # conv relu, functional conv + relu module
        conv_configs.append({
            "pattern": (torch.nn.ReLU, convs.func),
            "observation_type": observation_type,
            "dtype_configs": dtype_configs,
        })
        # conv relu, functional conv + functional relu
        conv_configs.append({
            "pattern": (F.relu, convs.func),
            "observation_type": observation_type,
            "dtype_configs": dtype_configs,
        })

        # (3) Conv + batchnorm (+ relu)
        # -------------------------------
        # conv bn, qat fused module
        conv_configs.append({
            "pattern": convs.bn_qat,
            "observation_type": observation_type,
            "dtype_configs": dtype_configs,
            "root_module": convs.root,
            "reference_quantized_module_for_root": convs.reference,
        })
        # conv bn relu, qat fused module
        conv_configs.append({
            "pattern": convs.bn_relu_qat,
            "observation_type": observation_type,
            "dtype_configs": dtype_configs,
            "root_module": convs.root,
            "reference_quantized_module_for_root": convs.reference,
        })
    return conv_configs

def _get_binary_op_configs():
    binary_op_configs: List[Dict[str, Any]] = []
    num_tensor_args_to_observation_type_mapping = {
        # TODO: this is not used right now since we have extra check in prepare
        # will need to change this to NO_OBSERVER later after we implemented
        # Tensor dtype inference properly
        0: ObservationType.OUTPUT_USE_DIFFERENT_OBSERVER_AS_INPUT,
        1: ObservationType.OUTPUT_SHARE_OBSERVER_WITH_INPUT,
        2: ObservationType.OUTPUT_USE_DIFFERENT_OBSERVER_AS_INPUT,
    }
    dtype_configs = [
        weighted_op_int8_dtype_config,
    ]
    for op_with_quantized_bop_scalar_variant in [
            operator.add, torch.add, operator.mul, torch.mul]:
        binary_op_configs.append({
            "pattern": (torch.nn.ReLU, op_with_quantized_bop_scalar_variant),
            "num_tensor_args_to_observation_type": num_tensor_args_to_observation_type_mapping,
            "dtype_configs": dtype_configs,
        })
        binary_op_configs.append({
            "pattern": (torch.nn.functional.relu, op_with_quantized_bop_scalar_variant),
            "num_tensor_args_to_observation_type": num_tensor_args_to_observation_type_mapping,
            "dtype_configs": dtype_configs,
        })
        binary_op_configs.append({
            "pattern": (torch.relu, op_with_quantized_bop_scalar_variant),
            "num_tensor_args_to_observation_type": num_tensor_args_to_observation_type_mapping,
            "dtype_configs": dtype_configs,
        })
        binary_op_configs.append({
            "pattern": op_with_quantized_bop_scalar_variant,
            "num_tensor_args_to_observation_type": num_tensor_args_to_observation_type_mapping,
            "dtype_configs": dtype_configs,
        })
    return binary_op_configs


def _get_fixed_qparams_op_configs():
    fixed_qparams_op_configs = []
    for fixed_qparam_op, output_observer in [
            (torch.nn.Hardsigmoid, default_affine_fixed_qparams_observer),
            (torch.nn.functional.hardsigmoid, default_affine_fixed_qparams_observer),
            ("hardsigmoid", default_affine_fixed_qparams_observer),
            ("hardsigmoid_", default_affine_fixed_qparams_observer),
            (torch.nn.Sigmoid, default_affine_fixed_qparams_observer),
            (torch.sigmoid, default_affine_fixed_qparams_observer),
            ("sigmoid", default_affine_fixed_qparams_observer),
            ("sigmoid_", default_affine_fixed_qparams_observer),
            (torch.nn.Tanh, default_symmetric_fixed_qparams_observer),
            (torch.tanh, default_symmetric_fixed_qparams_observer),
            ("tanh", default_symmetric_fixed_qparams_observer),
            ("tanh_", default_symmetric_fixed_qparams_observer),
    ]:
        fixed_qparams_op_configs.append({
            "pattern": fixed_qparam_op,
            "observation_type": ObservationType.OUTPUT_USE_DIFFERENT_OBSERVER_AS_INPUT,
            # TODO: The following two keys are temporary, since we don't want to put observer in the configs
            # we expect that it's provided by user
            # What we want to put here is the requirement on observers, in this case dtype,
            # quant_min, quant_max etc., but we need to first move all configs to
            # backend_config_dict to do that, we'll remove these keys after we fully migrated
            # everything to use backend_config_dict
            "_overwrite_output_fake_quantizer": FixedQParamsFakeQuantize.with_args(observer=output_observer),
            "_overwrite_output_observer": output_observer,
            "dtype_configs": [
                weighted_op_int8_dtype_config,
            ],
        })
    return fixed_qparams_op_configs

_CAT_CONFIG = {
    "pattern": torch.cat,
    "observation_type": ObservationType.OUTPUT_SHARE_OBSERVER_WITH_INPUT,
    "dtype_configs": [
        default_op_quint8_dtype_config,
    ]
}

def _get_bn_configs():
    """ Get configs related to batchnorm
    """
    bn_configs = []
    bn_to_fused_bn = {
        torch.nn.BatchNorm2d: nni.BNReLU2d,
        torch.nn.BatchNorm3d: nni.BNReLU3d,
    }
    for bn in [torch.nn.BatchNorm2d, torch.nn.BatchNorm3d]:
        # TODO: enable these and remove entries in fusion_patterns.py
        # bn_configs.append({
        #     "pattern": (torch.nn.ReLU, bn),
        #     "observation_type": ObservationType.OUTPUT_USE_DIFFERENT_OBSERVER_AS_INPUT,
        #     "dtype_configs": default_op_quint8_dtype_config,
        #     "fuser_method": reverse_sequential_wrapper2(bn_to_fused_bn[bn]),
        # })
        # bn_configs.append({
        #     "pattern": (torch.nn.functional.relu, bn),
        #     "observation_type": ObservationType.OUTPUT_USE_DIFFERENT_OBSERVER_AS_INPUT,
        #     "dtype_configs": default_op_quint8_dtype_config,
        #     "fuser_method": reverse_sequential_wrapper2(bn_to_fused_bn[bn]),
        # })
        bn_configs.append({
            "pattern": bn,
            "observation_type": ObservationType.OUTPUT_USE_DIFFERENT_OBSERVER_AS_INPUT,
            "dtype_configs": default_op_quint8_dtype_config,
        })

    for fused_bn in [nni.BNReLU2d, nni.BNReLU3d]:
        bn_configs.append({
            "pattern": fused_bn,
            "observation_type": ObservationType.OUTPUT_USE_DIFFERENT_OBSERVER_AS_INPUT,
            "dtype_configs": default_op_quint8_dtype_config,
        })
    return bn_configs

def _get_share_qparams_op_configs():
    """ Get the operator config for the operators that works for both float and quantized input
    if input is quantized, the output Tensor shares the same quantization parameter
    with input.
    Example operator: avgpool2d, reshape, transpose, maxpool2d
    Example observed operator:
    observer_0 - avgpool2d - observer_0 (same observer instance as input)
    """

    def _get_share_qprams_op_backend_config(op):
        return {
            "pattern": op,
            "observation_type": ObservationType.OUTPUT_SHARE_OBSERVER_WITH_INPUT,
            "dtype_configs": [default_op_quint8_dtype_config],
        }

    share_qparams_ops = [
        torch.nn.AdaptiveAvgPool1d,
        torch.nn.AdaptiveAvgPool2d,
        torch.nn.AdaptiveAvgPool3d,
        torch.nn.AvgPool1d,
        torch.nn.AvgPool2d,
        torch.nn.AvgPool3d,
        torch.nn.Hardtanh,
        torch.nn.Identity,
        torch.nn.MaxPool1d,
        torch.nn.MaxPool2d,
        torch.nn.MaxPool3d,
        torch.nn.ReLU,
        torch.nn.ReLU6,
        torch.adaptive_avg_pool1d,
        torch.nn.functional.adaptive_avg_pool2d,
        torch.nn.functional.adaptive_avg_pool3d,
        torch.nn.functional.hardtanh,
        torch.nn.functional.hardtanh_,
        torch.nn.functional.interpolate,
        torch.nn.functional.max_pool1d,
        torch.nn.functional.max_pool2d,
        torch.nn.functional.max_pool3d,
        torch.nn.functional.relu,
        torch.nn.functional.relu6,
        torch.avg_pool1d,
        torch._C._nn.avg_pool2d,
        torch._C._nn.avg_pool3d,
        torch.clamp,
        torch.flatten,
        torch.mean,
        torch.repeat_interleave,
        torch.transpose,
        torch.squeeze,
        torch.stack,
        torch.unsqueeze,
        operator.floordiv,
        "contiguous",
        "clamp",
        "detach",
        "detach_",
        "mean",
        "permute",
        "repeat",
        "repeat_interleave",
        "reshape",
        "resize_",
        "relu",
        "relu_",
        "shape",
        "size",
        "squeeze",
        "squeeze_",
        "transpose",
        "unsqueeze",
        "unsqueeze_",
        "view"
    ]
    return [_get_share_qprams_op_backend_config(op) for op in share_qparams_ops]

def get_native_backend_config_dict():
    """ Get backend_config_dict for PyTorch Native backend (fbgemm/qnnpack). """
    return {
        # optional
        "name": "native",
        "configs": [
            *_DEFAULT_OP_INT8_CONFIGS,
            *_get_linear_configs(),
            *_get_conv_configs(),
            *_get_binary_op_configs(),
            *_get_fixed_qparams_op_configs(),
            _CAT_CONFIG,
            *_get_bn_configs(),
            *_get_share_qparams_op_configs(),
        ],
    }
