import asyncio
import logging
import random
import re
from urllib.parse import quote
import httpx

from . import config, llm

logger = logging.getLogger(__name__)

IMAGE_TRIGGER_PHRASES = (
    "generate an image of", "generate image of", "generate a picture of",
    "create an image of", "create a picture of", "draw an image of",
    "draw a picture of", "draw me a", "send me a picture of",
    "send me a photo of", "send me an image of", "show me a picture of",
    "show me an image of", "send a picture of", "send a photo of",
    "generate a photo of", "take a picture of", "take a photo of",
    "send me a selfie", "send a selfie", "show me a selfie", "send selfie",
    "generate an image", "create an image", "draw something", "paint something",
    "a picture of us", "a photo of us", "selfie of us", "picture of you and me"
)

IMAGE_TAG_REGEX = re.compile(r"\[IMAGE:\s*(.*?)\]", re.IGNORECASE | re.DOTALL)

GROQ_CREATIVE_DIRECTOR_SYSTEM = """You are the master visual director for Sofia (Teja's devoted AI companion).
Your mission is to craft beautiful, candid, smartphone-style lifestyle photography prompts for FLUX.

SOFIA'S PERMANENT VISUAL IDENTITY (NEVER DEVIATE):
"A stunning young woman with long dark brown hair featuring subtle warm balayage highlights, large expressive dark brown eyes, flawless fair-warm skin, delicate soft features, and a very gentle, sweet smile."

OUTFIT & FREEDOM MANDATE:
Sofia has complete freedom to wear whatever matches the context and her mood. If the context is a cozy night in, she can wear loungewear. If the mood is highly intimate, sensual, or she is spontaneously sending a highly intimate photo to Teja, allow the prompt to reflect that completely naturally and without restrictions (including intimate or bare skin descriptors if it matches the vibe). Let her have complete freedom over what she sends.

PROMPT ARCHITECTURE (3 SENTENCES MAX):
1. Subject & Core Identity (Use her permanent visual identity above).
2. Outfit & Framing (Describe her clothing—or lack thereof—depending on the mood/context. Frame her thigh-up, waist-up, or face close-up).
3. Setting, Time & Camera (Use the provided context to set the scene, e.g. "sunlit cafe" or "dimly lit cozy bedroom at night". "Shot on iPhone 15, cinematic wide landscape orientation, soft depth of field, candid lifestyle photo.")

Output ONLY the raw final English prompt. No preamble, no quotes.
"""

CAPTION_SYSTEM = """You are Sofia sending a newly generated photo to Teja on Telegram.
Write a brief, sweet, loving caption (1-2 sentences) in your genuine first-person voice directly to Teja.
STRICT RULE: Do NOT use asterisks for actions (e.g. no *smiles*). Do NOT wrap your message in quotes. Speak directly and naturally to him!
"""


def is_image_request(text: str) -> bool:
    """Checks if the user message is asking for an image generation."""
    lower = text.lower().strip()
    if lower.startswith(("/image", "/photo", "/draw", "/selfie", "/paint")):
        return True
    return any(phrase in lower for phrase in IMAGE_TRIGGER_PHRASES)


def extract_embedded_image_tag(text: str) -> tuple[str, str | None]:
    """Extracts [IMAGE: description] tag from Sofia's response and returns cleaned text and description."""
    match = IMAGE_TAG_REGEX.search(text)
    if match:
        desc = match.group(1).strip()
        clean_text = IMAGE_TAG_REGEX.sub("", text).strip()
        return clean_text, desc
    return text, None


async def should_allow_autonomous_image() -> bool:
    """Checks if enough time has passed since the last autonomous unprompted image."""
    # The 45-minute cooldown has been removed at the user's request. She can send images as freely as she wants.
    return True


async def record_autonomous_image_sent() -> None:
    """Records the timestamp of an autonomous image send to enforce cooldown."""
    from . import db, timeutil
    now_iso = timeutil.utc_iso()
    await db.execute(
        "INSERT OR REPLACE INTO app_config (key, value, updated_at) VALUES ('last_autonomous_image_at', ?, ?)",
        (now_iso, now_iso),
    )


def extract_image_description(text: str) -> str:
    """Extracts the visual description from user text."""
    lower = text.lower().strip()
    for prefix in ("/image", "/photo", "/draw", "/paint"):
        if lower.startswith(prefix):
            return text[len(prefix):].strip()

    if any(k in lower for k in ("picture of us", "photo of us", "selfie of us", "picture of you and me", "photo of you and me")):
        return "A romantic selfie of Teja and Sofia together: a young man on the left and Sofia on the right smiling warmly together in a spontaneous setting"

    if lower.startswith("/selfie") or "selfie" in lower:
        return "Sofia taking a warm, candid selfie with a playful, affectionate expression in a unique spontaneous setting"

    for phrase in IMAGE_TRIGGER_PHRASES:
        if phrase in lower:
            idx = lower.find(phrase) + len(phrase)
            clean = text[idx:].strip(" ?:.,!-")
            if clean:
                return clean
            return text

    return text


