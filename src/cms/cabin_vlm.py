"""One cabin still through the local Qwen3.5-0.8B TensorRT engines.

Run this with the Edge-LLM interpreter. The dashboard environment does not
contain that runtime. The engines keep their weights in the checkpoint, so
this process loads both.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import cabin_record

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "third_party" / "Qwen3.5-0.8B"
ENGINES = MODEL / "engines"

PROMPT = (
    "Cabin camera still. Reply with one JSON object and no other text. "
    "Pick exactly one event. "
    '{"event":"none","confidence":0.0,"subjects":["passenger"],'
    '"description":"short","severity":"low"}. '
    "event is one of none, phone, reaching, lying_down, altercation, unusual_object. "
    "phone means a person is holding or looking at a phone."
)


def load_runtime():
    from tensorrt_edgellm import runtime

    native = runtime.load()
    engine = native.LLMRuntime(
        str(ENGINES),
        str(ENGINES),
        {},
        str(MODEL),
    )
    return native, engine


def ask(native, engine, image_path: Path) -> str:
    text = native.MessageContent("text", PROMPT)
    image = native.MessageContent("image", "")
    message = native.Message("user", [image, text])
    request = native.LLMGenerationRequest()
    row = native.Request(messages=[message])
    picture = native.load_image_from_path(str(image_path))
    picture.do_resize = True
    row.image_buffers = [picture]
    request.requests = [row]
    request.temperature = 0.0
    request.top_p = 1.0
    request.top_k = 1
    request.max_generate_length = 96
    request.enable_thinking = False
    request.apply_chat_template = True
    request.add_generation_prompt = True
    response = engine.handle_request(request)
    return response.output_texts[0] if response.output_texts else ""


def parse_event(text: str) -> dict:
    return cabin_record.parse_record(text)


def serve() -> None:
    native, engine = load_runtime()
    print("ready", flush=True)
    for line in sys.stdin:
        image = line.strip()
        if not image:
            continue
        try:
            result = parse_event(ask(native, engine, Path(image)))
        except Exception as exc:
            result = {"event": "none", "raw": "", "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False), flush=True)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        serve()
        return
    image = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "results" / "cabin_vlm_test.jpg")
    native, engine = load_runtime()
    result = parse_event(ask(native, engine, image))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
