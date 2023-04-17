import os
from argparse import Namespace
from string import Template

import torch
import transformers
from torch import nn
from trainer_rl import argument_parsing
from utils.utils import get_model


class SFTLogitsModel(nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, *args, **kwargs):
        return self.model(*args, **kwargs).logits


class RMLogitsModel(nn.Module):
    def __init__(self, model):
        super().__init__()
        self.model = model

    def forward(self, *args, **kwargs):
        return self.model(*args, **kwargs).logits[:, 0]


def load_model_and_tokenizer(config, device="cuda:0"):
    tokenizer = transformers.AutoTokenizer.from_pretrained(config.model_name)

    # For llama ...
    if tokenizer.pad_token_id == tokenizer.eos_token_id:
        tokenizer.add_special_tokens({"pad_token": "<|padding|>"})
    print("len tokenizer", len(tokenizer))

    model = get_model(config, tokenizer, pad_vocab_size_to_multiple_of=1)
    model.to(device)
    model.eval()

    if config.mode == "sft":
        model = SFTLogitsModel(model)
    elif config.mode == "rm":
        model = RMLogitsModel(model)
    else:
        raise ValueError(f"Unknown mode {config.mode}")

    return model, tokenizer


def write_traced_module(
    traced_script_module,
    model_name,
    dtype="fp16",
    output_dir="model_store_sft",
    config_template="configs/triton_config_sft.pbtxt",
):
    model_dir = os.path.join(output_dir, model_name, "1")
    os.makedirs(model_dir, exist_ok=True)
    traced_script_module.save(os.path.join(model_dir, "traced-model.pt"))

    with open(config_template) as f:
        template = Template(f.read())

    if dtype == "float16":
        dtype = "fp16"
    elif dtype == "float32":
        dtype = "fp32"
    elif dtype == "bfloat16":
        dtype = "bf16"

    config = template.substitute(
        {
            "model_name": model_name,
            "dtype": dtype,
            "output_type": f"TYPE_{dtype.upper()}",
        }
    )

    with open(os.path.join(output_dir, model_name, "config.pbtxt"), "w") as f:
        f.write(config)


def trace_model(model, tokenizer, device="cuda:0", trace_example="reward model's hash"):
    inputs = tokenizer(trace_example, return_tensors="pt")
    inputs = {k: v.to(device) for k, v in inputs.items() if k != "token_type_ids"}
    outputs = model(**inputs)
    print(f"Output shape: {outputs.shape}")

    traced_script_module = torch.jit.trace(model, (inputs["input_ids"], inputs["attention_mask"]))
    return traced_script_module


def main():
    conf = argument_parsing(triton_mode=None)

    if conf.triton_mode == "sft":
        config = Namespace(**conf.sft_config)
    elif conf.triton_mode == "rm":
        config = Namespace(**conf.rank_config)

    device = torch.device("cuda:0")

    model, tokenizer = load_model_and_tokenizer(config, device=device)

    traced_script_module = trace_model(model, tokenizer, device=device)

    model_name = config.model_name.replace("/", "-")
    write_traced_module(traced_script_module, model_name, config.cache_dir)


if __name__ == "__main__":
    main()