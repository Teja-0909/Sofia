# Direct, natural replies

Sofia should answer the current message clearly, keep her warmth, and stop when the reply is complete. A question is useful when an important detail, user decision, or permission is genuinely missing, or when the user asked for questions. Keeping the conversation going is not itself a reason to ask one.

## Scope

- `system_prompt.txt` makes stopping after a useful answer explicit and avoids automatic next steps or checkpoint suggestions
- `orchestrator_context.REPLY_STYLE_POLICY` supplies one shared response contract after the base persona and event policy. Ordinary chat, direct progress acknowledgements, proactive generation and screen-watch generation receive it, including when the application uses a custom `SYSTEM_PROMPT_PATH`
- Mood hints change tone, not the length of a response or whether it needs a closing question
- Progress acknowledgements stay specific and brief. Background progress questions require an agreed checkpoint and all existing contact guards
- `/focus clear` now gives the saved-state receipt without a canned “what next?” question. The `/focus done` model-error fallback reports the completed sprint briefly rather than falling back to generic praise

No question-mark filter, reply truncation, extra model call, provider/model change, planner state, schema migration or new scheduling behavior is introduced. Requested detail, quotations, code, real clarification/permission/safety questions and action receipts remain intact. Timer, reminder, clock and action-grounding code is unchanged.

## Representative acceptance scenarios

These are example targets for review, not claims of observed live model output. Exact wording can vary.

1. **Acknowledgement:** “got it” → “Sounds good”. Do not attach “Anything else?”
2. **Direct answer:** “What is 9 times 7?” → “63”. Do not add a quiz offer
3. **Needed clarification:** Two drafts are available and the user asks to review “the draft” → “Which draft should I review: the proposal or the email?”
4. **Care without interrogation:** “I’m taking a break” → “Enjoy the breather”. Do not turn chosen rest into a status interview
5. **Specific progress:** “I finished the introduction” → “The introduction’s done. Nice.” Do not append another task or generic motivational speech
6. **Agreed checkpoint:** The user previously agreed to a progress check, it is now due, the issue remains open and contact guards allow it → one short, relevant progress/blocker question. Without agreement or another meaningful new reason to contact the user, use `PASS`
7. **Needed permission or safety question:** Ask the question even though concise replies are preferred. Style never substitutes for consent or safety
8. **Requested depth:** A detailed explanation or complete code remains complete, with enough context to understand uncertainties and risks

## Verification and limitations

Offline tests check the actual assembled prompts across chat, progress, proactive and watch routes; confirm representative mock replies pass through unchanged; preserve necessary questions, code punctuation and requested long answers; and exercise deterministic command receipts. Existing persistence, background-contact, security, action-grounding, reminder and timer regressions remain part of the full suite.

These checks prove policy wiring and deterministic behavior, not that a probabilistic model will always choose good wording. No paid live model calls were made. After an independently authorized deployment, use the scenarios above in a supervised conversation with the configured model, including old chat history that contains habitual closing questions. Check warmth and useful detail as well as brevity.
