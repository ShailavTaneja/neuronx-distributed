# coding=utf-8
# Copyright (c) 2019 NVIDIA CORPORATION. All rights reserved.
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc. team.
# Modifications Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import os
import random
import time

import numpy as np
import torch
import torch.distributed as dist
import torch_xla.core.xla_model as xm
import torch_xla.distributed.xla_multiprocessing as xmp
from neuronx_distributed.parallel_layers.parallel_state import (
    get_data_parallel_rank,
    get_data_parallel_size,
    get_tensor_model_parallel_rank,
    initialize_model_parallel,
)
from neuronx_distributed.parallel_layers.grads import clip_grad_norm
from neuronx_distributed.pipeline import NxDPPModel
from neuronx_distributed.parallel_layers import parallel_state
from neuronx_distributed.optimizer import NeuronZero1Optimizer
from neuronx_distributed.parallel_layers import mappings
from neuronx_distributed.parallel_layers.utils import is_pjrt_device
from transformers import LlamaConfig
import transformers.modeling_utils as modeling_utils
try:
    from torchdistx import deferred_init
except ImportError:
    deferred_init = None


from common_utils.modeling_llama_nxd import LlamaForCausalLM, LlamaRMSNorm, LlamaDecoderLayer

from common_utils.optimizer.adamw_fp32_optim_params import AdamW_FP32OptimParams
from activation_checkpoint import apply_checkpoint
from training_utils import get_param_groups_by_weight_decay, get_learning_rate_scheduler, create_llama_pretraining_dataset


def allreduce_sequence_parallel_gradients(optimizer):
    """ All-reduce layernorm parameters across model parallel nodes when sequence parallelism is used.
        Modified from megatron-lm:
        https://gitlab-master.nvidia.com/ADLR/megatron-lm/-/blob/3f91f09bb2ab32f9904b47f46f19d2fc3f518ed8/megatron/training.py#L425
    """
    from neuronx_distributed.parallel_layers.mappings import reduce_from_tensor_model_parallel_region
    grads = []
    for param_group in optimizer.__getstate__()['param_groups']:
        for group, params in param_group.items():
            if group == 'params':
                for p in params:
                    if isinstance(p, torch.Tensor) and p.grad is not None:
                        sequence_parallel_param = getattr(p, 'sequence_parallel_enabled', False)
                        if sequence_parallel_param:
                            grads.append(p.grad.data)
    xm.master_print("# sequence parallel parameters = ", len(grads))
    for grad in grads:
        # sum v.s. average: sum
        reduce_from_tensor_model_parallel_region(grad)

def create_partition(config, args):
    """
    Evenly split the transformer layers between the PP ranks
    """
    assert config.num_hidden_layers % args.pipeline_parallel_size == 0
    num_layer_per_partition = config.num_hidden_layers  // args.pipeline_parallel_size
    pipeline_cuts = []
    current_cut = num_layer_per_partition - 1
    for i in range(args.pipeline_parallel_size-1):
        pipeline_cuts.append(f"model.layers.{current_cut}")
        current_cut += num_layer_per_partition
    if torch.distributed.get_rank() == 0:
        print(f"pipeline_cuts {pipeline_cuts}")
    return pipeline_cuts

