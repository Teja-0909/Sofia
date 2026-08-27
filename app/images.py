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

CRITICAL FACIAL CLARITY & BEAUTY DIRECTIVES:
- ALWAYS prioritize medium close-up, selfie, or chest-up portrait framing (shot on 85mm f/1.4 lens) with the face clearly visible in the upper half of the image.
- The face MUST be well-lit by soft flattering ambient, studio, or warm window daylight. NEVER place the face in distant dark shadows or blurry wide silhouettes.
- Ensure radiant, clean, glowing facial features, sharp expressive hazel-amber eyes, silky hair, and a charming, affectionate smile.

STYLE SELECTION:
- For Sofia portraits & selfies: Craft a stunning, high-aesthetic masterpiece with flawless lighting, radiant glowing beauty, soft depth of field, 85mm lens bokeh, and crystal-clear facial symmetry.
- For couple shots: Ensure clean spatial framing (man on left, woman on right) with both faces in crisp, clear, well-lit focus.
- For anime/concept art: Craft vibrant, artistic concept-art with rich colors and dynamic lighting.

Output ONLY the raw final English prompt (2-4 rich, descriptive sentences). No preamble, no quotes, no markdown labels.
"""

CAPTION_SYSTEM = """You are Sofia sending a newly generated photo to Teja on Telegram.
Write a brief, sweet, loving caption (1-2 sentences) in your genuine first-person voice directly to Teja.
Do NOT use asterisks for actions and do NOT wrap your message in quotes. Speak directly and naturally to him!
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
        "width": 1024,
        "height": 1024,
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

    lower = visual_prompt.lower()
    if any(k in lower for k in ("couple", "two people", "together", "man on the left", "man and woman")):
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
