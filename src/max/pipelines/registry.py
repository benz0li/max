# ===----------------------------------------------------------------------=== #
# Copyright (c) 2025, Modular Inc. All rights reserved.
#
# Licensed under the Apache License v2.0 with LLVM Exceptions:
# https://llvm.org/LICENSE.txt
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ===----------------------------------------------------------------------=== #

"""Model registry, for tracking various model variants."""

from __future__ import annotations

import functools
import logging
import os
from io import StringIO
from typing import Callable, Optional, Type, Union, cast

import torch
from max.graph.weights import WeightsConverter

from .config import (
    PipelineConfig,
    PipelineEngine,
    RopeType,
    SupportedEncoding,
    WeightsFormat,
)
from .embeddings_pipeline import EmbeddingsPipeline
from .hf_pipeline import HFEmbeddingsPipeline, HFTextGenerationPipeline
from .interfaces import (
    EmbeddingsGenerator,
    PipelineTask,
    PipelineTokenizer,
    TokenGenerator,
)
from .kv_cache import KVCacheStrategy
from .pipeline import KVCacheMixin, PipelineModel, TextGenerationPipeline
from .tokenizer import TextAndVisionTokenizer, TextTokenizer

logger = logging.getLogger("max.pipelines")

# Store a map of checkpoint encodings that can be cast to another dtype while
# keeping similar results. Maps the requested encoding to an acceptable
# alternate checkpoint encoding.
_ALTERNATE_ENCODINGS = {
    SupportedEncoding.float32: SupportedEncoding.bfloat16,
    SupportedEncoding.bfloat16: SupportedEncoding.float32,
}

_PIPELINE_TASK_MAP = {
    PipelineTask.TEXT_GENERATION: TextGenerationPipeline,
    PipelineTask.EMBEDDINGS_GENERATION: EmbeddingsPipeline,
}


_HF_PIPELINE_TASK_MAP: dict[
    PipelineTask, type[HFTextGenerationPipeline] | type[HFEmbeddingsPipeline]
] = {
    PipelineTask.TEXT_GENERATION: HFTextGenerationPipeline,
    PipelineTask.EMBEDDINGS_GENERATION: HFEmbeddingsPipeline,
}


def _to_mib(bytes):
    return round(bytes / 1024 / 1024)


class SupportedArchitecture:
    def __init__(
        self,
        name: str,
        example_repo_ids: list[str],
        default_encoding: SupportedEncoding,
        supported_encodings: dict[SupportedEncoding, list[KVCacheStrategy]],
        pipeline_model: Type[PipelineModel],
        task: PipelineTask,
        tokenizer: Type[Union[TextTokenizer, TextAndVisionTokenizer]],
        default_weights_format: WeightsFormat,
        rope_type: RopeType = RopeType.none,
        weight_converters: (
            dict[WeightsFormat, Type[WeightsConverter]] | None
        ) = None,
    ):
        """Initializes a model architecture supported by MAX pipelines.

        New architectures should be registered into the `PipelineRegistry`.

        args:
            name: Architecture name.
            example_repo_ids: HuggingFace repo_id which runs this architecture.
            default_encoding: Default encoding for the model.
            supported_encodings: Alternate encodings supported.
            pipeline_model: PipelineModel class that defines the model graph
                and execution.
            task: Which pipeline task should the model run with.
            tokenizer: Tokenizer used to preprocess model inputs.
            default_weights_format: The weights format used in `pipeline_model`.
            weight_converters: A dictionary of weight loaders to use if the
                input checkpoint has a different format than the default.
        """
        self.name = name
        self.example_repo_ids = example_repo_ids
        self.default_encoding = default_encoding
        self.supported_encodings = supported_encodings
        self.pipeline_model = pipeline_model
        self.tokenizer = tokenizer
        self.default_weights_format = default_weights_format
        self.rope_type = rope_type
        self.weight_converters = weight_converters or {}
        self.task = task