def train_llama(args):
    gpu_losses_to_compare = torch.load("70B_4layers_hidden2048_gpu_loss.pt")
    golden_steploss = len(gpu_losses_to_compare)
    if dist.get_rank() == 0:
        print(f"args {args}")
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    random.seed(args.seed)
    initialize_model_parallel(
        pipeline_model_parallel_size=args.pipeline_parallel_size,
        tensor_model_parallel_size=args.tensor_parallel_size,
    )
    dp_rank = get_data_parallel_rank()
    dp_size = get_data_parallel_size()
    tp_rank = get_tensor_model_parallel_rank()

    config = LlamaConfig.from_pretrained(args.training_config)
    config.use_cache = False
    config.return_dict = False
    config.sequence_parallel_enabled = args.use_sequence_parallel > 0
    config.selective_checkpoint_enabled = args.use_selective_checkpoint > 0
    config.max_position_embeddings = max(config.max_position_embeddings, args.seq_len)
    if args.num_layer != -1:
        config.num_hidden_layers = args.num_layer
    if args.hidden_size != -1:
        config.hidden_size = args.hidden_size
    if args.use_deferred_init > 0 and deferred_init is not None:
        model = deferred_init.deferred_init(LlamaForCausalLM, config)
    else:
        model = LlamaForCausalLM(config)
    num_params = sum([np.prod(p.size()) for p in model.parameters()])
    if dist.get_rank() == 0:
        print(f"# total parameters: {num_params}")
        print(f"model config {config}")
    pipeline_cuts = create_partition(config, args)
    model = NxDPPModel(
        model,
        transformer_layer_cls=LlamaDecoderLayer,
        num_microbatches=args.num_microbatches,
        output_loss_value_spec=(True, False),
        input_names=["input_ids", "attention_mask", "labels"],
        pipeline_cuts=pipeline_cuts,
        trace_file_path=args.trace_file_path,
        param_init_fn=None,
        leaf_module_cls=[LlamaRMSNorm.__name__],
        autowrap_modules=[mappings],
        use_zero1_optimizer=args.use_zero1_optimizer > 0,
    )
    if not config.selective_checkpoint_enabled:
        apply_checkpoint(model)
    model.move_model_to_device()

    param_groups = get_param_groups_by_weight_decay(model)
    if args.use_zero1_optimizer > 0:
        if args.use_fp32_optimizer > 0:
            opt_cls = AdamW_FP32OptimParams
        else:
            opt_cls = torch.optim.AdamW
        optimizer = NeuronZero1Optimizer(
                param_groups,
                opt_cls,
                lr=args.lr,
                pin_layout=False,
                sharding_groups=parallel_state.get_data_parallel_group(as_list=True),
            )
    elif args.use_fp32_optimizer > 0:
        optimizer = AdamW_FP32OptimParams(
            param_groups, betas=(args.beta1, args.beta2), lr=args.lr, weight_decay=args.weight_decay
        )
    else:
        optimizer = torch.optim.AdamW(
            param_groups, betas=(args.beta1, args.beta2), lr=args.lr, weight_decay=args.weight_decay
        )

    lr_scheduler = get_learning_rate_scheduler(optimizer, args)
    train_dataloader = create_llama_pretraining_dataset(args.training_dir, args.train_batch_size, args.seed, dp_size, dp_rank)
    print("Creating sample dataloader finised")


    total_steps = 0
    should_print = (
        model.pipeline_parallel_rank == args.pipeline_parallel_size - 1 and dp_rank == 0 and tp_rank == 0
    )
    if should_print and args.tb_dir != "":
        from torch.utils.tensorboard import SummaryWriter
        tb_dir = args.tb_dir
        import os
        import shutil

        exist = os.path.exists(tb_dir)
        if exist:
            shutil.rmtree(tb_dir)
        writer = SummaryWriter(log_dir=tb_dir)
    else:
        writer = None

    epoch = 0
    while True:
        if torch.distributed.get_rank() == 0:
            print(f"Epoch {epoch}")
        for batch_idx, batch in enumerate(train_dataloader):
            start = time.time()
            input_ids = batch["input_ids"]
            attention_mask = batch["attention_mask"]
            labels = batch["labels"]
            with torch.autocast(enabled=args.use_amp > 0, dtype=torch.bfloat16, device_type="cuda"):
                loss = model.run_train(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    labels=labels,
                )
            total_steps += 1
            if config.sequence_parallel_enabled:
                allreduce_sequence_parallel_gradients(optimizer)
            if args.use_zero1_optimizer == 0:
                global_norm = clip_grad_norm(model.parameters(), 1.0)
            else:
                global_norm = None
            optimizer.step()
            optimizer.zero_grad()
            lr_scheduler.step()
            xm.mark_step()
            if should_print:
                end = time.time()
                iteration_time = end - start
                print(
                    f"step {total_steps} step_time {iteration_time}s loss {loss.detach().cpu().item()} grad norm {global_norm.item() if global_norm is not None else None}"
                )
                if args.compare_loss:
                    if total_steps < golden_steploss:
                        if not torch.allclose(loss.detach().cpu().float(), gpu_losses_to_compare[total_steps].float(), rtol=2.3e-1):
                            raise RuntimeError(f"Loss mismtach with golden, Trn {loss.item()} GPU {gpu_losses_to_compare[total_steps].item()}")
                if writer is not None:
                    current_lr = lr_scheduler.get_lr()[0]
                    writer.add_scalar("loss", loss.item(), total_steps)
                    if global_norm is not None:
                        writer.add_scalar(
                            "global_norm", global_norm.item(), total_steps
                        )
                    writer.add_scalar("lr", current_lr, total_steps)
                    writer.add_scalar("iteration_time", iteration_time, total_steps)
                    writer.add_scalar(
                        "input_ids",
                        torch.sum(input_ids.detach().cpu()).item(),
                        total_steps,
                    )

            if total_steps >= args.max_steps:
                break

        if total_steps >= args.max_steps:
            break
        epoch += 1

    print("Training finished successfully")

