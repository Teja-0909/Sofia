import logging
import base64
import json
import re
import httpx

from . import config, db, timeutil

logger = logging.getLogger(__name__)

_cached_groq_model = None


class AllProvidersFailed(Exception):
    pass


def _provider_chain() -> list[tuple[str, str, str]]:
    chain = []
    # 1. Absolute Primary Brain: Gemini 3.5 / 2.5 Flash Lite
    if config.GEMINI_API_KEY:
        chain.append(("gemini", config.GEMINI_MODEL, "gemini"))
        for gm in ("gemini-2.0-flash", "gemini-1.5-flash"):
            if gm != config.GEMINI_MODEL:
                chain.append(("gemini", gm, "gemini"))
    # 2. Seamless Redundancy Fallback: Groq (if Gemini API key is missing or down)
    if config.GROQ_API_KEY:
        chain.append(("groq", "openai/gpt-oss-120b", "openai"))
        chain.append(("groq", "qwen/qwen3.8-27b", "openai"))
        chain.append(("groq", "openai/gpt-oss-20b", "openai"))
    # 3. Fallback 2: OpenRouter
    if config.OPENROUTER_API_KEY:
        chain.append(("openrouter", config.OPENROUTER_MODEL, "openai"))
    return chain


async def _get_best_groq_model(api_key: str, has_image: bool = False) -> str:
    global _cached_groq_model
    if not has_image and _cached_groq_model:
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
                        "qwen/qwen3.8-27b",
                    ]
                    for vp in vision_preferred:
                        if vp in chat_ids:
                            return vp

                text_preferred = [
                    "openai/gpt-oss-120b",
                    "qwen/qwen3.8-27b",
                    "openai/gpt-oss-20b",
                ]
                for p in text_preferred:
                    if p in chat_ids:
                        _cached_groq_model = p
                        return p

                for m in chat_ids:
                    if "gpt-oss" in m.lower() or "qwen" in m.lower():
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


async def _call_gemini(
    system: str, 
    messages: list[dict], 
    model: str, 
    response_format: dict | None = None,
    tools: list[dict] | None = None
) -> tuple[str, dict, list[dict]]:
    contents = []
    for m in messages:
        role = "model" if m.get("role") in ("assistant", "model", "sofia", "alisa", "tool") else "user"
        
        parts = []
        if m.get("role") == "tool":
            parts.append({
                "functionResponse": {
                    "name": m.get("name"),
                    "response": {"result": m.get("content")}
                }
            })
        else:
            text_content = m.get("content", "")
            if text_content:
                parts.append({"text": text_content})
            if m.get("image_bytes"):
                b64_str = base64.b64encode(m["image_bytes"]).decode("utf-8")
                parts.append({
                    "inlineData": {
                        "mimeType": m.get("mime_type", "image/jpeg"),
                        "data": b64_str,
                    }
                })
        
        if not parts:
            continue

        if not contents:
            if role == "model":
                continue  # Gemini contents must begin with a user turn
            contents.append({"role": "user", "parts": parts})
        else:
            if contents[-1]["role"] == role:
                contents[-1]["parts"].extend(parts)
            else:
                contents.append({"role": role, "parts": parts})

    if not contents:
        contents.append({"role": "user", "parts": [{"text": "Hello"}]})

    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "generationConfig": {
            "maxOutputTokens": 4096,
            "temperature": 0.7,
            "topP": 0.95,
        },
        "safetySettings": [
            {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
            {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
        ],
    }
    
    if response_format:
        body["generationConfig"]["responseMimeType"] = "application/json"
        if "schema" in response_format.get("json_schema", {}):
            body["generationConfig"]["responseSchema"] = response_format["json_schema"]["schema"]

    if tools:
        # Convert OpenAI tool format to Gemini tool format
        gemini_tools = []
        for t in tools:
            gemini_tools.append({
                "name": t["function"]["name"],
                "description": t["function"]["description"],
                "parameters": t["function"]["parameters"]
            })
        body["tools"] = [{"functionDeclarations": gemini_tools}]

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    async with httpx.AsyncClient(timeout=60) as client:
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
    
    parts = candidates[0]["content"]["parts"]
    raw_text = parts[0].get("text", "")
    text = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip() or raw_text.strip()
    
    tool_calls = []
    for part in parts:
        if "functionCall" in part:
            fc = part["functionCall"]
            tool_calls.append({
                "id": "call_gemini_" + fc["name"],
                "type": "function",
                "function": {
                    "name": fc["name"],
                    "arguments": json.dumps(fc.get("args", {}))
                }
            })

    usage_raw = data.get("usageMetadata", {})
    usage = {
        "prompt_tokens": usage_raw.get("promptTokenCount", 0),
        "completion_tokens": usage_raw.get("candidatesTokenCount", 0),
    }
    return text, usage, tool_calls


async def _call_openai_compatible(
    provider: str, 
    base_url: str, 
    api_key: str, 
    system: str, 
    messages: list[dict], 
    model: str,
    response_format: dict | None = None,
    tools: list[dict] | None = None
) -> tuple[str, dict, list[dict]]:
    has_image = any(m.get("image_bytes") for m in messages)
    target_model = model
    if provider == "groq" and has_image:
        target_model = await _get_best_groq_model(api_key, has_image=True)

    payload_messages = [{"role": "system", "content": system}]
    for m in messages:
        if m.get("role") == "tool":
            payload_messages.append({
                "role": "tool",
                "content": m.get("content"),
                "tool_call_id": m.get("tool_call_id", "")
            })
            continue

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
            msg_payload = {"role": m["role"], "content": m["content"]}
            if "tool_calls" in m:
                msg_payload["tool_calls"] = m["tool_calls"]
            payload_messages.append(msg_payload)

    json_payload = {
        "model": target_model,
        "messages": payload_messages,
        "max_tokens": 4096,
        "temperature": 0.7,
    }
    
    if response_format:
        json_payload["response_format"] = response_format
    if tools:
        json_payload["tools"] = tools

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key.strip()}"},
            json=json_payload,
        )
        if resp.status_code != 200:
            logger.error("Provider '%s' (model %s) error (HTTP %s): %s", provider, target_model, resp.status_code, resp.text)
        resp.raise_for_status()
        data = resp.json()
        
    choice = data["choices"][0]["message"]
    raw_text = choice.get("content") or ""
    text = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip() or raw_text.strip()
    tool_calls = choice.get("tool_calls") or []
    
    return text, data.get("usage", {}), tool_calls


