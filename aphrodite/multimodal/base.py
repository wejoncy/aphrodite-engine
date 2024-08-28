import sys
from abc import ABC, abstractmethod
from collections import UserDict, defaultdict
from typing import (Any, Callable, Dict, List, Optional, Type, TypedDict,
                    TypeVar, Union, cast)

import torch
import torch.types
from loguru import logger
from PIL import Image
from torch import nn

from aphrodite.common.config import ModelConfig
from aphrodite.inputs import InputContext

NestedTensors = Union[List[torch.Tensor], torch.Tensor]
"""
Use a list instead of a tensor if the dimensions of each element do not match.
Currently only supports up to singly nested list of tensors.
"""

BatchedTensors = Union[List[NestedTensors], NestedTensors]
"""
If each input tensor in the batch has the same size, this is a single batched
tensor; otherwise, this is a list of :class:`NestedTensors` with one element
per item in the batch.
"""

if sys.version_info < (3, 9):
    # UserDict cannot be subscripted
    class _MultiModalInputsBase(UserDict):
        pass
else:

    class _MultiModalInputsBase(UserDict[str, NestedTensors]):
        pass


class MultiModalInputs(_MultiModalInputsBase):
    """
    A dictionary that represents the keyword arguments to
    :meth:`~torch.nn.Module.forward`.
    """

    @staticmethod
    def try_concat(
        tensors: List[NestedTensors],
        *,
        device: torch.types.Device,
    ) -> BatchedTensors:
        # Avoid initializing CUDA too early
        import torch

        # may be list rather than tensors
        if isinstance(tensors[0], list):
            return [[t.to(device=device) for t in tensor[0]]
                    for tensor in tensors]

        tensors_ = cast(List[torch.Tensor], tensors)

        unbatched_shape = tensors_[0].shape[1:]

        for tensor in tensors_:
            if tensor.shape[1:] != unbatched_shape:
                return [
                    tensor.squeeze(0).to(device=device) for tensor in tensors_
                ]

        return torch.cat(tensors_, dim=0).to(device=device)

    @staticmethod
    def batch(
        inputs_list: List["MultiModalInputs"],
        device: torch.types.Device,
    ) -> Dict[str, BatchedTensors]:
        """Batch multiple inputs together into a dictionary."""
        if len(inputs_list) == 0:
            return {}

        keys = inputs_list[0].keys()

        item_lists: Dict[str, List[NestedTensors]] = defaultdict(list)

        for inputs in inputs_list:
            if inputs.keys() != keys:
                msg = f"Inputs do not share the same keys ({keys})"
                raise ValueError(msg)

            for k, v in inputs.items():
                item_lists[k].append(v)

        return {
            k: MultiModalInputs.try_concat(item_list, device=device)
            for k, item_list in item_lists.items()
        }


class MultiModalDataBuiltins(TypedDict, total=False):
    image: Image.Image


MultiModalDataDict = Union[MultiModalDataBuiltins, Dict[str, Any]]
"""
A dictionary containing an item for each modality type to input.

The data belonging to each modality is converted into keyword arguments 
to the model by the corresponding mapper. By default, the mapper of 
the corresponding plugin with the same modality key is applied.
"""

MultiModalInputMapper = Callable[[InputContext, object], MultiModalInputs]
"""
Return a dictionary to be passed as keyword arguments to
:meth:`~torch.nn.Module.forward`. This is similar in concept to tokenizers
and processors in HuggingFace Transformers.
If the data is not supported, throw :exc:`TypeError`.
"""

MultiModalTokensCalc = Union[int, Callable[[InputContext], int]]
"""
Calculate the maximum number of multimodal tokens input to the language
model. This does not include tokens that correspond to the input text.
"""

N = TypeVar("N", bound=Type[nn.Module])


