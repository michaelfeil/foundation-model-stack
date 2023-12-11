import argparse
import itertools
import os

import torch
import torch._inductor.config
from torch import distributed as dist

from fms.distributed.strategy import TensorParallelStrategy
from fms.models import get_model
from fms.models.llama import load_fms_llama
from fms.utils import generation, tokenizers
from fms.utils.cache import PagedKVCache
from fms.utils.generation import generate


# This example script validates the LLaMA implementation by running inference on a couple of prompts.
#
# Example usage with single-GPU 7B model on slurm, with torch.compile and determinstic behavior:
# CUBLAS_WORKSPACE_CONFIG=:4096:8 srun -N 1 --gres=gpu:1 python scripts/inference.py --model_path=~/models/7B-F/ --tokenizer=~/models/tokenizer.model --compile --deterministic
# Example usage of 13B model on 2 GPUs with Tensor Parallel:
# srun -N 1 --gres=gpu:2 torchrun --nproc_per_node=2 scripts/inference.py --model_path=~/models/13B-F --tokenizer=~/models/tokenizer.model --distributed

parser = argparse.ArgumentParser(description="Script to run inference on a LLaMA model")
parser.add_argument("--device_type", type=str, default="cuda")
parser.add_argument(
    "--model_path",
    type=str,
    required=True,
    help="Path to the directory containing LLaMa weights (.pth files sharded by tensor parallel rank, not HF weights)",
)
parser.add_argument(
    "--tokenizer",
    type=str,
    required=True,
    help="Path to the tokenizer (e.g. ~/tokenizer.model)",
)
parser.add_argument(
    "--no_use_cache",
    action="store_false",
    help="Disable the kv-cache (on by default)",
)
parser.add_argument(
    "--compile",
    action="store_true",
    help="Use torch.compile (slow for first inference pass)",
)
parser.add_argument(
    "--compile_mode",
    type=str,
    help="Mode for compilation",
    default="default",
    choices=["default", "reduce-overhead"],
)
parser.add_argument(
    "--deterministic",
    action="store_true",
    help="Set torch.use_deterministic_algorithms? Requires env variable `CUBLAS_WORKSPACE_CONFIG=:4096:8`",
)
parser.add_argument(
    "--distributed",
    action="store_true",
    help="This is a distributed job (multiple instances run with RANK+WORLD_SIZE)",
)
parser.add_argument("--context_file", type=str, default=None, help="File to summarize")

args = parser.parse_args()

local_rank = int(os.getenv("LOCAL_RANK", 0))
device = torch.device(args.device_type, local_rank)
torch.cuda.set_device(device)

torch.set_default_device(device)
torch.set_default_dtype(torch.half)

# requires setting environment variable: `CUBLAS_WORKSPACE_CONFIG=:4096:8`
if args.deterministic:
    torch.use_deterministic_algorithms(True)

if args.distributed:
    dist.init_process_group()

# print("loading model")
# model = get_model("llama", "13b", args.model_path, source="meta", device_type="cuda", norm_eps=1e-6, checkpoint_sharding="tp", distributed_strategy="tp")
# tokenizer = tokenizers.get_tokenizer(args.tokenizer)
# model.eval()
# torch.set_grad_enabled(False)
# print("loading complete on rank", local_rank)
#
# kv_cache = PagedKVCache(
#     model.config.nlayers,
#     model.config.nheads if model.config.kvheads == 0 else model.config.kvheads,
#     model.config.emb_dim,
#     total_num_gpu_blocks=400,
#     tensor_parallel_size=2,
#     device=f"cuda:{int(os.environ['LOCAL_RANK'])}",
#     dtype=model.shared.emb.weight.dtype,
# )

print("loading model")
model = get_model(
    "llama", "13b", args.model_path, source="meta", checkpoint_sharding="tp", distributed_strategy="tp", device_type="cuda", norm_eps=1e-6
)
tokenizer = tokenizers.get_tokenizer(args.tokenizer)
model.eval()
torch.set_grad_enabled(False)
print("loading complete on rank", local_rank)

kv_cache = PagedKVCache(
    model.config.nlayers,
    model.config.nheads,
    model.config.emb_dim,
    total_num_gpu_blocks=600,
    tensor_parallel_size=dist.get_world_size(),
    dtype=model.shared.emb.weight.dtype,
    device=device
)
# kv_cache = None


