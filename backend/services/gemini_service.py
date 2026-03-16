"""
Gemini 2.5 Flash helper for grounded F1 analyst answers.
"""
import json
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"


def gemini_available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def _get_api_key() -> Optional[str]:
    return os.environ.get("GEMINI_API_KEY")


def _trim_payload(payload: dict, max_chars: int = 18000) -> str:
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...(truncated)"


def _looks_incomplete(text: str) -> bool:
    stripped = (text or "").strip()
    if len(stripped) < 80:
        return True
    if stripped[-1] not in ".!?":
        return True
    incomplete_tails = (
        "because",
        "and",
        "but",
        "or",
        "so",
        "which",
        "that",
        "with",
        "to",
        "for",
        "must",
        "should",
    )
    last_word = stripped.rstrip(".,!?").split()[-1].lower() if stripped else ""
    return last_word in incomplete_tails


async def _call_gemini(prompt: str, max_output_tokens: int = 420) -> str:
    api_key = _get_api_key()
    if not api_key:
        raise ValueError("No GEMINI_API_KEY set")

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "topP": 0.9,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "text/plain",
        },
    }
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(GEMINI_URL, headers=headers, json=body)
        response.raise_for_status()
        data = response.json()

    candidates = data.get("candidates") or []
    for candidate in candidates:
        parts = candidate.get("content", {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts if part.get("text"))
        if text.strip():
            if _looks_incomplete(text):
                finish_reason = candidate.get("finishReason")
                raise ValueError(f"Gemini returned an incomplete answer. finishReason={finish_reason}")
            return text.strip()

    finish_reason = candidates[0].get("finishReason") if candidates else None
    raise ValueError(f"Gemini returned no text output. finishReason={finish_reason}")


async def answer_weekend_question_with_gemini(question: str, race: dict, analyst: dict) -> str:
    prompt = (
        "You are an F1 weekend analyst. Answer using only the supplied race context and analytics. "
        "Do not invent facts, lap times, weather, or standings. "
        "If the supplied data is insufficient, say so directly. "
        "Write a complete answer in 3 to 5 sentences and end cleanly. Do not stop mid-sentence.\n\n"
        f"Question: {question}\n\n"
        f"Weekend race context JSON:\n{_trim_payload({'race': race, 'analyst': analyst})}"
    )
    return await _call_gemini(prompt, max_output_tokens=420)


async def answer_debrief_question_with_gemini(question: str, report: dict) -> str:
    prompt = (
        "You are an F1 post-race debrief analyst. Answer using only the supplied team debrief data. "
        "Do not invent telemetry, incidents, or strategy details that are not present. "
        "If the supplied data is insufficient, say so directly. "
        "Write a complete answer in 3 to 5 sentences and end cleanly. Do not stop mid-sentence.\n\n"
        f"Question: {question}\n\n"
        f"Team debrief JSON:\n{_trim_payload(report)}"
    )
    return await _call_gemini(prompt, max_output_tokens=420)