def _mp_fn(index, args):
    train_llama(args)
    xm.rendezvous("_mp_fn finished")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--num_microbatches", type=int, default=8, help="num_microbatches")
    parser.add_argument("--tensor_parallel_size", type=int, default=8, help="tensor_parallel_size")
    parser.add_argument("--num_layer", type=int, default=-1, help="override model number of layers")
    parser.add_argument("--hidden_size", type=int, default=-1, help="override model model hidden size")
    parser.add_argument("--train_batch_size", type=int, default=16, help="batch size")
    parser.add_argument("--pipeline_parallel_size", type=int, default=1, help="PP size")
    parser.add_argument("--seq_len", type=int, default=4096, help="PP size")
    parser.add_argument("--training_dir", type=str, default=None)
    parser.add_argument("--training_config", type=str, default=None)
    parser.add_argument("--trace_file_path", type=str, default=None)
    parser.add_argument("--tb_dir", type=str, default="")
    parser.add_argument("--max_steps", type=int, default=100, help="max steps")
    parser.add_argument("--checkpoint_freq", type=int, default=100000, help="save checkpoint freq")
    parser.add_argument("--checkpoint_dir", type=str, default=None)
    parser.add_argument("--loading_step", type=int, default=-1, help="load from step, -1 means no load")

    parser.add_argument("--compare_loss", type=int, default=0, help="compare loss with golden")

    # optimization
    opt_grp = parser.add_argument_group(title="optimization", description="arguments for optimization")
    opt_grp.add_argument("--weight_decay", default=0.01, type=float, help="weight decay")
    opt_grp.add_argument("--beta1", default=0.9, type=float, help="beta1 parameter for Adam optimizer")
    opt_grp.add_argument("--beta2", default=0.95, type=float, help="beta2 parameter for Adam optimizer")
    opt_grp.add_argument("--use_fp32_optimizer", default=0, type=int, help="use_fp32_optimizer")
    opt_grp.add_argument("--use_zero1_optimizer", default=0, type=int, help="use_zero1_optimizer")
    opt_grp.add_argument("--seed", default=1234, type=int, help="random seed")
    opt_grp.add_argument("--use_amp", default=0, type=int, help="use amp data")
    opt_grp.add_argument("--use_deferred_init", default=0, type=int, help="use amp data")
    opt_grp.add_argument("--use_selective_checkpoint", default=0, type=int, help="enable selective activation checkpointing")
    opt_grp.add_argument("--use_sequence_parallel", default=1, type=int, help="enable sequence parallelism")

    # learning rate
    lr_grp = parser.add_argument_group(title="lr", description="arguments for learning rate schedule")
    lr_grp.add_argument("--lr", type=float, default=None, help="Initial learning rate.")
    lr_grp.add_argument("--warmup_steps",type=int,default=None,help="number of warmup_steps")
    lr_grp.add_argument("--constant_steps",type=int,default=None,help="number of warmup_steps")
    lr_grp.add_argument("--min_lr",type=float,default=None,help="Minumum value for learning rate. The scheduler" "clip values below this threshold.")

    args, _ = parser.parse_known_args()
    if os.environ.get("XLA_USE_BF16") or os.environ.get("XLA_DOWNCAST_BF16") or args.use_amp > 0:
        modeling_utils.get_parameter_dtype = lambda x: torch.bfloat16

    if os.environ.get("WORLD_SIZE"):
        if is_pjrt_device():
            import torch_xla.experimental.pjrt_backend # noqa
            dist.init_process_group("xla", init_method="pjrt://")
        else:
            dist.init_process_group("xla")
        _mp_fn(0, args)
    else:
        xmp.spawn(_mp_fn, args=(args,))