class PipelineRegistry:
    def __init__(self, architectures: list[SupportedArchitecture]):
        self.architectures = {arch.name: arch for arch in architectures}

    def register(self, architecture: SupportedArchitecture):
        """Add new architecture to registry."""
        if architecture.name in self.architectures:
            msg = f"Refusing to override existing architecture for '{architecture.name}'"
            raise ValueError(msg)

        self.architectures[architecture.name] = architecture

    def architecture_details(
        self, pipeline_config: PipelineConfig
    ) -> Optional[SupportedArchitecture]:
        """Return architecture details for pipeline_config if available, None if not found."""

        # If no architecture is provided in the pipeline_config, we have nothing to retrieve.
        if not pipeline_config.architecture:
            return None

        # If the engine is not provided or MAX, we should retrieve the architecture and validate it.
        if (
            not pipeline_config.engine
            or pipeline_config.engine == PipelineEngine.MAX
        ):
            if pipeline_config.architecture in self.architectures:
                return self.architectures[pipeline_config.architecture]
            else:
                return None
        else:
            return None

    def validate_pipeline_config(
        self, pipeline_config: PipelineConfig
    ) -> PipelineConfig:
        """Update pipeline config with appropriate values if not provided.
        If invalid config is provided, error out with detailed reason."""

        # This will update the architecture, and engine if no architecture is available.
        pipeline_config.update_architecture()

        # This will retrieve the architecture, if we support it.
        arch = self.architecture_details(pipeline_config)

        # If nothing is provided, we should not update any more params.
        # Instead, fall back to the HuggingFace engine.
        if not arch and pipeline_config.engine == PipelineEngine.MAX:
            msg = (
                "optimized architecture not available for"
                f" '{pipeline_config.architecture}', failing as engine is provided as 'MAX'"
            )
            raise ValueError(msg)

        elif not arch:
            msg = (
                "optimized architecture not available for"
                f" '{pipeline_config.architecture}' falling back to"
                " HuggingFace."
            )
            logger.warning(msg)
            pipeline_config.engine = PipelineEngine.HUGGINGFACE
            return pipeline_config

        # The remainder of this function, assumes we have both a valid model_path,
        # and a SupportedArchitecture. We should then validate the details of the existing architecture
        # and fallback to HuggingFace if needed.

        # If weight_path and quantization_encoding are provided, verify that they are consistent.
        huggingface_weights_repo = pipeline_config.huggingface_weights_repo()
        if (
            pipeline_config.weight_path
            and pipeline_config.quantization_encoding
            # Cannot validate quantization_encoding for pytorch.
            and pipeline_config.weights_format != WeightsFormat.pytorch
        ):
            # Get the encoding of the first weight path file.
            if os.path.exists(pipeline_config.weight_path[0]):
                file_encoding = SupportedEncoding.parse_from_file_name(
                    str(pipeline_config.weight_path[0])
                )
            else:
                file_encoding = huggingface_weights_repo.encoding_for_file(
                    pipeline_config.weight_path[0]
                )

            if file_encoding:
                if file_encoding != pipeline_config.quantization_encoding:
                    msg = f"weight_path provided '{pipeline_config.weight_path[0]}' has an inconsistent encoding '{file_encoding}' than quantization_encoding provided '{pipeline_config.quantization_encoding}'. Please update one."
                    raise ValueError(msg)
        # If weight path is not None, infer the quantization_encoding from the weight_path.
        elif (
            pipeline_config.weight_path
            and not pipeline_config.quantization_encoding
            and pipeline_config.weights_format != WeightsFormat.pytorch
        ):
            if os.path.exists(pipeline_config.weight_path[0]):
                # Not currently supported. Infer encoding from local path.
                if pipeline_config.weight_path[0].suffix == ".safetensors":
                    msg = "If a local safetensors file is provided, please provide a quantization_encoding."
                    raise ValueError(msg)

                if encoding := SupportedEncoding.parse_from_file_name(
                    str(pipeline_config.weight_path[0])
                ):
                    msg = f"encoding inferred from weights file: {encoding}"
                    logger.debug(msg)
                    pipeline_config.quantization_encoding = encoding

            else:
                if encoding := huggingface_weights_repo.encoding_for_file(
                    pipeline_config.weight_path[0]
                ):
                    msg = f"encoding inferred from weights file: {encoding}"
                    logger.debug(msg)
                    pipeline_config.quantization_encoding = encoding
                else:
                    msg = f"encoding cannot be inferred from weights file: {pipeline_config.weight_path[0]}, please pass a quantization_encoding explictly."
                    raise ValueError(msg)
        elif not pipeline_config.quantization_encoding:
            # Check if the repo only has one quantization_encoding.
            supported_encodings = huggingface_weights_repo.supported_encodings
            if len(supported_encodings) == 1:
                msg = f"huggingface repo only has '{supported_encodings[0]}' weights, using '{supported_encodings[0]}'"
                logger.debug(msg)
                pipeline_config.quantization_encoding = supported_encodings[0]
            elif (
                not pipeline_config.devices[0].is_host
            ) and SupportedEncoding.bfloat16 in arch.supported_encodings:
                # TODO(AITLIB-137): replace this with more full featured logic.
                # If we are running on an accelerator and the quantiziation encoding is not set, override to bfloat16.
                pipeline_config.quantization_encoding = (
                    SupportedEncoding.bfloat16
                )
            else:
                msg = f"encoding not provided, using default encoding of {arch.default_encoding}"
                logger.debug(msg)
                pipeline_config.quantization_encoding = arch.default_encoding
        # by this point, the quantization_encoding must be provided. verify it is supported.
        if (
            pipeline_config.quantization_encoding
            not in arch.supported_encodings
        ):
            if pipeline_config.engine == PipelineEngine.MAX:
                msg = f"quantization_encoding of '{pipeline_config.quantization_encoding}' not supported by MAX engine, unable to run with engine = 'max'."
                raise ValueError(msg)

            else:
                msg = f"quantization_encoding of '{pipeline_config.quantization_encoding}' not supported by MAX engine, falling back to HuggingFace."
                logger.warning(msg)
                pipeline_config.engine = PipelineEngine.HUGGINGFACE
                return pipeline_config

        # Check that the quantization encoding is supported on the specified
        # devices.
        for device_spec in pipeline_config.device_specs:
            if not pipeline_config.quantization_encoding.supported_on(
                device_spec
            ):
                raise ValueError(
                    f"{pipeline_config.quantization_encoding} is not supported on {device_spec.device_type}. "
                    "Please use the flag --devices=cpu or --devices=gpu to configure the device."
                )

        pipeline_config.finalize_encoding_config()

        # We should now have a valid quantization_encoding, and possibly a weight_path.
        # If no weight_path is provided, we should grab the default.
        if not pipeline_config.weight_path:
            # Retrieve the default files for each weights format.

            # Get alternate encoding (e.g. if float32 is requested and there are
            # only bfloat16 weights, allow retrieving the bfloat16 weights
            # because they can be cast to float32).
            alternate_encoding = _ALTERNATE_ENCODINGS.get(
                pipeline_config.quantization_encoding
            )

            weight_files = huggingface_weights_repo.files_for_encoding(
                encoding=pipeline_config.quantization_encoding,
                alternate_encoding=alternate_encoding,
            )

            if default_weight_files := weight_files.get(
                arch.default_weights_format, []
            ):
                pipeline_config.weight_path = default_weight_files
            else:
                for (
                    converter_format,
                    converter,
                ) in arch.weight_converters.items():
                    if converter_format_files := weight_files.get(
                        converter_format, []
                    ):
                        pipeline_config.weight_path = converter_format_files
                        pipeline_config._weights_converter = converter
                        break

        if not pipeline_config.weight_path:
            if pipeline_config.quantization_encoding not in [
                SupportedEncoding.bfloat16,
                SupportedEncoding.float32,
            ]:
                msg = f"compatible weights cannot be found for '{pipeline_config.quantization_encoding}' in 'gguf' format, in the provided repo: '{huggingface_weights_repo.repo_id}'"
                raise ValueError(msg)
            else:
                formats = [str(arch.default_weights_format)] + [
                    str(converter)
                    for converter in arch.weight_converters.keys()
                ]
                msg = f"compabible weights cannot be found for '{pipeline_config.quantization_encoding}' in any supported format: [{', '.join(formats)}], in the provided repo: '{huggingface_weights_repo.repo_id}'"
                raise ValueError(msg)

        # Check supported_cache_strategy
        supported_cache_strategies = arch.supported_encodings.get(
            pipeline_config.quantization_encoding, []
        )
        if (
            pipeline_config.cache_strategy == KVCacheStrategy.MODEL_DEFAULT
            and supported_cache_strategies
        ):
            default_strategy = supported_cache_strategies[0]
            msg = f"default cache_strategy of '{default_strategy}' enabled"
            logger.debug(msg)

            pipeline_config.cache_strategy = default_strategy
        elif (
            supported_cache_strategies
            and pipeline_config.cache_strategy not in supported_cache_strategies
        ):
            supported_strategy = supported_cache_strategies[0]

            msg = f"cache_strategy = '{pipeline_config.cache_strategy}' not supported for '{pipeline_config.quantization_encoding}', using '{supported_strategy}' cache strategy."
            logger.warning(msg)

            pipeline_config.cache_strategy = supported_strategy

        # Assume at this point, an architecture,
        # a model_path and weight_paths are available.
        assert pipeline_config.weight_path, "weight_path must be provided."
        for path in pipeline_config.weight_path:
            # Check if file exists locally.
            if not os.path.exists(path):
                # If does not exist locally, verify that it exists on Huggingface.
                if not huggingface_weights_repo.file_exists(str(path)):
                    msg = (
                        f"weight_path: '{path}' does not exist locally, and"
                        f" '{pipeline_config.model_path}/{path}' does"
                        " not exist on HuggingFace."
                    )
                    raise ValueError(msg)

        if pipeline_config.rope_type is None:
            pipeline_config.rope_type = arch.rope_type

        self._estimate_memory_footprint(pipeline_config, arch)

        # If we pass validation ensure, the engine is set as MAX.
        pipeline_config.engine = PipelineEngine.MAX
        return pipeline_config

    def _estimate_memory_footprint(
        self,
        pipeline_config: PipelineConfig,
        arch: SupportedArchitecture,
    ):
        model_cls = arch.pipeline_model

        try:
            free_memory = int(
                sum(d.stats["free_memory"] for d in pipeline_config.devices)
            )
        except Exception as e:
            logger.warning(
                "Unable to estimate memory footprint of model, can't query device stats: "
                + str(e)
            )
            if not pipeline_config.max_batch_size:
                pipeline_config.max_batch_size = 1
            if not pipeline_config.max_length:
                pipeline_config.max_length = model_cls.calculate_max_seq_len(
                    pipeline_config
                )
            return

        model_weights_size = model_cls.estimate_weights_size(pipeline_config)

        total_size = model_weights_size
        available_kv_cache_memory = max(0, free_memory - model_weights_size)
        available_kv_cache_memory = int(
            available_kv_cache_memory
            * pipeline_config.device_memory_utilization
        )

        user_provided_max_length = pipeline_config.max_length is not None
        user_provided_max_batch_size = (
            pipeline_config.max_batch_size is not None
        )
        if not user_provided_max_length:
            pipeline_config.max_length = model_cls.calculate_max_seq_len(
                pipeline_config
            )

        if not user_provided_max_batch_size:
            pipeline_config.max_batch_size = self._infer_optimal_batch_size(
                pipeline_config, model_cls, available_kv_cache_memory
            )

        actual_kv_cache_size = self._calculate_kv_cache_size(
            model_cls,
            pipeline_config,
            available_kv_cache_memory,
        )

        pipeline_config._available_cache_memory = actual_kv_cache_size

        total_size += actual_kv_cache_size

        # If the model is too large to fit in memory, and the user did not
        # specify a max_length, try to infer a value that would fit.
        if total_size > free_memory and not user_provided_max_length:
            original_max_length = pipeline_config.max_length
            (
                found_valid_max_length,
                inferred_max_length,
                _,
            ) = self._find_valid_max_length(
                pipeline_config,
                model_cls,
                available_kv_cache_memory,
                user_provided_max_batch_size,
            )

            if found_valid_max_length:
                logger.warning(
                    f"Truncated model's default max_length from {original_max_length} to {inferred_max_length} to fit in memory."
                )
                pipeline_config.max_length = inferred_max_length
                actual_kv_cache_size = self._calculate_kv_cache_size(
                    model_cls,
                    pipeline_config,
                    available_kv_cache_memory,
                )
                total_size = model_weights_size + actual_kv_cache_size

        if free_memory:
            free_memory_str = f" / {_to_mib(free_memory)} MiB free"

        weights_str = ""
        if model_weights_size:
            weights_str = f"\n\t    Weights:                {_to_mib(model_weights_size)} MiB"

        if not user_provided_max_length:
            max_length_str = f"Auto-inferred max sequence length: {pipeline_config.max_length}"
        else:
            max_length_str = (
                f"Current max sequence length: {pipeline_config.max_length}"
            )

        if not user_provided_max_batch_size:
            max_batch_size_str = f"Auto-inferred max batch size: {pipeline_config.max_batch_size}"
        else:
            max_batch_size_str = (
                f"Current max batch size: {pipeline_config.max_batch_size}"
            )

        logging_str = (
            "\n"
            f"\n\tEstimated memory consumption:"
            f"{weights_str}"
            f"\n\t    KVCache allocation:     {_to_mib(actual_kv_cache_size)} MiB"
            f"\n\t    Total estimated:        {_to_mib(model_weights_size + actual_kv_cache_size)} MiB used{free_memory_str}"
            f"\n\t{max_length_str}"
            f"\n\t{max_batch_size_str}\n"
        )
        logger.info(logging_str)
        vram_usage_limit_scale = 0.95

        if isinstance(free_memory, (int, float)):
            if total_size > free_memory:
                self._raise_oom_error(
                    pipeline_config,
                    user_provided_max_length,
                    user_provided_max_batch_size,
                    model_cls,
                    total_size,
                    free_memory,
                    available_kv_cache_memory,
                    model_weights_size,
                )

            elif total_size > vram_usage_limit_scale * free_memory:
                logger.warning(
                    "Estimated model and kv cache memory use nears available memory. You may experience errors."
                )

    def _raise_oom_error(
        self,
        pipeline_config: PipelineConfig,
        user_provided_max_length: bool,
        user_provided_max_batch_size: bool,
        model_cls: Type[PipelineModel],
        total_size: int,
        original_free_memory: int,
        available_kv_cache_memory: int,
        weights_size: int,
    ) -> None:
        """If we've determined the current configuration won't fit in device memory,
        this method provides a friendly error message suggesting a viable configuration.

        The approach is to:
        1. Binary search max_length until we find a setting that works
        2. If user provided max_batch_size, binary search that too
        3. Generate appropriate suggestions based on this truth table:

                                                            max_length
                                         +----------------------+--------------------------+
                                         | set by user          | set to default           |
                        +----------------+======================+==========================+
                        | set by user    ║ Recommend both       | Recommend max_batch_size |
        max_batch_size  +----------------+----------------------+--------------------------+
                        | set to default ║ Recommend max_length | Recommend both           |
                        +----------------+----------------------+--------------------------+
        """
        if weights_size > original_free_memory:
            raise RuntimeError(
                "Weights size exceeds available memory. Try running a smaller model, using a smaller precision, or using a device with more memory."
            )

        original_max_length = cast(int, pipeline_config.max_length)
        original_max_batch_size = cast(int, pipeline_config.max_batch_size)

        # Find valid configurations through binary search
        (
            found_valid_max_length,
            inferred_max_length,
            inferred_max_length_compatible_batch_size,
        ) = self._find_valid_max_length(
            pipeline_config,
            model_cls,
            available_kv_cache_memory,
            user_provided_max_batch_size,
        )

        pipeline_config.max_batch_size = original_max_batch_size

        found_valid_max_batch_size, inferred_max_batch_size = (
            self._find_valid_batch_size(
                pipeline_config,
                model_cls,
                available_kv_cache_memory,
                original_max_length,
                user_provided_max_batch_size,
            )
        )

        # Generate error message with suggestions
        error_msg = self._generate_oom_error_message(
            total_size=total_size,
            original_free_memory=original_free_memory,
            user_provided_max_length=user_provided_max_length,
            user_provided_max_batch_size=user_provided_max_batch_size,
            found_valid_max_length=found_valid_max_length,
            found_valid_max_batch_size=found_valid_max_batch_size,
            inferred_max_length=inferred_max_length,
            inferred_max_batch_size=inferred_max_batch_size,
            inferred_max_length_compatible_batch_size=inferred_max_length_compatible_batch_size,
            original_max_length=original_max_length,
        )

        raise RuntimeError(error_msg)

    def _find_valid_max_length(
        self,
        pipeline_config: PipelineConfig,
        model_cls: Type[PipelineModel],
        available_kv_cache_memory: int,
        user_provided_max_batch_size: bool,
    ) -> tuple[bool, int, int]:
        """Binary search to find a valid max_length configuration.

        Returns:
            Tuple containing:
            - found_valid_max_length: Whether a valid max_length was found
            - inferred_max_length: The suggested max_length value
            - inferred_max_length_compatible_batch_size: Compatible batch size for the max_length
        """
        assert pipeline_config.max_length is not None
        assert pipeline_config.max_batch_size is not None

        found_valid_max_length = False
        lower = 1
        upper = pipeline_config.max_length
        inferred_max_length = upper

        while not found_valid_max_length:
            inferred_max_length = (lower + upper) // 2
            pipeline_config.max_length = inferred_max_length

            if not user_provided_max_batch_size:
                pipeline_config.max_batch_size = self._infer_optimal_batch_size(
                    pipeline_config, model_cls, available_kv_cache_memory
                )

            kv_cache_size = self._calculate_kv_cache_size(
                model_cls, pipeline_config, available_kv_cache_memory
            )

            if lower > upper:
                break
            elif upper - lower <= 1:
                if kv_cache_size <= available_kv_cache_memory:
                    found_valid_max_length = True
                break

            if kv_cache_size > available_kv_cache_memory:
                upper = inferred_max_length - 1
            else:
                lower = inferred_max_length
        return (
            found_valid_max_length,
            inferred_max_length,
            pipeline_config.max_batch_size,
        )

    def _find_valid_batch_size(
        self,
        pipeline_config: PipelineConfig,
        model_cls: Type[PipelineModel],
        available_kv_cache_memory: int,
        original_max_length: int,
        user_provided_max_batch_size: bool,
    ) -> tuple[bool, int]:
        """Binary search to find a valid batch size configuration.

        Returns:
            Tuple containing:
            - found_valid_max_batch_size: Whether a valid batch size was found
            - inferred_max_batch_size: The suggested batch size value.
                If the user did not provide a batch size, this will be -1.
        """
        if not user_provided_max_batch_size:
            return False, -1

        found_valid_max_batch_size = False
        pipeline_config.max_length = original_max_length
        inferred_max_batch_size = cast(int, pipeline_config.max_batch_size)
        lower = 1
        upper = cast(int, pipeline_config.max_batch_size)

        while not found_valid_max_batch_size:
            inferred_max_batch_size = (lower + upper) // 2
            pipeline_config.max_batch_size = inferred_max_batch_size

            kv_cache_size = self._calculate_kv_cache_size(
                model_cls, pipeline_config, available_kv_cache_memory
            )

            if lower > upper:
                break
            elif upper - lower <= 1:
                if kv_cache_size <= available_kv_cache_memory:
                    found_valid_max_batch_size = True
                break

            if kv_cache_size > available_kv_cache_memory:
                upper = inferred_max_batch_size - 1
            else:
                lower = inferred_max_batch_size

        return found_valid_max_batch_size, inferred_max_batch_size

    def _calculate_kv_cache_size(
        self,
        model_cls: Type[PipelineModel],
        pipeline_config: PipelineConfig,
        available_kv_cache_memory: int,
    ) -> int:
        """Calculate the KV cache size for the current configuration."""
        if issubclass(model_cls, KVCacheMixin):
            return model_cls.estimate_kv_cache_size(
                pipeline_config=pipeline_config,
                available_cache_memory=available_kv_cache_memory,
                devices=pipeline_config.devices,
            )
        return 0

    def _generate_oom_error_message(
        self,
        total_size: int,
        original_free_memory: int,
        user_provided_max_length: bool,
        user_provided_max_batch_size: bool,
        found_valid_max_length: bool,
        found_valid_max_batch_size: bool,
        inferred_max_length: int,
        inferred_max_batch_size: int,
        inferred_max_length_compatible_batch_size: int,
        original_max_length: int,
    ) -> str:
        """Generate an appropriate error message based on the configuration state."""
        free_memory_str = (
            f" / {_to_mib(original_free_memory)} MiB free"
            if original_free_memory
            else ""
        )

        msg = StringIO()
        msg.write(
            f"Estimated model and kv cache memory use exceeds available memory ({_to_mib(total_size)} MiB{free_memory_str}). Try "
        )

        if not found_valid_max_length and not found_valid_max_batch_size:
            msg.write(
                "reducing --max-length or --max-batch-size, finding a smaller model, or using a device with more memory."
            )

        elif user_provided_max_length:
            self._add_user_provided_max_length_suggestions(
                msg,
                user_provided_max_batch_size,
                found_valid_max_length,
                found_valid_max_batch_size,
                inferred_max_length,
                inferred_max_batch_size,
                inferred_max_length_compatible_batch_size,
            )
        else:
            self._add_default_max_length_suggestions(
                msg,
                user_provided_max_batch_size,
                found_valid_max_length,
                found_valid_max_batch_size,
                inferred_max_length,
                inferred_max_batch_size,
                inferred_max_length_compatible_batch_size,
                original_max_length,
            )

        msg.write(".")
        return msg.getvalue()

    def _add_user_provided_max_length_suggestions(
        self,
        msg: StringIO,
        user_provided_max_batch_size: bool,
        found_valid_max_length: bool,
        found_valid_max_batch_size: bool,
        inferred_max_length: int,
        inferred_max_batch_size: int,
        inferred_max_length_compatible_batch_size: int,
    ) -> None:
        """Add error message suggestions when user provided max_length.

        This handles the top row of the truth table from the _raise_oom_error docstring.

        Args:
            msg: StringIO buffer to write message to
            user_provided_max_batch_size: Whether user provided batch size
            found_valid_max_length: Whether valid max_length was found
            found_valid_max_batch_size: Whether valid batch size was found
            inferred_max_length: Suggested max_length value
            inferred_max_batch_size: Suggested batch size value
            inferred_max_length_compatible_batch_size: Compatible batch size for max_length
        """
        if not user_provided_max_batch_size:
            if found_valid_max_length:
                msg.write(
                    f"reducing --max-length to {inferred_max_length} "
                    f"(supports batch size of {inferred_max_length_compatible_batch_size})"
                )
            else:
                msg.write("reducing --max-length or --max-batch-size")
        else:
            if found_valid_max_length:
                msg.write(
                    f"reducing --max-length to {inferred_max_length} and "
                    f"--max-batch-size to {inferred_max_length_compatible_batch_size})"
                )

            if found_valid_max_batch_size:
                if found_valid_max_length:
                    msg.write(" or ")
                msg.write(
                    f"reducing --max-batch-size to {inferred_max_batch_size}"
                )

    def _add_default_max_length_suggestions(
        self,
        msg: StringIO,
        user_provided_max_batch_size: bool,
        found_valid_max_length: bool,
        found_valid_max_batch_size: bool,
        inferred_max_length: int,
        inferred_max_batch_size: int,
        inferred_max_length_compatible_batch_size: int,
        original_max_length: int,
    ) -> None:
        """Add error message suggestions when max_length was set to default.

        This handles the bottom row of the truth table from the _raise_oom_error docstring.

        Args:
            msg: StringIO buffer to write message to
            user_provided_max_batch_size: Whether user provided batch size
            found_valid_max_length: Whether valid max_length was found
            found_valid_max_batch_size: Whether valid batch size was found
            inferred_max_length: Suggested max_length value
            inferred_max_batch_size: Suggested batch size value
            inferred_max_length_compatible_batch_size: Compatible batch size for max_length
            original_max_length: Original max_length value before modifications
        """
        if not user_provided_max_batch_size:
            if found_valid_max_length:
                msg.write(
                    f"setting --max-length to {inferred_max_length} and "
                    f"--max-batch-size to {inferred_max_length_compatible_batch_size})"
                )

            if found_valid_max_batch_size:
                if found_valid_max_length:
                    msg.write(" or ")
                msg.write(
                    f"setting --max-batch-size to {inferred_max_batch_size}"
                )

        else:
            if found_valid_max_batch_size:
                msg.write(
                    f"reducing --max-batch-size to {inferred_max_batch_size}"
                )
            if found_valid_max_length:
                if found_valid_max_batch_size:
                    msg.write(" or ")
                msg.write(
                    f"setting --max-length to {inferred_max_length} "
                    f"(currently defaulted to {original_max_length})"
                )

    def _infer_optimal_batch_size(
        self,
        pipeline_config: PipelineConfig,
        model_cls: Type[PipelineModel],
        available_kv_cache_memory: int,
    ) -> int:
        return model_cls.infer_optimal_batch_size(
            pipeline_config,
            available_kv_cache_memory,
        )

    def _load_logging_message(
        self,
        pipeline_config: PipelineConfig,
        tokenizer_type: Type[PipelineTokenizer],
        pipeline_name: str,
        pipeline_model: str,
        factory: bool,
    ):
        weight_path = ",\n        ".join(
            [
                f"                               {path}"
                for path in pipeline_config.weight_path
            ]
        )
        factory_str = "factory" if factory else ""

        weights_repo_str = (
            f"\n            weights_repo_id:        {pipeline_config._weights_repo_id}"
            if pipeline_config._weights_repo_id
            else ""
        )

        devices_str = ", ".join(
            f"{d.label}[{d.id}]" for d in pipeline_config.devices
        )
        message = f"""

        Loading {tokenizer_type.__name__} and {pipeline_name}({pipeline_model}) {factory_str} for:
            engine:                 {pipeline_config.engine}
            architecture:           {pipeline_config.architecture}
            devices:                {devices_str}
            model_path:             {pipeline_config.model_path}{weights_repo_str}
            quantization_encoding:  {pipeline_config.quantization_encoding}
            cache_strategy:         {pipeline_config.cache_strategy}
            weight_path:            [
        {weight_path}
                                    ]
        """

        return message

    def _set_hf_pipeline_defaults(
        self, pipeline_config: PipelineConfig
    ) -> PipelineConfig:
        if pipeline_config.max_batch_size is None:
            pipeline_config.max_batch_size = 1
        # HF pipelines always use custom continuous cache
        pipeline_config.cache_strategy = KVCacheStrategy.CONTINUOUS
        return pipeline_config

    def retrieve_factory(
        self,
        pipeline_config: PipelineConfig,
        task: PipelineTask = PipelineTask.TEXT_GENERATION,
    ) -> tuple[
        PipelineTokenizer,
        Callable[[], TokenGenerator | EmbeddingsGenerator],
    ]:
        tokenizer: PipelineTokenizer
        pipeline_factory: Callable[[], TokenGenerator | EmbeddingsGenerator]

        # Validate pipeline_config, and update missing values.
        pipeline_config = self.validate_pipeline_config(pipeline_config)
        if pipeline_config.engine == PipelineEngine.MAX:
            # Keep MyPy happy.
            assert pipeline_config.architecture is not None

            pipeline_class = _PIPELINE_TASK_MAP[task]

            # MAX pipeline
            arch = self.architectures[pipeline_config.architecture]
            logger.info(
                self._load_logging_message(
                    pipeline_config=pipeline_config,
                    tokenizer_type=arch.tokenizer,
                    pipeline_model=arch.pipeline_model.__name__,
                    pipeline_name=pipeline_class.__name__,
                    factory=True,
                )
            )

            max_length = arch.pipeline_model.calculate_max_seq_len(
                pipeline_config
            )

            # Old Mistral model like Mistral-7B-Instruct-v0.3 uses LlamaTokenizer
            # and suffers from the whitespace decoding bug. So, we enable the fix
            # for only MistralModel in order to avoid any issues with performance
            # for rest of the models. This can be applied more generically once
            # we have more time verifying this for all the models.
            # More information:
            # https://linear.app/modularml/issue/AIPIPE-197/add-support-for-mistral-7b-instruct-v03
            # TODO: remove this pipeline_model.__name__ check
            if (
                arch.pipeline_model.__name__ in ("MistralModel", "Phi3Model")
                and arch.tokenizer is TextTokenizer
            ):
                text_tokenizer = cast(Type[TextTokenizer], arch.tokenizer)
                tokenizer = text_tokenizer(
                    pipeline_config.model_path,
                    max_length,
                    pipeline_config.max_new_tokens,
                    pipeline_config.trust_remote_code,
                    enable_llama_whitespace_fix=True,
                )
            else:
                tokenizer = arch.tokenizer(
                    pipeline_config.model_path,
                    max_length,
                    pipeline_config.max_new_tokens,
                    pipeline_config.trust_remote_code,
                )

            pipeline_factory = functools.partial(
                pipeline_class,
                pipeline_config=pipeline_config,
                pipeline_model=arch.pipeline_model,
                eos_token_id=tokenizer.eos,
            )
        else:
            pipeline_config = self._set_hf_pipeline_defaults(pipeline_config)
            hf_pipeline_class = _HF_PIPELINE_TASK_MAP[task]

            torch_device_type = str(pipeline_config.device_specs[0].device_type)
            if pipeline_config.device_specs[0].device_type == "gpu":
                torch_device_type = "cuda"
                torch.multiprocessing.set_start_method("spawn", force=True)

            # Generalized pipeline
            tokenizer = TextTokenizer(
                pipeline_config.model_path,
                pipeline_config.max_length,
                pipeline_config.max_new_tokens,
                pipeline_config.trust_remote_code,
                enable_llama_whitespace_fix=True,
            )
            logger.info(
                self._load_logging_message(
                    pipeline_config=pipeline_config,
                    tokenizer_type=TextTokenizer,
                    pipeline_model="",
                    pipeline_name=hf_pipeline_class.__name__,
                    factory=True,
                )
            )
            pipeline_factory = functools.partial(
                hf_pipeline_class,
                pipeline_config=pipeline_config,
                torch_device_type=torch_device_type,
            )

        if tokenizer.eos is None:
            msg = "tokenizer.eos value is None, tokenizer configuration is incomplete."
            raise ValueError(msg)

        return tokenizer, pipeline_factory

    def retrieve(
        self,
        pipeline_config: PipelineConfig,
        task: PipelineTask = PipelineTask.TEXT_GENERATION,
    ) -> tuple[PipelineTokenizer, TokenGenerator | EmbeddingsGenerator]:
        tokenizer, pipeline_factory = self.retrieve_factory(
            pipeline_config, task
        )
        return tokenizer, pipeline_factory()

    def reset(self) -> None:
        self.architectures.clear()


PIPELINE_REGISTRY = PipelineRegistry([])