class MultiModalPlugin(ABC):
    """
    Base class that defines data processing logic for a specific modality.

    In particular, we adopt a registry pattern to dispatch data processing
    according to the model being used (considering that different models may
    process the same data differently). This registry is in turn used by
    :class:`~MultiModalRegistry` which acts at a higher level
    (i.e., the modality of the data).
    """

    def __init__(self) -> None:
        self._input_mappers: Dict[Type[nn.Module], MultiModalInputMapper] = {}
        self._max_mm_tokens: Dict[Type[nn.Module], MultiModalTokensCalc] = {}

    @abstractmethod
    def get_data_key(self) -> str:
        """
        Get the data key corresponding to the modality.
        """
        raise NotImplementedError

    @abstractmethod
    def _default_input_mapper(self, ctx: InputContext,
                              data: object) -> MultiModalInputs:
        """
        Return a dictionary to be passed as keyword arguments to
        :meth:`~torch.nn.Module.forward`. This is similar in concept to
        tokenizers and processors in HuggingFace Transformers.
        If the data is not supported, throw :exc:`TypeError`.
        """
        raise NotImplementedError

    def register_input_mapper(
        self,
        mapper: Optional[MultiModalInputMapper] = None,
    ):
        """
        Register an input mapper to a model class.
        When the model receives input data that matches the modality served by
        this plugin (see :meth:`get_data_type`), the provided function is
        invoked to transform the data into a dictionary of model inputs.
        If `None` is provided, then the default input mapper is used instead.

        See also:
            :ref:`input_processing_pipeline`
            :ref:`adding_a_new_multimodal_model`
        """

        def wrapper(model_cls: N) -> N:
            if model_cls in self._input_mappers:
                logger.warning(
                    f"Model class {model_cls} already has an input mapper "
                    f"registered to {self}. It is overwritten by the new one.")

            self._input_mappers[model_cls] = mapper \
                or self._default_input_mapper

            return model_cls

        return wrapper

    def map_input(self, model_config: ModelConfig,
                  data: object) -> MultiModalInputs:
        """
        Apply an input mapper to a data passed
        to the model, transforming the data into a dictionary of model inputs.

        If the data is not something that the mapper expects, throws TypeError.

        The model is identified by ``model_config``.

        See also:
            :ref:`adding_a_new_multimodal_model`
        """
        # Avoid circular import
        from aphrodite.modeling.model_loader import get_model_architecture

        model_cls, _ = get_model_architecture(model_config)

        mapper = self._input_mappers.get(model_cls)
        if mapper is None:
            raise KeyError(f"No input mapper in {self} is registered for "
                           f"model class {model_cls.__name__}.")

        return mapper(InputContext(model_config), data)

    @abstractmethod
    def _default_max_multimodal_tokens(self, ctx: InputContext) -> int:
        """
        Calculate the maximum number of multimodal tokens input to the language
        model. This does not include tokens that correspond to the input text.
        """
        raise NotImplementedError

    def _validate_max_multimodal_tokens(self, max_mm_tokens: int):
        if max_mm_tokens < 1:
            raise ValueError("You should set the number of tokens to a "
                             f"positive integer. Found: {max_mm_tokens}")

    def register_max_multimodal_tokens(
        self,
        max_mm_tokens: Optional[MultiModalTokensCalc] = None,
    ):
        """
        Register the maximum number of multi-modal tokens input to the
        language model for a model class.
        If `None` is provided, then the default calculation is used instead.
        See also:
            :ref:`adding_a_new_multimodal_model`
        """

        def wrapper(model_cls: N) -> N:
            if model_cls in self._max_mm_tokens:
                logger.warning(
                    f"Model class {model_cls} already calculates maximum "
                    f"number of tokens in {self}. It is overwritten by the "
                    "new one.")

            if isinstance(max_mm_tokens, int):
                self._validate_max_multimodal_tokens(max_mm_tokens)

            self._max_mm_tokens[model_cls] = max_mm_tokens \
                or self._default_max_multimodal_tokens

            return model_cls

        return wrapper

    def get_max_multimodal_tokens(self, model_config: ModelConfig) -> int:
        """
        Get the maximum number of multi-modal tokens
        for profiling the memory usage of a model.
        If this registry is not applicable to the model, `0` is returned.
        The model is identified by ``model_config``.
        See also:
            :ref:`adding_a_new_multimodal_model`
        """
        # Avoid circular import
        from aphrodite.modeling.model_loader import get_model_architecture

        model_cls, _ = get_model_architecture(model_config)

        if model_cls not in self._input_mappers:
            return 0

        max_mm_tokens = self._max_mm_tokens.get(model_cls)
        if max_mm_tokens is None:
            raise KeyError(f"No maximum number of multi-modal tokens is given "
                           f"for model class {model_cls.__name__} in {self}.")

        if callable(max_mm_tokens):
            max_mm_tokens = max_mm_tokens(InputContext(model_config))

        self._validate_max_multimodal_tokens(max_mm_tokens)

        return max_mm_tokens