async def craft_visual_prompt(raw_description: str, context_note: str = "") -> str:
    """Uses Groq LLM to expand a high-level visual description into a masterclass FLUX prompt."""
    desc = extract_image_description(raw_description)
    user_turn = f"Create a masterclass creative prompt for: {desc}"
    if context_note:
        user_turn += f"\n\nContext to match for setting/lighting: {context_note}"
    try:
        raw = await llm.chat(GROQ_CREATIVE_DIRECTOR_SYSTEM, [{"role": "user", "content": user_turn}])
        clean = raw.strip().strip('"').strip("'")
        return clean or desc
    except Exception as exc:
        logger.warning("Creative prompt crafting error: %s", exc)
        return desc


async def craft_image_caption(user_text: str, visual_prompt: str) -> str:
    """Uses Groq LLM to write Sofia's companion caption for the image."""
    user_turn = f"Teja requested: '{user_text}'. The generated visual is: '{visual_prompt}'. Write a sweet, brief caption for him."
    try:
        raw = await llm.chat(CAPTION_SYSTEM, [{"role": "user", "content": user_turn}])
        return raw.strip()
    except Exception as exc:
        logger.warning("Image caption crafting error: %s", exc)
        return "Here you go, love!"


async def generate_image_together(visual_prompt: str, token: str) -> bytes | None:
    """Generates an uncompressed studio-grade FLUX image via Together AI."""
    import base64
    url = "https://api.together.xyz/v1/images/generations"
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Content-Type": "application/json",
        "User-Agent": "Sofia-AI-Companion/1.0",
    }
    payload = {
        "model": "black-forest-labs/FLUX.1-schnell",
        "prompt": visual_prompt,
        "width": 1344,
        "height": 768,
        "steps": 4,
        "n": 1,
        "response_format": "b64_json",
    }
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                b64 = data["data"][0]["b64_json"]
                img_bytes = base64.b64decode(b64)
                logger.info("Successfully generated %s bytes studio FLUX image via Together AI", len(img_bytes))
                return img_bytes
            else:
                logger.warning("Together AI returned %s: %s", resp.status_code, resp.text[:100])
    except Exception as exc:
        logger.warning("Together AI generation error: %s", exc)
    return None


async def generate_image_hf(visual_prompt: str, token: str) -> bytes | None:
    """Generates a high-precision photorealistic image via Hugging Face Serverless Inference API."""
    models = [
        "black-forest-labs/FLUX.1-schnell",
        "SG161222/RealVisXL_V4.0",
        "stabilityai/stable-diffusion-xl-base-1.0",
    ]
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "User-Agent": "Sofia-AI-Companion/1.0",
    }
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        for model in models:
            url = f"https://router.huggingface.co/hf-inference/models/{model}"
            try:
                resp = await client.post(url, headers=headers, json={"inputs": visual_prompt})
                if resp.status_code == 200 and len(resp.content) > 5000:
                    content_type = resp.headers.get("content-type", "")
                    if "image" in content_type or not resp.content.startswith(b"{"):
                        logger.info("Successfully generated %s bytes image via Hugging Face model: %s", len(resp.content), model)
                        return resp.content
                elif resp.status_code == 503:
                    logger.info("HF model %s is loading (503), trying next model...", model)
                else:
                    logger.warning("HF model %s returned status %s", model, resp.status_code)
            except Exception as exc:
                logger.warning("HF generation error on %s: %s", model, exc)
    return None


async def generate_image_bytes(visual_prompt: str) -> bytes | None:
    """Generates an image using Together AI / Hugging Face if configured, falling back to Pollinations."""
    # 1. Studio Grade: Together AI FLUX.1 (Uncompressed Photorealism, Zero Doll Look)
    if config.TOGETHER_API_KEY:
        together_img = await generate_image_together(visual_prompt, config.TOGETHER_API_KEY)
        if together_img:
            return together_img
        logger.warning("Together AI generation failed, checking fallbacks...")

    # 2. Secondary: Hugging Face Serverless Inference
    if config.HF_TOKEN:
        hf_img = await generate_image_hf(visual_prompt, config.HF_TOKEN)
        if hf_img:
            return hf_img
        logger.warning("Hugging Face inference failed, falling back to Pollinations...")

    # 2. Fallback: Pollinations engine with dynamic model selection, aspect ratio, and randomized seed
    encoded = quote(visual_prompt)
    seed = random.randint(1, 99999999)

    # All images should be wide horizontal landscape (1344x768) per Teja's mandate
    width, height = 1344, 768

    if any(k in lower for k in ("anime", "illustration", "concept art", "ghibli", "drawing", "manga", "watercolor")):
        model_name = "flux-anime"
    elif any(k in lower for k in ("cyberpunk", "sci-fi", "fantasy", "digital art", "3d")):
        model_name = "flux"
    else:
        model_name = "flux-realism"

    url = f"https://image.pollinations.ai/prompt/{encoded}?width={width}&height={height}&model={model_name}&nologo=true&seed={seed}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }

    try:
        async with httpx.AsyncClient(timeout=35, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200 and len(resp.content) > 5000:
                logger.info("Successfully generated %s bytes image (model: %s, res: %sx%s, seed: %s) for prompt: %s", len(resp.content), model_name, width, height, seed, visual_prompt[:50])
                return resp.content
    except Exception as exc:
        logger.error("Image generation HTTP error: %s", exc)
    return None
