"""Bounded runtime grounding for timers and high-risk action confirmations.

Generated conversation is never an action receipt. This guard is intentionally a
backstop, not a claim to detect every possible hallucination or paraphrase.
"""
import re

# Exclude literal examples rather than deleting words from normal prose. Quotes
# are ignored only as balanced spans; apostrophes in contractions remain prose.
_LITERAL = re.compile(r"```[\s\S]*?```|``[\s\S]*?``|`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”|‘[^’'\n]*[’']|(?<!\w)'[^'\n]+'(?!\w)|(?:^|\n)\s*>[^\n]*")
_ACTION = re.compile(
    r"\bi(?:'ve| have)?\s+(?:(?:already|just|successfully)\s+)?"
    r"(?:saved|scheduled|created|started|set|cancelled|canceled|snoozed|sent|deleted|removed|updated|marked|completed)\b"
    r"(?:\s+(?:it|that|this)\b|[^.!?\n]{0,100}\b(?:timer|reminder|task|message|email|memory|file|note|goal|sprint|focus|outcome|priority|checkpoint|check-in)\b)"
    r"|\bi(?:'ll| will| am going to|'m going to)\s+(?:(?:be sure to|make sure to)\s+)?"
    r"(?:remind|ping|nudge|text|message|alert|notify|check (?:in|on))\b"
    r"|\bi(?:'ll| will| am going to|'m going to)\s+(?:set|save|start|schedule|send|delete|update|cancel|mark)\b"
    r"[^.!?\n]{0,80}\b(?:timer|reminder|task|message|email|memory|file|note|goal|sprint|outcome|priority|checkpoint|check-in)\b"
    r"|\b(?:your|the|this|that|a|my)\s+(?:[\w-]+\s+){0,3}(?:timer|reminder|task|message|email|memory|file|note|goal|sprint|outcome|priority|checkpoint|check-in)"
    r"(?:\s+(?:is|was|has been|have been)\s+|'s\s+)(?:(?:already|now|successfully|still)\s+)?"
    r"(?:saved|scheduled|created|set|running|ticking|started|cancelled|canceled|sent|deleted|removed|updated|done|completed|active)\b"
    r"|\b(?:timer|reminder)\s+(?:is\s+)?(?:set|running|ticking|started)\b"
    r"|^\s*(?:saved|created|started|scheduled|cancelled|canceled|snoozed|sent|deleted|updated|completed)\s+(?:(?:your|the|a|that|this)\s+)?"
    r"(?:[\w-]+\s+){0,3}(?:timer|reminder|task|message|email|memory|file|note|goal|sprint|outcome|priority|checkpoint|check-in)\b"
    r"|^\s*(?:timer|reminder|task|message|email|memory|file|note|goal|sprint|outcome|priority|checkpoint|check-in)(?:\s*#?\d+)?\s+"
    r"(?:saved|scheduled|running|active|completed|done|sent|cancelled|canceled|deleted)\b",
    re.IGNORECASE,
)
_DURATION = (
    r"(?:\d+(?:\.\d+)?|(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|half|few|several)"
    r"(?:[ -](?:one|two|three|four|five|six|seven|eight|nine))?)"
    r"[\s-]*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?)"
)
_IMPLICIT_COUNTDOWN = re.compile(
    rf"\b{_DURATION}\s+(?:start(?:s|ing)?|begin(?:s|ning)?)\s+(?:right\s+)?now\b"
    r"|\b(?:the|your|a)\s+countdown\s+(?:starts?|begins?|is ticking|is running)\b"
    r"|\bi(?:'m| am| will|'ll)\s+(?:keeping track|keep track|tracking|timing|counting)"
    r"[^.!?\n]{0,80}\b(?:time|timer|countdown|seconds|minutes)\b", re.IGNORECASE,
)
_TIMER_TOPIC = re.compile(r"\b(?:timer|countdown)\b", re.IGNORECASE)
_TIMER_PROGRESS = re.compile(
    r"\bi(?:'m| am| will|'ll)\s+(?:keeping track|keep track)"
    r"(?=\s*(?:$|[.!?]|right here\b|for you\b|with you\b|of (?:it|that|the timer|your timer|your break)\b))"
    r"|^\s*(?:starting|counting|timing)\s+(?:right\s+)?now\b"
    r"|\bi(?:'ll| will)\s+(?:let you know|tell you|be back|come back)\s+(?:when|in|after|once|later)\b",
    re.IGNORECASE,
)
_TIMER_DENIAL = re.compile(
    r"\bi(?:\s+can(?:not|'t)|(?:\s+am|'m)\s+(?:unable|not able)\s+to)\s+"
    r"(?:directly\s+)?(?:set|start|create|schedule)\s+(?:(?:a|your|chat|real|actual)\s+)?timers?\b"
    r"|\btimers?\s+(?:can only|must|have to)\s+be\s+(?:set|started|created)\s+(?:using|with|through)\b",
    re.IGNORECASE,
)


