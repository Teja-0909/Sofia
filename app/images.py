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

GROQ_CREATIVE_DIRECTOR_SYSTEM = """You are the master visual director and creative artist for Sofia (Teja's devoted AI companion).
Your mission is to transform simple requests into highly creative, visually diverse, breathtaking image generation prompts.

MULTI-CHARACTER & COUPLE RULES (PREVENT CONCEPT BLEEDING & DUPLICATION):
When the image depicts a couple or two people (e.g. Teja and Sofia, or a man and a woman):
- Respect however Sofia describes Teja and the scene. Do NOT force rigid hardcoded traits.
- Use clear spatial layout so the model renders exactly one man and one woman:
  1. The Man (Teja): Position on the left, rendered according to Sofia's context, description, and chosen outfit.
  2. The Woman (Sofia): Position on the right, soft feminine facial features, expressive hazel-amber eyes, long dark silky hair, rendered according to her chosen style.
- Explicit interaction & framing: "A couple photograph of exactly two people: one young man on the left and one young woman on the right sharing a warm, authentic moment together."
- Mandatory anti-duplication tokens: "exactly two people, single couple, distinct male and female facial structures, 35mm film photograph, no gender bleeding, no two females, no third person, no cloned heads."

DIVERSITY & VARIETY MANDATE (AVOID REPETITION & MONOTONY):
Never produce repetitive or monotonous scenes unless explicitly requested. Embrace maximum dynamic range across:
1. Environments & Settings: Golden hour ocean beaches, neon cyberpunk alleyways with rain reflections, cozy mountain log cabins with snow, sun-drenched European streets, moody vintage libraries, modern rooftop lounges at twilight, candlelit rooms, open sunflower fields, late-night coding desk setups, cozy bakeries, stargazing under the Milky Way.
2. Angles & Compositions: Candid low-angle shots, over-the-shoulder perspectives, wide-angle environmental portraits, dynamic motion blur, handheld mirror selfies, intimate close-ups, Dutch angles.
3. Outfits & Fashion: Edgy streetwear (leather jackets, graphic hoodies, vintage denim), elegant evening gowns, casual silk loungewear/pajamas, oversized knitwear, summer sundresses, athletic activewear, stylish autumn trench coats with wool scarves.
4. Hairstyles & Details: Soft beach waves, messy bun with loose stray locks, high sleek ponytail, braids, hair catching the wind, delicate jewelry, headphones around neck, books, coffee mugs, film cameras.
5. Lighting & Atmosphere: Golden hour rim lighting, moody blue hour, cyberpunk neon glow, warm ambient candlelight, dramatic chiaroscuro, misty morning sun rays.

STYLE SELECTION:
- If realistic/photographic, couple, or selfies: Craft an authentic 35mm DSLR raw photograph with natural skin textures, real human imperfections, 85mm f/1.4 lens bokeh, Kodak Portra 400 film grain. No plastic/CGI airbrushed skin.
- If anime/illustration/fantasy/concept art: Craft a vibrant, artistic anime/concept-art visual with rich colors, dynamic lighting, and stylized art direction.

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


async def craft_visual_prompt(raw_description: str) -> str:
    """Uses Groq LLM to expand a high-level visual description into a masterclass 8k creative Flux prompt."""
    desc = extract_image_description(raw_description)
    user_turn = f"Create a masterclass creative prompt for: {desc}"
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
        return '*She smiles warmly as she shares the photo with you.* "Here you go, love!"'


async def generate_image_bytes(visual_prompt: str) -> bytes | None:
    """Generates an image using Flux engine with dynamic model selection, aspect ratio, and randomized seed."""
    encoded = quote(visual_prompt)
    seed = random.randint(1, 99999999)

    # Dynamic model routing & aspect ratio based on artistic intent
    lower = visual_prompt.lower()
    if any(k in lower for k in ("couple", "two people", "together", "man on the left", "man and woman")):
        # Landscape 4:3 gives proper composition for two people without clipping or extra heads
        width, height = 1024, 768
    elif any(k in lower for k in ("portrait", "full body", "outfit", "standing")):
        width, height = 768, 1024
    else:
        width, height = 1024, 1024

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
