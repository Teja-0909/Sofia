"""Bounded runtime grounding for timers and high-risk action confirmations.

Generated conversation is never an action receipt. This guard is intentionally a
backstop, not a claim to detect every possible hallucination or paraphrase.
"""
import re

# Exclude literal examples rather than deleting words from normal prose. Quotes
# are ignored only as balanced spans; apostrophes in contractions remain prose.
_LITERAL = re.compile(r"```[\s\S]*?```|``[\s\S]*?``|`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”|(?<!\w)'[^'\n]+'(?!\w)|(?:^|\n)\s*>[^\n]*")
_ACTION = re.compile(
    r"\bi(?:'ve| have)?\s+(?:(?:already|just|successfully)\s+)?"
    r"(?:saved|scheduled|created|started|set|cancelled|canceled|snoozed|sent|deleted|removed|updated|marked|completed)\b"
    r"(?:\s+(?:it|that|this)\b|[^.!?\n]{0,100}\b(?:timer|reminder|task|message|email|memory|file|note|goal|sprint|focus)\b)"
    r"|\bi(?:'ll| will| am going to|'m going to)\s+(?:(?:be sure to|make sure to)\s+)?"
    r"(?:remind|ping|nudge|text|message|alert|notify|check (?:in|on))\b"
    r"|\bi(?:'ll| will| am going to|'m going to)\s+(?:set|save|start|schedule|send|delete|update|cancel|mark)\b"
    r"[^.!?\n]{0,80}\b(?:timer|reminder|task|message|email|memory|file|note|goal|sprint)\b"
    r"|\b(?:your|the|this|that|a|my)\s+(?:[\w-]+\s+){0,3}(?:timer|reminder|task|message|email|memory|file|note|goal|sprint)"
    r"(?:\s+(?:is|was|has been|have been)\s+|'s\s+)(?:(?:already|now|successfully|still)\s+)?"
    r"(?:saved|scheduled|created|set|running|ticking|started|cancelled|canceled|sent|deleted|removed|updated|done|completed|active)\b"
    r"|\b(?:timer|reminder)\s+(?:is\s+)?(?:set|running|ticking|started)\b"
    r"|^\s*(?:saved|created|started|scheduled|cancelled|canceled|snoozed|sent|deleted|updated|completed)\s+(?:(?:your|the|a|that|this)\s+)?"
    r"(?:[\w-]+\s+){0,3}(?:timer|reminder|task|message|email|memory|file|note|goal|sprint)\b"
    r"|^\s*(?:timer|reminder|task|message|email|memory|file|note|goal|sprint)(?:\s*#?\d+)?\s+"
    r"(?:saved|scheduled|running|active|completed|done|sent|cancelled|canceled|deleted)\b",
    re.IGNORECASE,
)
_NON_ASSERTION = re.compile(
    r"\b(?:if|unless|when|once|suppose|imagine|hypothetically|example|would|could|might)\b"
    r"|\b(?:no|not|never|don't|doesn't|didn't|haven't|hasn't|can't|cannot|couldn't|wasn't|isn't|won't)\b",
    re.IGNORECASE,
)


def unsupported_action_claim(text: str) -> bool:
    """Detect affirmative action/status claims, preserving quotes and conditionals.

    Evaluate clauses separately so 'I didn't send it, but I saved the timer'
    cannot use the first negation to launder the second positive assertion.
    """
    def mask_literal(match):
        value = match.group()
        if value.startswith("`") or value.lstrip().startswith(">") or _ACTION.search(value.strip('\"“”\'')):
            return " "
        # Quoted noun objects are still objects: I saved "the note" is a claim.
        return value.strip('\"“”\'')
    prose = _LITERAL.sub(mask_literal, text.replace("’", "'"))
    for sentence in re.findall(r"[^.!?\n;]+[.!?\n;]?", prose):
        if sentence.rstrip().endswith("?") and re.match(r"\s*(?:is|are|did|have|has|do|does|can|could|would|when|why|how|what|where)\b", sentence, re.IGNORECASE):
            continue
        # A conditional future offer is not an unsolicited commitment. An
        # unrelated conditional preface cannot validate a past-tense receipt.
        conditional = bool(re.match(r"\s*(?:if|unless|when|once)\b", sentence, re.IGNORECASE))
        for clause in re.split(r",|\b(?:but|however|and)\b", sentence, flags=re.IGNORECASE):
            for match in _ACTION.finditer(clause):
                if _NON_ASSERTION.search(clause[:match.start()]):
                    continue
                if conditional and (re.match(r"i(?:'ll| will| am going to|'m going to)\b", match.group(), re.IGNORECASE)
                                    or (re.search(r"\b(?:create|start|set|save)\b", sentence.split(",")[0], re.IGNORECASE)
                                        and not re.match(r"i\b", match.group(), re.IGNORECASE))):
                    continue
                return True
    return False


UNVERIFIED_ACTION_REPLY = (
    "I don't have a verified result for that action, so I can't say it's saved, running or sent. "
    "For a timer, say ‘set a timer for 20 minutes’; /tasks shows saved reminders, "
    "and /done or /cancel needs the task ID."
)


def guard_generated_reply(text: str, *, background: bool = False) -> str:
    """Backstop for common unsupported action claims at generated-text boundaries.

    Real mutations are acknowledged by deterministic handlers from saved rows or
    explicit operation results, outside this function. We replace an unsupported
    draft as a whole instead of selectively rewriting its factual meaning.
    """
    if unsupported_action_claim(text):
        return "PASS" if background else UNVERIFIED_ACTION_REPLY
    return text
