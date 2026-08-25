import logging
import base64
import re
import httpx

from . import config, db, timeutil

logger = logging.getLogger(__name__)

_cached_groq_model = None


class AllProvidersFailed(Exception):
    pass


def _provider_chain() -> list[tuple[str, str, str]]:
    chain = []
    # Primary: Gemini 3.5 Flash Lite (500 free requests/day, relaxed human voice)
    if config.GEMINI_API_KEY:
        chain.append(("gemini", config.GEMINI_MODEL, "gemini"))
        if config.GEMINI_MODEL != "gemini-3.5-flash":
            chain.append(("gemini", "gemini-3.5-flash", "gemini"))
    # Seamless Fallback: Groq (14,400 free requests/day)
    if config.GROQ_API_KEY:
        chain.append(("groq", config.GROQ_MODEL, "openai"))
    # Fallback 2: OpenRouter
    if config.OPENROUTER_API_KEY:
        chain.append(("openrouter", config.OPENROUTER_MODEL, "openai"))
    return chain


async def _get_best_groq_model(api_key: str, has_image: bool = False) -> str:
    global _cached_groq_model
    if _cached_groq_model and not has_image:
        return _cached_groq_model
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {api_key.strip()}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                raw_ids = [m["id"] for m in data.get("data", []) if m.get("active", True)]

                # Filter out safety guards, audio transcription, and embeddings
                chat_ids = [
                    m for m in raw_ids
                    if not any(bad in m.lower() for bad in ["guard", "whisper", "embed", "safeguard", "moderation"])
                ]

                if has_image:
                    vision_preferred = [
                        "qwen/qwen3.6-27b",
                        "llama-3.2-11b-vision-preview",
                        "llama-3.2-90b-vision-preview",
                    ]
                    for vp in vision_preferred:
                        if vp in chat_ids:
                            return vp
                    for m in chat_ids:
                        if "vision" in m.lower() or "qwen" in m.lower():
                            return m

                text_preferred = [
                    "openai/gpt-oss-120b",
                    "qwen/qwen3.6-27b",
                    "openai/gpt-oss-20b",
                    "llama-3.3-70b-versatile",
                    "llama-3.1-8b-instant",
                    "llama-3.1-70b-versatile",
                    "allam-2-7b",
                    "gemma2-9b-it",
                ]
                for p in text_preferred:
                    if p in chat_ids:
                        _cached_groq_model = p
                        return p

                for m in chat_ids:
                    if "gpt-oss" in m.lower() or "instant" in m.lower() or "qwen" in m.lower() or "it" in m.lower():
                        _cached_groq_model = m
                        return m

                if chat_ids:
                    _cached_groq_model = chat_ids[0]
                    return chat_ids[0]
    except Exception as exc:
        logger.debug("Failed to query Groq model list: %s", exc)
    return config.GROQ_VISION_MODEL if has_image else config.GROQ_MODEL


async def _log_usage(provider: str, model: str, usage: dict) -> None:
    try:
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("promptTokenCount") or 0)
        completion_tokens = int(usage.get("completion_tokens") or usage.get("candidatesTokenCount") or 0)
        await execute_usage_upsert(
            provider,
            model,
            prompt_tokens,
            completion_tokens,
        )
    except Exception as exc:
        logger.debug("Usage logging note: %s", exc)


async def execute_usage_upsert(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> None:
    await db.execute(
        """
        INSERT INTO api_usage_log (day, provider, model, requests, input_tokens, output_tokens)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(day, provider) DO UPDATE SET
            requests = requests + 1,
            input_tokens = input_tokens + excluded.input_tokens,
            output_tokens = output_tokens + excluded.output_tokens
        """,
        (timeutil.ist_day(), provider, model, prompt_tokens, completion_tokens),
    )


async def _call_gemini(system: str, messages: list[dict], model: str) -> tuple[str, dict]:
    contents = []
    for m in messages:
        role = "model" if m["role"] == "assistant" else "user"
        parts = [{"text": m["content"]}]
        if m.get("image_bytes"):
            b64_str = base64.b64encode(m["image_bytes"]).decode("utf-8")
            parts.append({
                "inlineData": {
                    "mimeType": m.get("mime_type", "image/jpeg"),
                    "data": b64_str,
                }
            })
        contents.append({"role": role, "parts": parts})

    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {"maxOutputTokens": 1024},
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.post(
            url,
            params={"key": config.GEMINI_API_KEY.strip()},
            json=body,
        )
        if resp.status_code != 200:
            logger.error("Gemini API error (HTTP %s): %s", resp.status_code, resp.text)
        resp.raise_for_status()
        data = resp.json()

    candidates = data.get("candidates", [])
    if not candidates or "content" not in candidates[0] or "parts" not in candidates[0]["content"]:
        raise ValueError(f"Gemini returned invalid or blocked candidate structure: {data}")
    raw_text = candidates[0]["content"]["parts"][0].get("text", "")
    text = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip() or raw_text.strip()
    usage_raw = data.get("usageMetadata", {})
    usage = {
        "prompt_tokens": usage_raw.get("promptTokenCount", 0),
        "completion_tokens": usage_raw.get("candidatesTokenCount", 0),
    }
    return text, usage


async def _call_openai_compatible(
    provider: str, base_url: str, api_key: str, system: str, messages: list[dict], model: str
) -> tuple[str, dict]:
    has_image = any(m.get("image_bytes") for m in messages)
    target_model = model
    if provider == "groq":
        target_model = await _get_best_groq_model(api_key, has_image=has_image)

    payload_messages = [{"role": "system", "content": system}]
    for m in messages:
        if m.get("image_bytes"):
            b64_str = base64.b64encode(m["image_bytes"]).decode("utf-8")
            mime = m.get("mime_type", "image/jpeg")
            payload_messages.append({
                "role": m["role"],
                "content": [
                    {"type": "text", "text": m["content"]},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64_str}"}},
                ],
            })
        else:
            payload_messages.append({"role": m["role"], "content": m["content"]})

    async with httpx.AsyncClient(timeout=45) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            json={
                "model": target_model,
                "messages": payload_messages,
                "max_tokens": 1024,
            },
        )
        if resp.status_code != 200:
            logger.error("Provider '%s' (model %s) error (HTTP %s): %s", provider, target_model, resp.status_code, resp.text)
        resp.raise_for_status()
        data = resp.json()
    raw_text = data["choices"][0]["message"]["content"]
    text = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip() or raw_text.strip()
    return text, data.get("usage", {})


async def chat(system: str, messages: list[dict]) -> str:
    chain = _provider_chain()
    if not chain:
        logger.error("No LLM API keys configured! Set GROQ_API_KEY, OPENROUTER_API_KEY, or GEMINI_API_KEY")
        raise AllProvidersFailed("No LLM API keys configured in environment")

    errors = []
    for provider, model, kind in chain:
        try:
            if kind == "gemini":
                text, usage = await _call_gemini(system, messages, model)
            else:
                base_url = (
                    "https://api.groq.com/openai/v1"
                    if provider == "groq"
                    else "https://openrouter.ai/api/v1"
                )
                text, usage = await _call_openai_compatible(
                    provider,
                    base_url,
                    getattr(config, f"{provider.upper()}_API_KEY"),
                    system,
                    messages,
                    model,
                )
            await _log_usage(provider, model, usage)
            return text
        except Exception as exc:
            logger.error("Provider '%s' (%s) failed: %s", provider, model, exc)
            errors.append(f"{provider}: {exc}")
    raise AllProvidersFailed("; ".join(errors))
