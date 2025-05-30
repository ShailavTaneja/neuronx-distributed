# Note : This file location may change in future.
import argparse
import json
import os
import re

from numpy import format_float_scientific

import torch
import torch_xla.utils.serialization as xser

from neuronx_distributed.pipeline.partition import (
    create_partitions,
    stage_to_pipeline_parallel_rank,
)
from neuronx_distributed.trainer.checkpoint import _xser_load_data
from neuronx_distributed.trainer.checkpoint_storage import BaseCheckpointStorage, create_checkpoint_storage
from neuronx_distributed.scripts.yaml_converter import convert_yaml_to_json


class CheckpointConverterBase:

    # ParallelEmbedding
    embedding_partition_dim = 0
    # ColumnParallelLinear or GQAQKVColumnParallelLinear
    qkv_partition_dim = 0
    # ColumnParallelLinear
    gate_up_proj_partition_dim = 0
    # RowParallelLinear
    down_proj_partition_dim = 1
    # RowParallelLinear
    o_proj_partition_dim = 1

    def get_partition_dim(self, name):
        if "embed_tokens" in name or "lm_head" in name:
            partition_dim = self.embedding_partition_dim
        elif self.is_qkv_weight(name):
            partition_dim = self.qkv_partition_dim
        elif "gate_proj" in name or "up_proj" in name or "gate_up_proj" in name:
            partition_dim = self.gate_up_proj_partition_dim
        elif "down_proj" in name:
            partition_dim = self.down_proj_partition_dim
        elif "o_proj" in name:
            partition_dim = self.o_proj_partition_dim
        else:
            raise AssertionError(f"Unknown partition_dim for {name}")
        return partition_dim

    # QKV Helper functions
    def get_hf_to_nxd_model_keys(self, qkv_linear=True, is_gqa=True):
        if qkv_linear:
            keys_hf_to_nxd = {
                "q_proj.weight": "qkv_proj.weight_q",
                "k_proj.weight": "qkv_proj.weight_k",
                "v_proj.weight": "qkv_proj.weight_v",
            }
        elif is_gqa: # shouldnt hit this case as it qkv linear is used for gqa
            keys_hf_to_nxd = {
                "q_proj.weight": "q_proj.weight",
                "k_proj.weight": "k_proj.weight",
                "v_proj.weight": "v_proj.weight",
            }
        else:
            keys_hf_to_nxd = {
                "q_proj.weight": "qkv_proj.weight",
                "k_proj.weight": "qkv_proj.weight",
                "v_proj.weight": "qkv_proj.weight",
            }
        keys_nxd_to_hf = {v: k for k, v in keys_hf_to_nxd.items()}
        return keys_hf_to_nxd, keys_nxd_to_hf


    def download_and_save_hf_model(self, model_identifier, config_path=None):
        from getpass import getpass
        from huggingface_hub import login

        from transformers import AutoModelForCausalLM, AutoConfig

        token = getpass("Enter your Hugging Face API token: (If you don't have one, create it at https://huggingface.co/settings/tokens): ")
        login(token=token)

        print(f"Downloading model: {model_identifier}")
        try:
            # Download the model
            config = None
            if config_path:
                config = AutoConfig.from_pretrained(config_path)
            model = AutoModelForCausalLM.from_pretrained(model_identifier, token=token, config=config)
            return model.state_dict()
        except Exception as e:
            print(f"An error occurred: {str(e)}")

    def get_fused_qkv_key(self):
        return "qkv_proj.weight_qkv"

    def is_qkv_weight(self, name):
        return "q_proj" in name or "k_proj" in name or "v_proj" in name or "qkv_proj" in name or "query_key_value" in name

    def coalesce_qkv(self, state_dict, config, tp_degree):
        for i in range(config["num_hidden_layers"]):
            q = state_dict.pop(f"model.layers.{i}.self_attn.q_proj.weight")
            k = state_dict.pop(f"model.layers.{i}.self_attn.k_proj.weight")
            v = state_dict.pop(f"model.layers.{i}.self_attn.v_proj.weight")
            partition_size = config["hidden_size"] // tp_degree
            tp_partititons = []
            for tp_rank in range(tp_degree):
                q_split = q.narrow(0, tp_rank * partition_size, partition_size).detach().clone()
                k_split = k.narrow(0, tp_rank * partition_size, partition_size).detach().clone()
                v_split = v.narrow(0, tp_rank * partition_size, partition_size).detach().clone()
                tp_partititons.append(torch.cat([q_split, k_split, v_split], dim=self.qkv_partition_dim))

            state_dict[f"model.layers.{i}.self_attn.qkv_proj.weight"] = torch.cat(tp_partititons, dim=self.qkv_partition_dim)

        return state_dict

    def get_weight_key(self, keys_hf_to_nxd, keys_nxd_to_hf, name, hf_to_nxd):
        if not self.is_qkv_weight(name):
            return name

        keys = keys_hf_to_nxd if hf_to_nxd else keys_nxd_to_hf
        return ".".join(name.split(".")[:-2]) + "." + keys[".".join(name.split(".")[-2:])]

    def rename_keys_for_megatron(self, key, model_style, hf_to_nxdt=False):
        if model_style != 'megatron':
            return key

        megatron_name_to_hf_name = {
            'language_model.embedding.word_embeddings.weight' : 'model.embed_tokens.weight',
            'language_model.encoder.final_layernorm.weight' : 'model.norm.weight',
            'language_model.encoder.' : 'model.',
            'self_attention.' : 'self_attn.',
            'core_attention.rotary_emb.inv_freq' : 'rotary_emb.inv_freq',
            'dense.weight' : 'o_proj.weight',
            'dense_h_to_4h.weight' : 'gate_up_proj.weight',
            'dense_4h_to_h.weight' : 'down_proj.weight',
            'language_model.output_layer.weight' : 'lm_head.weight',
            'query_key_value.weight' : 'qkv_proj.weight'
        }

        def check_replace_complete(strings, key, meg_str, hf_str):
            for string in strings:
                if string in key:
                    if string in meg_str+hf_str : # check whther its present atleast in one of them
                        return True
            return False

        for meg_str,hf_str in megatron_name_to_hf_name.items():
            if not hf_to_nxdt:
                key = key.replace(meg_str,hf_str)
            else:
                key = key.replace(hf_str,meg_str)
            if check_replace_complete(['embed','final_layernorm','model.norm'], key, meg_str, hf_str):
                break

        return key

    def modify_qkv_for_megatron(self,partial_state,args):
        if args.model_style != 'megatron':
            return
        if not args.qkv_linear:
            if args.convert_from_full_state:
                # merge k_proj,q_proj and v_proj to query_key_value.weight
                pkeys = list(partial_state.keys())
                for key in pkeys:
                    if 'q_proj' in key:
                        q = partial_state[key]
                        k = partial_state[key.replace('q_proj','k_proj')]
                        v = partial_state[key.replace('q_proj','v_proj')]
                        partial_state[key.replace('q_proj','query_key_value')] = torch.cat((q,k,v),dim=0).detach().clone()
                        del partial_state[key], partial_state[key.replace('q_proj','k_proj')], partial_state[key.replace('q_proj','v_proj')]
        else:
            if args.convert_from_full_state:
                # Opposite of :: query.weight and key_value.weight to qkv_proj.weight_q, qkv_proj.weight_k, qkv_proj.weight_v
                pkeys = list(partial_state.keys())
                for key in pkeys:
                    # Reverse weight projection renaming
                    original_key = None
                    if 'query_key_value.weight_q' in key:
                        original_key = key.replace('query_key_value.weight_q', 'query.weight')
                        partial_state[original_key] = partial_state[key].detach().clone()
                        del partial_state[key]
                    # Reverse weight tensor splitting
                    elif ('query_key_value.weight_k' in key or 'query_key_value.weight_v' in key) and key in partial_state: # partial state will not have removed keys
                        if key.endswith('query_key_value.weight_k'):
                            weight_k_key = key
                            weight_v_key = key.replace('query_key_value.weight_k', 'query_key_value.weight_v')
                            original_key = key.replace('query_key_value.weight_k', 'key_value.weight')
                        else:
                            weight_k_key = key.replace('query_key_value.weight_v', 'query_key_value.weight_k')
                            weight_v_key = key
                            original_key = key.replace('query_key_value.weight_v', 'key_value.weight')
                        combined_tensor = torch.cat([partial_state[weight_k_key], partial_state[weight_v_key]], dim=0)
                        partial_state[original_key] = combined_tensor.detach().clone()
                        del partial_state[weight_k_key]
                        del partial_state[weight_v_key]
                        print(f"{original_key=},{key=}")
            else:
                # query.weight and key_value.weight to qkv_proj.weight_q, qkv_proj.weight_k, qkv_proj.weight_v
                pkeys = list(partial_state.keys())
                for key in pkeys:
                    if 'query.weight' in key:
                        partial_state[key.replace('query.weight','qkv_proj.weight_q')] = partial_state[key].detach().clone()
                        del partial_state[key]
                    elif 'key_value.weight' in key:
                        split_size = partial_state[key].size(0) // 2
                        tensor1, tensor2 = torch.split(partial_state[key], split_size, dim=0)
                        partial_state[key.replace('key_value.weight','qkv_proj.weight_k')] = tensor1.detach().clone()
                        partial_state[key.replace('key_value.weight','qkv_proj.weight_v')] = tensor2.detach().clone()
                        del partial_state[key]

    def is_q_or_o_for_megatron(self, args, name):
        if args.model_style != 'megatron':
            return False
        if 'q' in name or 'o_proj' in name: # since is_qkv_weight is already checked a simple 'q' is good enough.
            # Since GQA doesnt support replication we will return true here and then do direct torch.cat without worrying about shuffling
            return True
        return False


    def find_size(self, size_total):
        # Divide the total size by 3 to get the q,k,v component sizes, which are of equal size.
        size = size_total / 3
        return int(size)

    # Find the fused qkv weight in the partial state and split it into q,k,v components.
    # Update the partial state accordingly
    def convert_partial_state_to_non_fused_qkv(self, partial_state, keys_nxd_to_hf, kv_size_multiplier, num_hidden_layers):
        qkv_key = self.get_fused_qkv_key()
        pattern = re.compile(rf"model\.layers\.(\d+)\.self_attn\.{re.escape(qkv_key)}")
        matching_keys, layer_numbers = [], []
        for key in partial_state.keys():
            match = pattern.match(key)
            if match:
                matching_keys.append(key)
                layer_numbers.append(int(match.group(1)))
        for i, mkey in zip(layer_numbers, matching_keys):
            qkv = partial_state.pop(mkey)
            size = self.find_size(qkv.size(0))
            q, k, v = torch.split(qkv, (size, size, size), dim=0)

            # Set q,k,v in partial state
            q_key = next(key for key in keys_nxd_to_hf.keys() if "weight_q" in key)
            k_key = next(key for key in keys_nxd_to_hf.keys() if "weight_k" in key)
            v_key = next(key for key in keys_nxd_to_hf.keys() if "weight_v" in key)
            partial_state[f"model.layers.{i}.self_attn.{q_key}"] = q
            partial_state[f"model.layers.{i}.self_attn.{k_key}"] = k
            partial_state[f"model.layers.{i}.self_attn.{v_key}"] = v
        return partial_state

    # Take the individual q,k,v components and concat them
    # Update the partial state accordingly
    def convert_partial_state_to_fused_qkv(self, partial_state, keys_nxd_to_hf, num_hidden_layers):
        qkv_key = "qkv_proj.weight_q"
        pattern = re.compile(rf"model\.layers\.(\d+)\.self_attn\.{re.escape(qkv_key)}")
        layer_numbers = [int(match) for match in re.findall(pattern, ' '.join(partial_state.keys()))]
        for i in layer_numbers:
            q_key = next(key for key in keys_nxd_to_hf.keys() if "weight_q" in key)
            k_key = next(key for key in keys_nxd_to_hf.keys() if "weight_k" in key)
            v_key = next(key for key in keys_nxd_to_hf.keys() if "weight_v" in key)
            q = partial_state.pop(f"model.layers.{i}.self_attn.{q_key}")
            k = partial_state.pop(f"model.layers.{i}.self_attn.{k_key}")
            v = partial_state.pop(f"model.layers.{i}.self_attn.{v_key}")
            qkv = torch.cat([q, k, v], dim=0)
            qkv_key = self.get_fused_qkv_key()
            partial_state[f"model.layers.{i}.self_attn.{qkv_key}"] = qkv
        return partial_state

    # Helper function for convert_to_full_state()
    def merge_tp_checkpoints(self, args):
        full_state = {}
        with open(args.config, "r") as f:
            config = json.load(f)
        q_heads = config["num_attention_heads"]
        kv_heads = config["num_key_value_heads"]
        head_dim = config["hidden_size"] // q_heads
        is_gqa = q_heads != kv_heads
        keys_hf_to_nxd, keys_nxd_to_hf = self.get_hf_to_nxd_model_keys(args.qkv_linear, is_gqa)

        for tp_rank in range(args.tp_size):
            for pp_rank in range(args.pp_size):
                for ep_rank in range(args.ep_size):
                    if args.load_xser:
                        partial_state = self.load_partial_xser(args, tp_rank, pp_rank, ep_rank)
                    else:
                        partial_state = self.load_partial_no_xser(args, tp_rank, pp_rank, ep_rank)
                    pkeys = list(partial_state.keys())
                    for key in pkeys:
                        partial_state[self.rename_keys_for_megatron(key, args.model_style, hf_to_nxdt=False)] = partial_state[key].cpu()
                        if args.model_style=='megatron':
                            del partial_state[key]
                    self.modify_qkv_for_megatron(partial_state, args) # dict so gets auto modified.
                    if args.model_key is not None and args.model_key in partial_state:
                        partial_state = partial_state[args.model_key]
                    if args.fuse_qkv:
                        partial_state = self.convert_partial_state_to_non_fused_qkv(partial_state, keys_nxd_to_hf, args.kv_size_multiplier, args.n_layers)
                    for name, param in partial_state.items():
                        if (self.is_qkv_weight(name) or "o_proj" in name) and args.qkv_linear:
                            # qkv_proj would be a key if we are using the QKVLinear layer
                            partition_dim = self.get_partition_dim(name)
                            name = self.get_weight_key(keys_hf_to_nxd, keys_nxd_to_hf, name, False)

                            if name not in full_state:
                                full_state[name] = []

                            full_state[name].append(param)
                            if tp_rank != (args.tp_size - 1):
                                continue

                            full_weight = torch.cat(full_state[name], dim=partition_dim)
                            if "k" in name or "v" in name or self.is_q_or_o_for_megatron(args,name): # no kv replication in megatron so q needs to be appended directly
                                # If kv_multiplier is set, the kv heads are repeated. So we need to
                                # take only the first chunk
                                full_state[name] = torch.chunk(full_weight, args.kv_size_multiplier)[0].detach().clone()
                            else:
                                """
                                Since we do the replication of KV heads, the Q heads are placed as:

                                Example: num_heads = 64, num_kv_heads = 16, kv_size_multiplier= 4
                                For TRN1 (interleaved KV replication):
                                - KV pattern: (K0,K1,...,K15)(K0,K1,...,K15)... repeated 4 times
                                - Query order: Q0,Q16,Q32,Q48, Q1,Q17,Q33,Q49, ..., Q15,Q31,Q47,Q63
                                This groups 4 query heads (e.g., Q0,Q16,Q32,Q48) with each KV head,
                                repeating 4 times to match the KV replication.

                                For TRN2 (blocked KV replication):
                                - KV pattern: (K0,K0,K0,K0)(K1,K1,K1,K1)...(K15,K15,K15,K15)
                                - Query order: Q0,Q1,Q2,Q3, Q4,Q5,Q6,Q7, ..., Q60,Q61,Q62,Q63
                                This groups 4 consecutive query heads with each replicated KV head.

                                Hence when creating the merged checkpoint, we need to bring the Q heads and o_proj in order.
                                Reordering query heads to align with the KV head replication pattern.
                                """
                                if "o_proj" in name:
                                    # The shuffling is same for both o_proj and q, but o_proj is sharded on column.
                                    # Hence to reuse the same shuffling code, we just transpose, do the shuffling and
                                    # transpose back
                                    full_weight = torch.transpose(full_weight, 0, 1)
                                weights = full_weight.reshape(q_heads, head_dim, -1)
                                weights_shape = weights.size()
                                weight_splits = []
                                if args.hw_backend == "trn2":
                                    group_size = q_heads // kv_heads
                                    weights = weights.reshape(kv_heads, group_size, head_dim, weights_shape[-1])
                                    for i in range(kv_heads):
                                        weight_splits.append(weights[i].reshape(-1, weights_shape[-1]))
                                else:
                                    weights = weights.reshape(
                                        -1, q_heads // (kv_heads * args.kv_size_multiplier), head_dim, weights_shape[-1]
                                    )
                                    indicies = torch.arange(0, args.tp_size // kv_heads) * kv_heads
                                    for i in range(kv_heads):
                                        weight_splits.append(weights[indicies + i].reshape(-1, weights_shape[-1]))
                                full_weight = torch.cat(weight_splits, dim=self.qkv_partition_dim)
                                full_state[name] = (
                                    torch.transpose(full_weight, 0, 1).detach().clone()
                                    if "o_proj" in name
                                    else full_weight.detach().clone()
                                )
                        elif "qkv_proj" in name and not is_gqa:
                            partition_dim = self.get_partition_dim(name)
                            partition_size = config["hidden_size"] // args.tp_size
                            q, k, v = torch.split(param, partition_size, dim=partition_dim)
                            q_name = name.replace("qkv", "q")
                            k_name = name.replace("qkv", "k")
                            v_name = name.replace("qkv", "v")
                            for name, weight in zip([q_name, k_name, v_name], [q, k, v]):
                                if name not in full_state:
                                    full_state[name] = []
                                full_state[name].append(weight)
                                if tp_rank == (args.tp_size - 1):
                                    full_weight = torch.cat(full_state[name], dim=partition_dim)
                                    full_state[name] = full_weight.detach().clone()
                        elif (
                            "embed_tokens" in name
                            or self.is_qkv_weight(name)
                            or "o_proj" in name
                            or "lm_head" in name
                        ):
                            partition_dim = self.get_partition_dim(name)
                            name = self.get_weight_key(keys_hf_to_nxd, keys_nxd_to_hf, name, False)
                            if name not in full_state:
                                full_state[name] = []
                            full_state[name].append(param)
                            if tp_rank == (args.tp_size - 1):
                                full_weight = torch.cat(full_state[name], dim=partition_dim)
                                full_state[name] = full_weight.detach().clone()
                        elif "down_proj" in name:
                            partition_dim = self.get_partition_dim(name)
                            expert_partition_dim = 0
                            name = self.get_weight_key(keys_hf_to_nxd, keys_nxd_to_hf, name, False)
                            if name not in full_state:
                                full_state[name] = [[]]
                            full_state[name][tp_rank].append(param)
                            if ep_rank == (args.ep_size - 1):
                                full_weight = torch.cat(full_state[name][tp_rank], dim=expert_partition_dim)
                                full_state[name][tp_rank] = full_weight.detach().clone()
                                if tp_rank != (args.tp_size - 1):
                                    full_state[name].append([])
                                else:
                                    full_weight = torch.cat(full_state[name], dim=partition_dim)
                                    full_state[name] = full_weight
                        elif "gate_up_proj" in name:
                            partition_dim = self.get_partition_dim(name)
                            expert_partition_dim = 0
                            dim_size = param.size()[partition_dim] // 2
                            gate_proj_name = name.replace("gate_up_proj", "gate_proj")
                            up_proj_name = name.replace("gate_up_proj", "up_proj")
                            gate_proj_weight = param.narrow(partition_dim, 0, dim_size).detach().clone()
                            up_proj_weight = param.narrow(partition_dim, dim_size, dim_size).detach().clone()
                            if gate_proj_name not in full_state:
                                full_state[gate_proj_name] = [[]]
                            if up_proj_name not in full_state:
                                full_state[up_proj_name] = [[]]
                            full_state[gate_proj_name][tp_rank].append(gate_proj_weight)
                            full_state[up_proj_name][tp_rank].append(up_proj_weight)
                            if ep_rank == (args.ep_size - 1):
                                full_gate_proj_weight = torch.cat(full_state[gate_proj_name][tp_rank], dim=expert_partition_dim)
                                full_up_proj_weight = torch.cat(full_state[up_proj_name][tp_rank], dim=expert_partition_dim)
                                full_state[gate_proj_name][tp_rank] = full_gate_proj_weight
                                full_state[up_proj_name][tp_rank] = full_up_proj_weight
                                if tp_rank != args.tp_size - 1:
                                    full_state[gate_proj_name].append([])
                                    full_state[up_proj_name].append([])
                                else:
                                    full_gate_proj_weight = torch.cat(full_state[gate_proj_name], dim=partition_dim)
                                    full_up_proj_weight = torch.cat(full_state[up_proj_name], dim=partition_dim)
                                    full_state[gate_proj_name] = full_gate_proj_weight
                                    full_state[up_proj_name] = full_up_proj_weight

                        elif "expert_mlps" in name:
                            if name not in full_state:
                                full_state[name] = []
                            full_state[name].append(param)
                            if ep_rank == args.ep_size - 1:
                                expert_dim = 0
                                full_state[name] = torch.cat(full_state[name], dim=expert_dim)
                        else:
                            if name not in full_state:
                                full_state[name] = param

        full_state = self.post_process_full_state_after_tp_conversion(full_state, args)
        return full_state

    # Helper function for convert_from_full_state()
    def convert_full_state_to_tp(self, full_state, args, tp_rank, pp_rank, ep_rank, partitions, config):
        tp_size = args.tp_size
        pp_size = args.pp_size
        ep_size = args.ep_size
        kv_size_multiplier = args.kv_size_multiplier

        partial_state = {}
        q_heads = config["num_attention_heads"]
        kv_heads = config["num_key_value_heads"]
        head_dim = config["hidden_size"] // q_heads

        is_gqa = q_heads != kv_heads
        keys_hf_to_nxd, keys_nxd_to_hf = self.get_hf_to_nxd_model_keys(args.qkv_linear, is_gqa)

        for name, full_p in full_state.items():
            ##################### PP Slice #########################################
            # Embedding only in first PP
            if pp_rank != 0 and "embed_tokens" in name:
                continue

            # Non-expert parameters only in EP rank 0
            if ep_rank != 0 and "expert_mlps" not in name:
                continue

            # LMhead and final layer norm only in last PP rank
            if pp_rank != pp_size - 1 and ("lm_head" in name or "model.norm.weight" in name):
                continue
            if "layers" in name:
                layer_idx = int(name.split(".")[2])
                current_stage = len(partitions)
                # iterate through the pp cuts and find the current stage
                for stage, pp_cut in enumerate(partitions):
                    cut_layer_idx = int(pp_cut.split(".")[2])
                    if layer_idx <= cut_layer_idx:
                        current_stage = stage
                        break
                current_pp_rank = stage_to_pipeline_parallel_rank(current_stage, pp_size=pp_size)
                if current_pp_rank != pp_rank:
                    continue

            ##################### EP Slice #########################################
            if "expert_mlps" in name:
                expert_dim = 0
                expert_dim_size = full_p.shape[expert_dim]
                if expert_dim_size % ep_size != 0:
                    raise ValueError(f"Expert dimension ({expert_dim_size}) is not divisible by expert parallelism degree ({ep_size}).")
                num_local_experts = expert_dim_size // ep_size
                with torch.no_grad():
                    weight_slice = full_p.narrow(expert_dim, num_local_experts * ep_rank, num_local_experts)
                partial_state[name] = weight_slice

            ##################### TP Slice #########################################
            if (self.is_qkv_weight(name) or "o_proj" in name) and args.qkv_linear:
                name = self.get_weight_key(keys_hf_to_nxd, keys_nxd_to_hf, name, True)
                if "weight_k" in name or "weight_v" in name or self.is_q_or_o_for_megatron(args,name):
                    repeated_kv = full_p.repeat(kv_size_multiplier, 1)

                    dim_size = repeated_kv.size()[0]
                    assert dim_size % tp_size == 0, "0th dim after KV replication is not divisible by tp_size"
                    partition_size = dim_size // tp_size
                    with torch.no_grad():
                        partition_dim = 0
                        if "o_proj" in name: # only in megatron case we come here
                            partition_dim = self.get_partition_dim(name)
                        to_load = repeated_kv.narrow(partition_dim, tp_rank * partition_size, partition_size).detach().clone()
                        # Cloning the tensor is really important, since we have performed slice and reshape operations.
                        # These operations are just views and if we don't clone, we would end up saving the entire tensor

                        partial_state[name] = to_load.detach().clone()
                else:
                    """
                    When GQAQKV linear with kv_multiplier is used, we need to reshuffle the order of Q heads
                    so they interact with the right KV heads. Now since the heads are shuffled, we have to
                    shuffle the o_proj rows since that translates the heads to hidden dim

                    Example:
                    For num_heads=64, num_kv_heads=16, kv_size_multiplier=4:

                    TRN1 (Interleaved KV pattern):
                    KV pattern: (K0,K1,K2,...,K15)(K0,K1,K2,...,K15)... (4 times)
                    Q reordering: [Q0,Q4,Q8,...,Q60, Q1,Q5,Q9,...,Q61, Q2,Q6,Q10,...,Q62, Q3,Q7,Q11,...,Q63]

                    TRN2 (Grouped KV pattern):
                    KV pattern: (K0,K0,K0,K0)(K1,K1,K1,K1)...(K15,K15,K15,K15)
                    Q reordering: [Q0,Q1,Q2,Q3, Q16,Q17,Q18,Q19, Q32,Q33,Q34,Q35, Q48,Q49,Q50,Q51,
                                Q4,Q5,Q6,Q7, Q20,Q21,Q22,Q23, Q36,Q37,Q38,Q39, Q52,Q53,Q54,Q55, ...]
                    """
                    if "o_proj" in name:
                        # The shuffling is same for both o_proj and q, but o_proj is sharded on column.
                        # Hence to reuse the same shuffling code, we just transpose, do the shuffling and
                        # transpose back
                        full_p = torch.transpose(full_p, 0, 1)
                    weights = full_p.reshape(q_heads, head_dim, -1)
                    weights_shape = weights.size()
                    weight_splits = []

                    if args.hw_backend == "trn2":
                        group_size = q_heads // kv_heads
                        weights = weights.reshape(kv_heads, group_size, head_dim, weights_shape[-1])
                        for i in range(kv_heads):
                            weight_splits.append(weights[i])
                    else:
                        weights = weights.reshape(-1, q_heads // (kv_heads * kv_size_multiplier), head_dim, weights_shape[-1])
                        indicies = torch.arange(0, kv_heads) * tp_size // kv_heads
                        for i in range(tp_size // kv_heads):
                            weight_splits.append(weights[indicies + i])
                    weights = torch.cat(weight_splits, dim=self.qkv_partition_dim)
                    with torch.no_grad():
                        if args.hw_backend == "trn2":
                            start_idx = tp_rank * (q_heads // tp_size)
                            end_idx = (tp_rank + 1) * (q_heads // tp_size)
                            # Select the appropriate slice for this rank
                            to_load = weights[start_idx:end_idx].reshape(-1, weights_shape[-1])
                        else:
                            to_load = weights[tp_rank].reshape(-1, weights_shape[-1])
                        if "o_proj" in name:
                            to_load = torch.transpose(to_load, 0, 1)
                        # Cloning the tensor is really important, since we have performed slice and reshape operations.
                        # These operations are just views and if we don't clone, we would end up saving the entire tensor
                        partial_state[name] = to_load.detach().clone()
            elif (
                "embed_tokens" in name
                or self.is_qkv_weight(name)
                or "o_proj" in name
                or "down_proj" in name
                or "lm_head" in name
            ):
                partition_dim = self.get_partition_dim(name)
                dim_size = full_p.size()[partition_dim]
                assert dim_size % tp_size == 0, "vocab size is not divisiable"
                partition_size = dim_size // tp_size
                with torch.no_grad():
                    to_load = full_p.narrow(partition_dim, tp_rank * partition_size, partition_size)
                    partial_state[name] = to_load.detach().clone()
            elif "gate_proj" in name or "up_proj" in name:
                partition_dim = self.get_partition_dim(name)
                dim_size = full_p.size()[partition_dim]
                assert dim_size % tp_size == 0, "vocab size is not divisiable"
                partition_size = dim_size // tp_size
                with torch.no_grad():
                    to_load = full_p.narrow(partition_dim, tp_rank * partition_size, partition_size).detach().clone()
                token = "gate_proj" if "gate_proj" in name else "up_proj"
                updated_name = name.replace(token, "gate_up_proj")
                if updated_name in partial_state:
                    if token == "gate_proj":
                        partial_state[updated_name] = (
                            torch.cat([to_load, partial_state[updated_name]], dim=partition_dim).detach().clone()
                        )
                    else:
                        partial_state[updated_name] = (
                            torch.cat([partial_state[updated_name], to_load], dim=partition_dim).detach().clone()
                        )
                else:
                    partial_state[updated_name] = to_load.detach().clone()
            else:
                # no TP
                partial_state[name] = full_p
        pkeys = list(partial_state.keys())
        for key in pkeys:
            partial_state[self.rename_keys_for_megatron(key, args.model_style, hf_to_nxdt = True)] = partial_state[key]
            if args.model_style=='megatron':
                del partial_state[key]
        self.modify_qkv_for_megatron(partial_state,args)
        if args.fuse_qkv:
            partial_state = self.convert_partial_state_to_fused_qkv(partial_state, keys_nxd_to_hf, args.n_layers)
        return partial_state

    # Placeholder functions for additional processing of full_state
    def pre_process_full_state_before_tp_conversion(self, full_state, args):
        """Child classes can override this function to implement custom logic."""
        return full_state

    def post_process_full_state_after_tp_conversion(self, full_state, args):
        """Child classes can override this function to implement custom logic."""
        return full_state

    # Helper functions for save/load
    def load_full_state(self, args):
        if args.hf_model_name and not args.input_dir:
            full_state = self.download_and_save_hf_model(args.hf_model_name, args.config)
        elif args.input_dir:
            full_state = torch.load(args.input_dir, map_location='cpu')
        else:
            raise ValueError("Error: Please provide either HuggingFace model name or input path to consolidated statedict")
        return full_state

    def get_input_filename(self, args, tp_rank, pp_rank, ep_rank, xser):
        if xser:
            v1_api_filename = os.path.join(args.input_dir, "tp_rank_{:02d}_pp_rank_{:02d}".format(tp_rank, pp_rank))
        else:
            v1_api_filename = os.path.join(
                args.input_dir, "tp_rank_{:02d}_pp_rank_{:02d}".format(tp_rank, pp_rank), "checkpoint.pt"
            )

        v2_api_filename = os.path.join(
            args.input_dir, "dp_rank_00_tp_rank_{:02d}_pp_rank_{:02d}.pt".format(tp_rank, pp_rank)
        )

        v3_api_filename = os.path.join(
                args.input_dir, "dp_rank_00_ep_rank_{:02d}_tp_rank_{:02d}_pp_rank_{:02d}.pt".format(ep_rank, tp_rank, pp_rank)
        )

        if os.path.exists(v1_api_filename):
            return v1_api_filename

        if os.path.exists(v2_api_filename):
            return v2_api_filename

        if os.path.exists(v3_api_filename):
            return v3_api_filename

        raise RuntimeError(f"Error: neither {v1_api_filename}, nor {v2_api_filename}, nor {v3_api_filename} exist")

    def get_output_filename(self, args, tp_rank, pp_rank, ep_rank, xser):
        if args.ep_size > 1:
            return os.path.join(
                    args.output_dir, "model", "dp_rank_00_ep_rank_{:02d}_tp_rank_{:02d}_pp_rank_{:02d}.pt".format(ep_rank, tp_rank, pp_rank)
            )
        else:
            return os.path.join(
                    args.output_dir, "model", "dp_rank_00_tp_rank_{:02d}_pp_rank_{:02d}.pt".format(tp_rank, pp_rank)
            )

    def load_partial_xser(self, args, tp_rank, pp_rank, ep_rank):
        filename = self.get_input_filename(args, tp_rank, pp_rank, ep_rank, 1)
        dir_name = os.path.join(*(filename.split("/")[:-3]))
        checkpoint_dir = create_checkpoint_storage(dir_name)
        partial_state = _xser_load_data(checkpoint_dir, filename, None, ep_only=ep_rank > 0)
        self.prune_state(partial_state, ep_rank)
        return partial_state

    def prune_state(self, state, ep_rank):
        if ep_rank > 0:
            to_remove = []
            for k, v in state.items():
                if v is None:
                    to_remove.append(k)
            for k in to_remove:
                state.pop(k)

    def load_partial_no_xser(self, args, tp_rank, pp_rank, ep_rank):
        filename = self.get_input_filename(args, tp_rank, pp_rank, ep_rank, 0)
        partial_state = torch.load(filename)
        return partial_state

    def save_full(self, args, full_state):
        save_path = args.output_dir
        os.makedirs(save_path, exist_ok=True)
        if os.path.isdir(save_path):
            save_path = os.path.join(save_path, "checkpoint.pt")
        print(f"Saving full checkpoint to {save_path}")
        torch.save(full_state, save_path)

    def save_partial_xser(self, args, partial_state, tp_rank, pp_rank, ep_rank):
        filename = self.get_output_filename(args, tp_rank, pp_rank, ep_rank, 1)
        os.makedirs(args.output_dir + "/model", exist_ok=True)
        print(f"Saving to {filename}")
        xser.save(partial_state, filename)

    def save_partial_no_xser(self, args, partial_state, tp_rank, pp_rank, ep_rank):
        filename = self.get_output_filename(args, tp_rank, pp_rank, ep_rank, 0)
        os.makedirs(args.output_dir + "/model", exist_ok=True)
        print(f"Saving to {filename}")
        torch.save(partial_state, filename)

    # Main functions to run checkpoint conversion
    def convert_from_xser(self, args):
        for tp_rank in range(args.tp_size):
            for pp_rank in range(args.pp_size):
                partial_state = self.load_partial_xser(args, tp_rank, pp_rank)
                self.save_partial_no_xser(args, partial_state, tp_rank, pp_rank)

    def convert_to_xser(self, args):
        for tp_rank in range(args.tp_size):
            for pp_rank in range(args.pp_size):
                partial_state = self.load_partial_no_xser(args, tp_rank, pp_rank)
                self.save_partial_xser(args, partial_state, tp_rank, pp_rank)

    def convert_from_full_state(self, args):
        full_state = self.load_full_state(args)
        layer_name_pattern = r"^(model\.layers\.\d+)"
        model_layer_names = sorted(
            list(
                set(
                    [
                        re.match(layer_name_pattern, key).group(1)
                        for key in full_state.keys()
                        if re.match(layer_name_pattern, key)
                    ]
                )
            ),
            key=lambda x: int(re.search(r"\d+", x).group()),
        )
        partitions = create_partitions(args.pp_size * args.virtual_pp_size, model_layer_names)
        print(f"pipeline_cuts {partitions}")
        with open(args.config, "r") as f:
            config = json.load(f)
        if args.coalesce_qkv:
            full_state = self.coalesce_qkv(full_state, config, args.tp_size)

        full_state = self.pre_process_full_state_before_tp_conversion(full_state, args)

        for tp_rank in range(args.tp_size):
            for pp_rank in range(args.pp_size):
                for ep_rank in range(args.ep_size):
                    partial_state = self.convert_full_state_to_tp(
                        full_state,
                        args,
                        tp_rank,
                        pp_rank,
                        ep_rank,
                        partitions,
                        config,
                    )
                    if args.save_xser:
                        self.save_partial_xser(args, partial_state, tp_rank, pp_rank, ep_rank)
                    else:
                        self.save_partial_no_xser(args, partial_state, tp_rank, pp_rank, ep_rank)

    def convert_to_full_state(self, args):
        full_state = self.merge_tp_checkpoints(args)
        self.save_full(args, full_state)

    # Argument parsing and execution
    def get_arg_parser(self):
        """Child classes can override this to add new arguments."""

        parser = argparse.ArgumentParser()
        parser.add_argument("--input_dir", type=str, default=None, help="Path to input model/weights (merged checkpoint file)")
        parser.add_argument("--hf_model_name", type=str, default=None, help="HuggingFace model identifier")
        parser.add_argument("--output_dir", type=str, required=True, help="Path to save converted model/weights")
        parser.add_argument("--hw_backend", type=str, required=True, help="Specify the hardware backend (trn1/trn2)")
        parser.add_argument("--config", type=str, help="Config.json")
        parser.add_argument(
            "--model_key", type=str, default="model", help="Key of the model state dict in the checkpoint object"
        )
        parser.add_argument("--tp_size", type=int, default=1, help="Tensor Parallel degree for the model")
        parser.add_argument("--pp_size", type=int, default=1, help="Pipeline Parallel degree for the model")
        parser.add_argument("--ep_size", type=int, default=1, help="Expert Parallel degree for the model")
        parser.add_argument("--virtual_pp_size", type=int, default=1, help="Virtual Pipeline Parallel degree for the model")
        parser.add_argument("--n_layers", type=int, default=0, help="Number of Layers")
        parser.add_argument("--coalesce_qkv", type=bool, default=False, help="whether to coalesce qkv")
        parser.add_argument(
            "--kv_size_multiplier", type=int, default=1, help="Factor by which the KV heads were replicated"
        )
        parser.add_argument(
            "--qkv_linear", type=bool, default=False, help="Factor by which the KV heads were replicated"
        )
        parser.add_argument(
            "--fuse_qkv", type=bool, default=False, help="Whether to fuse qkv"
        )
        parser.add_argument("--load_xser", type=bool, default=False, help="Load from xser saved checkpoints")
        parser.add_argument("--save_xser", type=bool, default=False, help="Save with xser")
        parser.add_argument(
            "--convert_from_xser", action="store_true", help="Convert xser saved checkpoint to normal torch checkpoint"
        )
        parser.add_argument(
            "--convert_to_xser", action="store_true", help="Convert normal torch checkpoint to xser checkpoint"
        )
        parser.add_argument("--convert_from_full_state", action="store_true", help="Convert full model to sharded model")
        parser.add_argument("--convert_to_full_state", action="store_true", help="Convert sharded model to full model")
        parser.add_argument('--model_style', type=str, choices=['hf', 'megatron'], default='hf', help='The source style.')
        parser.add_argument("--nxdt_yaml_config", type=str, help="NxDT yaml file for model config")


        return parser

    def run(self, args):
        """Main function used to run checkpoint conversion."""

        assert sum(
                int(getattr(args, flag))
                for flag in ["convert_from_full_state", "convert_to_full_state", "convert_from_xser", "convert_to_xser"]
            ) == 1, "Exactly one '--convert_*' flag must be specified"

        if args.nxdt_yaml_config:
            args.config = convert_yaml_to_json(args.nxdt_yaml_config)

        if args.convert_from_full_state:
            self.convert_from_full_state(args)
        elif args.convert_to_full_state:
            self.convert_to_full_state(args)
        elif args.convert_from_xser:
            self.convert_from_xser(args)
        elif args.convert_to_xser:
            self.convert_to_xser(args)
