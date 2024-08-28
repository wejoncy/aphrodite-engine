import os

from PIL import Image
from transformers import AutoTokenizer

from aphrodite import LLM, SamplingParams

# 2.0
# MODEL_NAME = "HwwwH/MiniCPM-V-2"
# 2.5
MODEL_NAME = "openbmb/MiniCPM-Llama3-V-2_5"

image_path = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                            "burg.jpg")
image = Image.open(image_path)

# convert the image to rgb with pil
image = image.convert("RGB")

tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, trust_remote_code=True)
llm = LLM(model=MODEL_NAME,
          trust_remote_code=True,
          max_model_len=4096)

messages = [{
    'role':
    'user',
    'content':
    '(<image>./</image>)\n' + "What's the content of the image?"
}]
prompt = tokenizer.apply_chat_template(messages,
                                       tokenize=False,
                                       add_generation_prompt=True)
# 2.0
# stop_token_ids = [tokenizer.eos_id]
# 2.5
stop_token_ids = [tokenizer.eos_id, tokenizer.eot_id]

sampling_params = SamplingParams(
    stop_token_ids=stop_token_ids,
    # temperature=0.7,
    # top_p=0.8,
    # top_k=100,
    # seed=3472,
    max_tokens=1024,
    # min_tokens=150,
    temperature=1.2,
    min_p=0.06,
    # use_beam_search=True,
    # length_penalty=1.2,
    # best_of=3
    )

outputs = llm.generate({
    "prompt": prompt,
    "multi_modal_data": {
        "image": image
    }
},
                       sampling_params=sampling_params)
print(outputs[0].outputs[0].text)