if args.compile:
    print("compiling model")
    # Bug with kv-cache in PT2.1
    torch._inductor.config.joint_graph_constant_folding = False
    # compiling can make first inference pass slow
    model = torch.compile(model, mode=args.compile_mode)


def ids_for_prompt(prompt):
    tokens = tokenizer.tokenize(prompt)
    tokens = ["<s>"] + tokens
    ids = tokenizer.convert_tokens_to_ids(tokens)
    ids = torch.tensor(ids, dtype=torch.long, device=device)
    return ids


def pad_prompt(prompt, pad_len, pad_token="<unk>"):
    to_pad = pad_len - len(prompt)
    if to_pad == 0:
        return prompt

    pad_id = tokenizer.convert_tokens_to_ids(pad_token)
    pad_ids = [pad_id] * to_pad
    pads = torch.tensor(pad_ids, device=device)
    return torch.cat((pads, prompt))


if args.context_file is not None:
    # during testing, the context_file used was a copy/paste of the text of:
    # https://arxiv.org/pdf/2306.15595.pdf
    with open(args.context_file) as file:
        long_prompt = file.read()
        prompt1 = (
            long_prompt
            + "\nPlease give me a brief summary of this research paper in a few bullet points."
        )
        # prompt1 = long_prompt + "\nDescribe work that was done concurrently with the research in this paper."
        prompt2 = long_prompt + "\nPlease write me the abstract for this paper."
else:
    template = "Below is an instruction that describes a task. Write a response that appropriately completes the request.\n\n### Instruction:\n{}\n\n### Response:"

    prompt1 = template.format(
        "Provide a list of instructions for preparing chicken soup."
    )
    prompt2 = template.format("Explain some popular greetings in Spanish.")
    prompt3 = template.format("Explain to me why ignorance is bliss.")
    prompt4 = template.format(
        "I have just come into a very large sum of money. I received the money from my parents who told me I could do whatever I want with it. My first thought was to go to a financial advisor. Provide me a list of things that I can do with my new found wealth."
    )

prompt1 = ids_for_prompt(prompt1)
prompt2 = ids_for_prompt(prompt2)
prompt3 = ids_for_prompt(prompt3)
prompt4 = ids_for_prompt(prompt4)

max_len = max([len(prompt) for prompt in [prompt1, prompt2, prompt3, prompt4]])

# LLaMA 7B did better on the spanish prompt vs 13B.
# TODO: add a better english prompt to demonstrate padding/batching.
prompt1 = pad_prompt(prompt1, max_len)
prompt2 = pad_prompt(prompt2, max_len)
prompt3 = pad_prompt(prompt3, max_len)
prompt4 = pad_prompt(prompt4, max_len)
ids = torch.stack((prompt1, prompt2, prompt3, prompt4), dim=0)

# ids = prompt2.unsqueeze(0)
# kv_cache=None


def print_result(result):
    if local_rank != 0:
        return
    # stop at EOS token if present
    result = generation.truncate_after_eos(
        result, tokenizer.convert_tokens_to_ids("</s>")
    )
    # print(result)
    # print(tokenizer.convert_ids_to_tokens(result))
    print(tokenizer.convert_tokens_to_string(tokenizer.convert_ids_to_tokens(result)))
    print()


def infer(use_cache, do_sample):
    # With greedy generation (do_sample=False) we _should_ always get the same results.
    # There is currently a bug in start_pos for batched rotary embeddings that can lead
    # varying results for the same prompt.
    if local_rank == 0:
        print("use_cache", use_cache, ";; do_sample", do_sample)
        print("==================")
    if model.config.ntk_scaling:
        max_seq_len = max(max_len, model.config.max_expected_seq_len)
    else:
        # without ntk scaling, extending the seq length too far gives bogus results.
        max_seq_len = model.config.max_expected_seq_len

    result = generate(
        model,
        ids,
        max_new_tokens=100,
        use_cache=use_cache,
        paged_kv_cache=kv_cache,
        do_sample=do_sample,
        max_seq_len=max_seq_len,
    )
    for i in range(result.shape[0]):
        print_result(result[i])


print("generating output", local_rank)
do_sample = [False]
use_cache = [
    args.no_use_cache
]  # True/False are identical with greedy iff `torch.use_deterministic_algorithms(True)`
for sample, cache in itertools.product(do_sample, use_cache):
    infer(cache, sample)
