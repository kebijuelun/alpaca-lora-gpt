import os
import sys

import torch
import torch.nn as nn
import bitsandbytes as bnb
from datasets import load_dataset
import transformers

assert (
    "LlamaTokenizer" in transformers._import_structure["models.llama"]
), "LLaMA is now in HuggingFace's main branch.\nPlease reinstall it: pip uninstall transformers && pip install git+https://github.com/huggingface/transformers.git"
from transformers import LlamaForCausalLM, LlamaTokenizer
from transformers import AutoTokenizer, AutoModelForCausalLM
from transformers import GPTNeoXForCausalLM

from peft import (
    prepare_model_for_int8_training,
    LoraConfig,
    get_peft_model,
    get_peft_model_state_dict,
)


def print_trainable_parameters(model):
    """
    Prints the number of trainable parameters in the model.
    """
    trainable_params = 0
    all_param = 0
    for _, param in model.named_parameters():
        all_param += param.numel()
        if param.requires_grad:
            trainable_params += param.numel()
    print(
        f"trainable params: {trainable_params} || all params: {all_param} || trainable%: {100 * trainable_params / all_param}"
    )


# optimized for RTX 4090. for larger GPUs, increase some of these?
MICRO_BATCH_SIZE = 96  # this could actually be 5 but i like powers of 2
BATCH_SIZE = 96 * 2
GRADIENT_ACCUMULATION_STEPS = BATCH_SIZE // MICRO_BATCH_SIZE
EPOCHS = 1  # we don't always need 3 tbh
LEARNING_RATE = 3e-4  # the Karpathy constant
CUTOFF_LEN = 256  # 256 accounts for about 96% of the data
LORA_R = 16
LORA_ALPHA = 32
LORA_DROPOUT = 0.05
VAL_SET_SIZE = 2000
TARGET_MODULES = [
    "query_key_value",
    "xxx",
]
# DATA_PATH = "alpaca_data_cleaned.json"
DATA_PATH = "Belle.train.json"
OUTPUT_DIR = "lora-gpt-neox-20b-belle"

device_map = "auto"
world_size = int(os.environ.get("WORLD_SIZE", 1))
ddp = world_size != 1
if ddp:
    device_map = {"": int(os.environ.get("LOCAL_RANK") or 0)}
    GRADIENT_ACCUMULATION_STEPS = GRADIENT_ACCUMULATION_STEPS // world_size

model = AutoModelForCausalLM.from_pretrained(
    "EleutherAI/gpt-neox-20b", load_in_8bit=True, device_map=device_map
)
tokenizer = AutoTokenizer.from_pretrained("EleutherAI/gpt-neox-20b", add_eos_token=True)

print("before lora")
print_trainable_parameters(model)

model = prepare_model_for_int8_training(model, output_embedding_layer_name="embed_out")

# hacky workaround due to issues with "EleutherAI/gpt-neox-20b"
for name, param in model.named_parameters():
    # freeze base model's layers
    param.requires_grad = False

    if getattr(model, "is_loaded_in_8bit", False):
        # cast layer norm in fp32 for stability for 8bit models
        if param.ndim == 1 and "layer_norm" in name:
            param.data = param.data.to(torch.float16)

config = LoraConfig(
    r=LORA_R,
    lora_alpha=LORA_ALPHA,
    target_modules=TARGET_MODULES,
    lora_dropout=LORA_DROPOUT,
    bias="none",
    task_type="CAUSAL_LM",
)
model = get_peft_model(model, config)
print("after lora")
print_trainable_parameters(model)

tokenizer.pad_token_id = 0  # unk. we want this to be different from the eos token
data = load_dataset("json", data_files=DATA_PATH)


def generate_prompt(data_point):
    # sorry about the formatting disaster gotta move fast
    return f"""Below is an instruction that describes a task. Write a response that appropriately completes the request.

### Instruction:
{data_point["input"]}

### Response:
{data_point["target"]}"""


def tokenize(prompt):
    # there's probably a way to do this with the tokenizer settings
    # but again, gotta move fast
    result = tokenizer(
        prompt,
        truncation=True,
        max_length=CUTOFF_LEN + 1,
        padding="max_length",
    )
    return {
        "input_ids": result["input_ids"][:-1],
        "attention_mask": result["attention_mask"][:-1],
    }


def generate_and_tokenize_prompt(data_point):
    # This function masks out the labels for the input,
    # so that our loss is computed only on the response.

    user_prompt = f"""Below is an instruction that describes a task. Write a response that appropriately completes the request.

                ### Instruction:
                {data_point["input"]}

                ### Response:
                """

    len_user_prompt_tokens = (
        len(
            tokenizer(
                user_prompt,
                truncation=True,
                max_length=CUTOFF_LEN + 1,
            )["input_ids"]
        )
        - 1
    )  # no eos token
    full_tokens = tokenizer(
        # user_prompt + data_point["output"],
        user_prompt + data_point["target"],
        truncation=True,
        max_length=CUTOFF_LEN + 1,
        padding="max_length",
    )["input_ids"][:-1]
    return {
        "input_ids": full_tokens,
        "labels": [-100] * len_user_prompt_tokens
        + full_tokens[len_user_prompt_tokens:],
        "attention_mask": [1] * (len(full_tokens)),
    }


if VAL_SET_SIZE > 0:
    train_val = data["train"].train_test_split(
        test_size=VAL_SET_SIZE, shuffle=True, seed=42
    )
    train_data = train_val["train"].shuffle().map(generate_and_tokenize_prompt)
    val_data = train_val["test"].shuffle().map(generate_and_tokenize_prompt)
else:
    train_data = data["train"].shuffle().map(generate_and_tokenize_prompt)
    val_data = None

trainer = transformers.Trainer(
    model=model,
    train_dataset=train_data,
    eval_dataset=val_data,
    args=transformers.TrainingArguments(
        per_device_train_batch_size=MICRO_BATCH_SIZE,
        gradient_accumulation_steps=GRADIENT_ACCUMULATION_STEPS,
        warmup_steps=100,
        num_train_epochs=EPOCHS,
        learning_rate=LEARNING_RATE,
        fp16=True,
        logging_steps=20,
        evaluation_strategy="steps" if VAL_SET_SIZE > 0 else "no",
        save_strategy="steps",
        eval_steps=200 if VAL_SET_SIZE > 0 else None,
        save_steps=200,
        output_dir=OUTPUT_DIR,
        save_total_limit=3,
        load_best_model_at_end=True if VAL_SET_SIZE > 0 else False,
        ddp_find_unused_parameters=False if ddp else None,
    ),
    data_collator=transformers.DataCollatorForLanguageModeling(tokenizer, mlm=False),
)
model.config.use_cache = False

old_state_dict = model.state_dict
model.state_dict = (
    lambda self, *_, **__: get_peft_model_state_dict(self, old_state_dict())
).__get__(model, type(model))

if torch.__version__ >= "2" and sys.platform != "win32":
    model = torch.compile(model)


trainer.train()

model.save_pretrained(OUTPUT_DIR)

print("\n If there's a warning about missing keys above, please disregard :)")
