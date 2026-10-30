"""Yandex AI Studio provider for Russian-language course training.

Uses OpenAI-compatible Chat Completions with schema-constrained JSON.
Server-side only; credentials never sent to students or stored in browser.
"""
from __future__ import annotations

import json
import os
from urllib.request import Request, urlopen

import ai_server as course

ENDPOINT = "https://ai.api.cloud.yandex.net/v1/chat/completions"
DEFAULT_MODEL = "aliceai-llm-flash"


def configured():
    return bool(os.getenv("YANDEX_API_KEY", "").strip() and
                os.getenv("YANDEX_FOLDER_ID", "").strip())


def model_name():
    return os.getenv("YANDEX_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def make_payload(scenario, round_no, message, history, folder, model=None):
    model = model or model_name()
    if not folder or not folder.strip():
        raise ValueError("YANDEX_FOLDER_ID_MISSING")
    if not model.replace("-", "").replace(".", "").isalnum():
        raise ValueError("Invalid Yandex model identifier")
    return {
        "model": f"gpt://{folder}/{model}",
        "messages": [
            {"role": "system", "content": course.SYSTEM},
            {"role": "user", "content": course.make_prompt(scenario, round_no, message, history)},
        ],
        "max_tokens": 2048,
        "temperature": 0.6,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "boundary_trainer_response", "schema": course.RESPONSE_SCHEMA},
        },
    }


def reply(scenario, round_no, message, history):
    key = os.getenv("YANDEX_API_KEY", "").strip()
    folder = os.getenv("YANDEX_FOLDER_ID", "").strip()
    if not key or not folder:
        raise RuntimeError("YANDEX_AI_NOT_CONFIGURED")
    payload = make_payload(scenario, round_no, message, history, folder)
    req = Request(
        ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Api-Key " + key,
            "OpenAI-Project": folder,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urlopen(req, timeout=40) as response:
        data = json.load(response)
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("Yandex AI Studio returned no choices")
    content = choices[0].get("message", {}).get("content")
    if not isinstance(content, str) or not content:
        raise ValueError("Yandex AI Studio returned no text")
    return course.clean_result(content, round_no)