async def chat(
    system: str, 
    messages: list[dict],
    response_format: dict | None = None,
    tools: list[dict] | None = None
) -> tuple[str, list[dict]]:
    """Returns a tuple of (text_response, tool_calls). If response_format is used, text_response will be JSON string."""
    chain = _provider_chain()
    if not chain:
        logger.error("No LLM API keys configured! Set GROQ_API_KEY, OPENROUTER_API_KEY, or GEMINI_API_KEY")
        raise AllProvidersFailed("No LLM API keys configured in environment")

    errors = []
    for provider, model, kind in chain:
        try:
            if kind == "gemini":
                text, usage, tool_calls = await _call_gemini(system, messages, model, response_format, tools)
            else:
                base_url = (
                    "https://api.groq.com/openai/v1"
                    if provider == "groq"
                    else "https://openrouter.ai/api/v1"
                )
                text, usage, tool_calls = await _call_openai_compatible(
                    provider,
                    base_url,
                    getattr(config, f"{provider.upper()}_API_KEY"),
                    system,
                    messages,
                    model,
                    response_format,
                    tools
                )
            await _log_usage(provider, model, usage)
            return text, tool_calls
        except Exception as exc:
            logger.error("Provider '%s' (%s) failed: %s", provider, model, exc)
            errors.append(f"{provider}: {exc}")
    raise AllProvidersFailed("; ".join(errors))


async def embed_text(text: str) -> list[float]:
    """Generates an embedding vector using Gemini text-embedding-004."""
    if not text or not config.GEMINI_API_KEY:
        return []
        
    url = "https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url,
                params={"key": config.GEMINI_API_KEY.strip()},
                json={
                    "model": "models/text-embedding-004",
                    "content": {"parts": [{"text": text}]}
                }
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["embedding"]["values"]
            else:
                logger.warning("Gemini embedding failed with %s: %s", resp.status_code, resp.text)
                # Fallback to older embedding-001 model if text-embedding-004 is rejected
                if resp.status_code in (400, 404):
                    logger.info("Attempting fallback to models/embedding-001...")
                    fallback_url = "https://generativelanguage.googleapis.com/v1beta/models/embedding-001:embedContent"
                    resp_fb = await client.post(
                        fallback_url,
                        params={"key": config.GEMINI_API_KEY.strip()},
                        json={
                            "model": "models/embedding-001",
                            "content": {"parts": [{"text": text}]}
                        }
                    )
                    if resp_fb.status_code == 200:
                        return resp_fb.json()["embedding"]["values"]
                    else:
                        logger.warning("Fallback Gemini embedding failed with %s: %s", resp_fb.status_code, resp_fb.text)
    except Exception as exc:
        logger.error("Gemini embedding HTTP error: %s", exc)

    return []