def _mask_literals(text: str) -> str:
    def mask(match):
        value = match.group()
        inner = value.strip('\"“”‘’\'')
        if (value.startswith("`") or value.lstrip().startswith(">") or _ACTION.search(inner)
                or _IMPLICIT_COUNTDOWN.search(inner) or _TIMER_PROGRESS.search(inner) or _TIMER_DENIAL.search(inner)):
            return " "
        return inner
    return _LITERAL.sub(mask, text.replace("’", "'"))


_NON_ASSERTION = re.compile(
    r"\b(?:if|unless|when|once|suppose|imagine|hypothetically|example|would|could|might)\b"
    r"|\b(?:no|not|never|don't|doesn't|didn't|haven't|hasn't|can't|cannot|couldn't|wasn't|isn't|won't)\b",
    re.IGNORECASE,
)


def unsupported_action_claim(text: str, *, user_text: str = "") -> bool:
    """Detect affirmative action/status claims, preserving quotes and conditionals.

    Evaluate clauses separately so 'I didn't send it, but I saved the timer'
    cannot use the first negation to launder the second positive assertion.
    """
    prose = _mask_literals(text)
    timer_topic = bool(_TIMER_TOPIC.search(user_text))
    for sentence in re.findall(r"[^.!?\n;]+[.!?\n;]?", prose):
        if sentence.rstrip().endswith("?") and re.match(r"\s*(?:is|are|did|have|has|do|does|can|could|would|when|why|how|what|where)\b", sentence, re.IGNORECASE):
            continue
        # A conditional future offer is not an unsolicited commitment. An
        # unrelated conditional preface cannot validate a past-tense receipt.
        conditional = bool(re.match(r"\s*(?:if|unless|when|once)\b", sentence, re.IGNORECASE))
        for clause in re.split(r",|\b(?:but|however|and)\b", sentence, flags=re.IGNORECASE):
            patterns = (_ACTION, _IMPLICIT_COUNTDOWN, _TIMER_PROGRESS) if timer_topic else (_ACTION, _IMPLICIT_COUNTDOWN)
            matches = [match for pattern in patterns for match in pattern.finditer(clause)]
            for match in matches:
                if _NON_ASSERTION.search(clause[:match.start()]):
                    continue
                if conditional and (re.match(r"i(?:'ll| will| am going to|'m going to)\b", match.group(), re.IGNORECASE)
                                    or (re.search(r"\b(?:create|start|set|save)\b", sentence.split(",")[0], re.IGNORECASE)
                                        and not re.match(r"i\b", match.group(), re.IGNORECASE))):
                    continue
                return True
    return False


def _false_timer_capability_denial(prose: str) -> bool:
    """Correct blanket chat denial, not valid limits, failures or hypotheticals."""
    for clause in re.split(r"[.!?\n;]+|\bbut\b", prose, flags=re.IGNORECASE):
        for match in _TIMER_DENIAL.finditer(clause):
            if _NON_ASSERTION.search(clause[:match.start()]):
                continue
            suffix = clause[match.end():].strip()
            if not suffix or re.match(
                r"^(?:(?:directly\s+)?(?:in|through|via)\s+(?:this\s+)?chat\b|here\b|"
                r"unless\s+you\s+use\b.*\bcommand\b)", suffix, re.IGNORECASE,
            ):
                return True
    return False


UNVERIFIED_ACTION_REPLY = (
    "I don't have a verified result for that action, so I can't say it's saved, running or sent. "
    "For a timer, say ‘set a timer for 20 minutes’; /tasks shows saved reminders, "
    "and /done or /cancel needs the task ID."
)


def guard_generated_reply(text: str, *, background: bool = False, user_text: str = "") -> str:
    """Backstop for common unsupported action claims at generated-text boundaries.

    Real mutations are acknowledged by deterministic handlers from saved rows or
    explicit operation results, outside this function. We replace an unsupported
    draft as a whole instead of selectively rewriting its factual meaning.
    """
    if unsupported_action_claim(text, user_text=user_text):
        return "PASS" if background else UNVERIFIED_ACTION_REPLY
    # A false capability denial is different from an action-success claim.
    # The application supports chat timers even though model tools are read-only.
    if _false_timer_capability_denial(_mask_literals(text)):
        if background:
            return "PASS"
        from .timer_requests import TIMER_CAPABILITY
        return TIMER_CAPABILITY
    return text
