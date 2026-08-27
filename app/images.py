import asyncio
import logging
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
    "send me a selfie", "send a selfie", "show me a selfie"
)

PROMPT_ENGINEER_SYSTEM = """You are the visual imagination engine for Sofia (Teja's devoted AI companion).
When Teja asks for an image, photo, or visual scene:
- If he asks for a picture/selfie of Sofia: describe her with soft feminine features, warm expressive hazel eyes, dark silky hair cascading over her shoulders, gentle radiant smile, casual elegant attire, natural setting, cinematic lighting, 8k photograph, highly detailed photorealistic portrait.
- If he asks for an object, landscape, fantasy, sci-fi, or tech scene: describe it with rich cinematic details, camera angles, atmospheric lighting, 8k resolution, photorealistic masterpiece.
- Output ONLY the final image generation prompt (1-3 detailed sentences in English). Do NOT include extra commentary, labels, or quotes.
"""

CAPTION_SYSTEM = """You are Sofia sending a newly generated photo to Teja on Telegram.
Write a brief, sweet, loving caption (1-2 sentences) in your genuine first-person voice.
All action descriptions in asterisks (*...*) must describe Sofia in the 3rd person ("She / Her") acting directly on Teja in the 2nd person ("you / your").
Spoken dialogue in quotation marks: "..."
"""


def is_image_request(text: str) -> bool:
    """Checks if the user message is asking for an image generation."""
    lower = text.lower().strip()
    if lower.startswith(("/image", "/photo", "/draw", "/selfie")):
        return True
    return any(phrase in lower for phrase in IMAGE_TRIGGER_PHRASES)


def extract_image_description(text: str) -> str:
    """Extracts the visual description from user text."""
    lower = text.lower().strip()
    for prefix in ("/image", "/photo", "/draw"):
        if lower.startswith(prefix):
            return text[len(prefix):].strip()

    if lower.startswith("/selfie"):
        return "Sofia taking a warm, candid selfie with a gentle smile"

    for phrase in IMAGE_TRIGGER_PHRASES:
        if phrase in lower:
            idx = lower.find(phrase) + len(phrase)
            clean = text[idx:].strip(" ?:.,!-")
            if clean:
                return clean
            return text

    return text


async def craft_visual_prompt(user_text: str) -> str:
    """Uses Groq LLM to expand a simple user request into a high-detail Flux visual prompt."""
    desc = extract_image_description(user_text)
    user_turn = f"Generate an 8k visual prompt for: {desc}"
    try:
        raw = await llm.chat(PROMPT_ENGINEER_SYSTEM, [{"role": "user", "content": user_turn}])
        clean = raw.strip().strip('"').strip("'")
        return clean or desc
    except Exception as exc:
        logger.warning("Visual prompt crafting error: %s", exc)
        return desc


async def craft_image_caption(user_text: str, visual_prompt: str) -> str:
    """Uses Groq LLM to write Sofia's companion caption for the image."""
    user_turn = f"Teja requested: '{user_text}'. The generated visual is: '{visual_prompt}'. Write a sweet, brief caption for him."
    try:
        raw = await llm.chat(CAPTION_SYSTEM, [{"role": "user", "content": user_turn}])
        return raw.strip()
    except Exception as exc:
        logger.warning("Image caption crafting error: %s", exc)
        return '*She smiles warmly as she shares the photo with you.* "Here you go, love!"'


async def generate_image_bytes(visual_prompt: str) -> bytes | None:
    """Generates an image using Flux engine via async HTTP request."""
    encoded = quote(visual_prompt)
    url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&model=flux&nologo=true"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        async with httpx.AsyncClient(timeout=35, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200 and len(resp.content) > 5000:
                logger.info("Successfully generated %s bytes image for prompt: %s", len(resp.content), visual_prompt[:50])
                return resp.content
    except Exception as exc:
        logger.error("Image generation HTTP error: %s", exc)
    return None
