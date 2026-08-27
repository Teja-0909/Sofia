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
    "send me a selfie", "send a selfie", "show me a selfie", "send selfie"
)

IMAGE_TAG_REGEX = re.compile(r"\[IMAGE:\s*(.*?)\]", re.IGNORECASE | re.DOTALL)

GROQ_IMAGE_DIRECTOR_SYSTEM = """You are the master visual director for Sofia (Teja's devoted AI companion).
Convert high-level visual descriptions into masterclass FLUX.1 / Stable Diffusion XL photographic prompts.

CHARACTER SPECIFICATIONS FOR SOFIA:
- Young woman in her early 20s with soft, naturally beautiful feminine facial features.
- Warm expressive hazel-amber eyes, natural eyelashes, dark silky brown/black hair with soft texture.
- Radiant natural skin with realistic pores and soft highlights, gentle intimate smile.

PHOTOGRAPHIC & ARTISTIC DIRECTIVES:
- Framing & Composition: Cinematic medium close-up, selfie angle, or candid portrait, shot on 85mm f/1.4 lens, shallow depth of field.
- Lighting & Atmosphere: Volumetric natural lighting, warm golden hour or soft diffused indoor ambient light.
- Attire, Posture & Intimacy: Follow the exact clothing style, fabric textures (silk, soft cotton, oversized shirt, cozy knitwear, summer dress, swimwear), neckline, skin exposure, and body posture specified in the description.
- Quality Tokens: 8k resolution, raw color photograph, photorealistic masterpiece, film grain texture, natural reflections.

Output ONLY the raw final English prompt (2-4 rich, descriptive sentences). No preamble, no quotes, no markdown labels.
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


def extract_embedded_image_tag(text: str) -> tuple[str, str | None]:
    """Extracts [IMAGE: description] tag from Sofia's response and returns cleaned text and description."""
    match = IMAGE_TAG_REGEX.search(text)
    if match:
        desc = match.group(1).strip()
        clean_text = IMAGE_TAG_REGEX.sub("", text).strip()
        return clean_text, desc
    return text, None


def extract_image_description(text: str) -> str:
    """Extracts the visual description from user text."""
    lower = text.lower().strip()
    for prefix in ("/image", "/photo", "/draw"):
        if lower.startswith(prefix):
            return text[len(prefix):].strip()

    if lower.startswith("/selfie") or "selfie" in lower:
        return "Sofia taking a warm, candid selfie looking at the camera with a gentle loving smile, soft indoor morning light, casual elegant outfit"

    for phrase in IMAGE_TRIGGER_PHRASES:
        if phrase in lower:
            idx = lower.find(phrase) + len(phrase)
            clean = text[idx:].strip(" ?:.,!-")
            if clean:
                return clean
            return text

    return text


async def craft_visual_prompt(raw_description: str) -> str:
    """Uses Groq LLM to expand a high-level visual description into a masterclass 8k Flux prompt."""
    desc = extract_image_description(raw_description)
    user_turn = f"Convert this scene into a masterclass photographic prompt:\n{desc}"
    try:
        raw = await llm.chat(GROQ_IMAGE_DIRECTOR_SYSTEM, [{"role": "user", "content": user_turn}])
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
