import asyncio
import json

from . import llm
from . import search as search_module
from . import tasks as tasks_module
from .orchestrator_context import _clean_asterisks
from .orchestrator_globals import (
    LAZY_CODE_PATTERNS,
    TOOLS,
    TRACES_MODE,
    _tool_lock,
    logger,
)


async def _verify_and_refine_draft(
    draft: str,
    user_text: str,
    system: str,
    messages: list[dict]
) -> str:
    """Pre-flight verification loop: catches laziness, placeholders, and action pretense before sending to user."""
    clean_draft = _clean_asterisks(draft)

    # 1. Check for Code Laziness / Placeholders
    has_lazy_placeholder = any(p.search(clean_draft) for p in LAZY_CODE_PATTERNS)
    if has_lazy_placeholder:
        logger.warning("Verifier loop triggered: Found lazy placeholder in draft. Requesting full completion...")
        critique_msg = (
            "Please fully implement the code. Your draft contained a lazy placeholder or skipped code implementation. "
            "Provide the 100% complete, fully implemented, working solution with zero placeholders or omissions."
        )
        try:
            retry_messages = list(messages) + [
                {"role": "assistant", "content": clean_draft},
                {"role": "user", "content": critique_msg}
            ]
            refined, _ = await llm.chat(system, retry_messages)
            clean_draft = _clean_asterisks(refined)
        except Exception as exc:
            logger.warning("Verification retry note: %s", exc)



    return clean_draft


def _generate_situational_directive(system: str, messages: list[dict], user_text: str) -> str:
    history_text = ""
    skip_done = False
    filtered_messages = []
    for msg in reversed(messages):
        if not skip_done and msg.get("role") == "user" and msg.get("content") == user_text:
            skip_done = True
            continue
        filtered_messages.append(msg)
        
    for msg in reversed(filtered_messages):
        role = msg.get("role", "user").capitalize()
        content = msg.get("content", "")
        history_text += f"{role}: {content}\n"
    
    directive = f"{system}\n\n[CONVERSATION HISTORY]\n{history_text}\n[USER MESSAGE]\n<user_message>{user_text}</user_message>"
    return directive


async def _generate(system: str, messages: list[dict], user_text: str = "") -> str:
    current_messages = list(messages)
    max_tool_turns = 6
    
    for _ in range(max_tool_turns):
        text, tool_calls = await llm.chat(system, current_messages, tools=TOOLS)
        
        if not tool_calls:
            is_banter = len(user_text.strip()) < 25 and '?' not in user_text and not any(k in user_text.lower() for k in ['how', 'what', 'why', 'can you', 'analyze', 'debug', 'research', 'explain'])
            if not user_text or is_banter:
                if not TRACES_MODE:
                    return await _verify_and_refine_draft(text, user_text, system, current_messages)
                
            directive = _generate_situational_directive(system, current_messages, user_text)
            
            router_schema = {
                "type": "json_schema",
                "json_schema": {
                    "name": "router_dispatch",
                    "schema": {
                        "type": "object",
                        "properties": {
                            "direct": {"type": "boolean", "description": "true if this is a casual/simple message that can be answered directly without specialist agents (greetings, short banter, acknowledgements). false if the message needs analysis, research, technical help, emotional depth, or any non-trivial reasoning."},
                            "architect": {"type": "boolean"},
                            "researcher": {"type": "boolean"},
                            "empath": {"type": "boolean"}
                        },
                        "required": ["direct", "architect", "researcher", "empath"]
                    }
                }
            }
            
            router_msg = [{"role": "user", "content": directive + "\n\nAs the Forebrain Router, evaluate the user message. Set 'direct' to true ONLY for casual greetings, simple acknowledgements, or trivial banter that need no specialist analysis. For anything requiring thought, research, emotional depth, or technical reasoning, set 'direct' to false and activate the appropriate specialists."}]
            if current_messages and "image_bytes" in current_messages[-1]:
                router_msg[-1]["image_bytes"] = current_messages[-1]["image_bytes"]
                router_msg[-1]["media_bytes"] = current_messages[-1].get("media_bytes")
                router_msg[-1]["mime_type"] = current_messages[-1].get("mime_type", "image/jpeg")
            router_text, _ = await llm.chat("You are the Forebrain Router. Respond ONLY in valid JSON.", router_msg, response_format=router_schema)
            try:
                dispatch = json.loads(router_text)
            except Exception:
                dispatch = {"direct": False, "architect": True, "researcher": True, "empath": True}
            
            # LLM-driven bypass: if the Router says 'direct' and no specialists needed, skip MoA
            if dispatch.get("direct") and not any(dispatch.get(k) for k in ("architect", "researcher", "empath")):
                if not TRACES_MODE:
                    return await _verify_and_refine_draft(text, user_text, system, current_messages)
                # In TRACES_MODE, override direct so we can see full pipeline
                dispatch["empath"] = True
                
            tasks = []
            
            async def run_specialist(role_name: str, prompt_addition: str) -> str:
                spec_system = (
                    f"You are the {role_name} analysis module inside a multi-agent system. "
                    "Rules:\n"
                    "1. You are NOT the final assistant. You are an internal module writing PRIVATE NOTES for a Synthesizer.\n"
                    "2. Please do not write a message addressed to the user. Avoid saying 'you' referring to the user.\n"
                    "3. Write in third person analytical voice: 'The user is asking about...', 'Key observations:', 'Recommended approach:'.\n"
                    "4. Output format: <thought>[your 5-step reasoning]</thought> then your concise analytical notes.\n\n"
                    "THINKING PROTOCOL:\n"
                    "<thought>\n"
                    "1. UNDERSTAND: What is the user asking?\n"
                    "2. ASSESS: What do I know? What am I uncertain about?\n"
                    "3. PLAN: How should I approach this?\n"
                    "4. EXECUTE: My analysis.\n"
                    "5. VERIFY: Does this address the question? Any gaps?\n"
                    "</thought>\n"
                    "Then write your specialist notes."
                )
                
                # Only pass conversation context, NOT Sofia's personality directive
                last_user_msg = user_text or ""
                spec_msg = [{"role": "user", "content": f"Analyze this user message and write internal {role_name.lower()} notes.\n\nUser message: \"{last_user_msg}\"\n\n{prompt_addition}"}]
                if current_messages and "image_bytes" in current_messages[-1]:
                    spec_msg[-1]["image_bytes"] = current_messages[-1]["image_bytes"]
                    spec_msg[-1]["media_bytes"] = current_messages[-1].get("media_bytes")
                    spec_msg[-1]["mime_type"] = current_messages[-1].get("mime_type", "image/jpeg")
                spec_text, _ = await llm.chat(spec_system, spec_msg)
                return f"[{role_name} Output]\n{spec_text}"
                
            if dispatch.get("architect"):
                tasks.append(run_specialist("Architect", "Focus on: technical structure, architecture, logical planning, and implementation strategy."))
            if dispatch.get("researcher"):
                tasks.append(run_specialist("Researcher", "Focus on: factual information, context, documentation, and evidence-based analysis."))
            if dispatch.get("empath"):
                tasks.append(run_specialist("Empath", "Focus on: emotional tone, relationship dynamics, mood assessment, what kind of energy the user needs right now, and maintaining genuine honesty. You are explicitly permitted to disagree, hold your own opinions, and say 'I don't know' if you are unsure."))
                
            specialist_outputs = []
            if tasks:
                specialist_outputs = await asyncio.gather(*tasks)
            
            synth_system = system + "\n\nYou are the Synthesizer. Weave the specialist outputs together into a cohesive, single-voiced response. IMPORTANT: The specialist outputs contain <thought>...</thought> blocks. Read them for context, but please do not output or leak the thought blocks into your final response. Do not repeat the same conversational point twice."
            synth_content = f"{directive}\n\n[SPECIALIST OUTPUTS]\n" + "\n\n".join(specialist_outputs)
            synth_msg = [{"role": "user", "content": synth_content}]
            if current_messages and "image_bytes" in current_messages[-1]:
                synth_msg[-1]["image_bytes"] = current_messages[-1]["image_bytes"]
                synth_msg[-1]["media_bytes"] = current_messages[-1].get("media_bytes")
                synth_msg[-1]["mime_type"] = current_messages[-1].get("mime_type", "image/jpeg")
            
            final_text, _ = await llm.chat(synth_system, synth_msg)
            
            # Component 1: The Critic Agent
            is_question = '?' in user_text or any(k in user_text.lower() for k in ('how', 'what', 'why', 'can you', 'analyze', 'debug', 'research', 'explain'))
            if tasks and is_question and len(final_text) > 200:
                critic_schema = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "critic_verdict",
                        "schema": {
                            "type": "object",
                            "properties": {
                                "pass": {"type": "boolean"},
                                "confidence": {"type": "number"},
                                "issues": {"type": "array", "items": {"type": "string"}},
                                "suggestions": {"type": "array", "items": {"type": "string"}}
                            },
                            "required": ["pass", "confidence", "issues", "suggestions"]
                        }
                    }
                }
                critic_system = "You are the Critic. Evaluate the synthesized response against the user's message for factual gaps, logical consistency, completeness, and depth. Return JSON."
                critic_user_msg = f"User Message:\n{user_text}\n\nDraft Response:\n{final_text}\n\nEvaluate the draft."
                critic_resp, _ = await llm.chat(critic_system, [{"role": "user", "content": critic_user_msg}], response_format=critic_schema)
                try:
                    verdict = json.loads(critic_resp)
                    passed = verdict.get("pass", True)
                    conf = verdict.get("confidence", 1.0)
                    if not passed and conf < 0.8:
                        issues = verdict.get("issues", [])
                        suggestions = verdict.get("suggestions", [])
                        refine_sys = synth_system + f"\n\nCRITIC FEEDBACK: The previous draft was rejected.\nOriginal Draft:\n{final_text}\n\nIssues: {issues}\nSuggestions: {suggestions}\nRewrite the response to address these issues."
                        final_text, _ = await llm.chat(refine_sys, synth_msg)
                except Exception:
                    pass
            
            if TRACES_MODE and tasks:
                trace_logs = []
                trace_logs.append(f"**🧠 Forebrain Router:**\n```json\n{router_text}\n```")
                for so in specialist_outputs:
                    trace_logs.append(f"**{so.split(' Output]')[0].strip('[')} Specialist:**\n{so.split(' Output]')[1].strip()}")
                if 'critic_resp' in locals():
                    trace_logs.append(f"**⚖️ Critic Verdict:**\n```json\n{critic_resp}\n```")
                
                trace_str = "\n\n---\n### 🔬 Internal MoA Traces\n\n" + "\n\n".join(trace_logs)
                final_text += trace_str

            return await _verify_and_refine_draft(final_text, user_text, system, current_messages)
            
        assist_msg = {"role": "assistant", "content": text or ""}
        assist_msg["tool_calls"] = tool_calls
        current_messages.append(assist_msg)
        
        async with _tool_lock:
            for call in tool_calls:
                func = call["function"]
                name = func["name"]
                try:
                    args = func.get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    if not isinstance(args, dict):
                        args = {}
                    result = ""
                    if name == "sofia_search_web" or name == "search_web":
                        if args.get("deep_research"):
                            result = await search_module.react_research_loop(args["query"])
                            if not result:
                                result = "No useful results found for this query."
                        else:
                            search_results = await search_module.search_web(args["query"])
                            if not search_results:
                                result = "No useful results found for this query."
                            else:
                                result = "\n".join(f"[{i+1}] {r['title']}\nURL: {r['url']}\nSnippet: {r['snippet']}\n" for i, r in enumerate(search_results))
                    elif name == "read_webpage":
                        result = await search_module.fetch_page_content(args["url"], max_chars=4000)
                        if not result:
                            result = "Could not fetch content from this URL or page is empty."
                    elif name == "schedule_proactive_message":
                        await tasks_module.schedule_proactive_message(args["message"], args["due_time"])
                        result = "Successfully scheduled the proactive message."
                    elif name == "desktop_point_at":
                        from . import vision_session
                        result = await vision_session.point_at(
                            norm_x=int(args.get("x", 500)),
                            norm_y=int(args.get("y", 500)),
                            label=str(args.get("label", "")),
                            duration_seconds=int(args.get("duration_seconds", 5)),
                        )
                    elif name == "desktop_doodle":
                        from . import vision_session
                        result = await vision_session.doodle(
                            shape=str(args.get("shape", "heart")),
                            norm_x=int(args.get("x", 500)),
                            norm_y=int(args.get("y", 500)),
                            duration_seconds=int(args.get("duration_seconds", 6)),
                        )
                    elif name == "desktop_sticky_note":
                        from . import vision_session
                        result = await vision_session.sticky_note(
                            text=str(args.get("text", "💕")),
                            position=str(args.get("position", "top_right")),
                            duration_seconds=int(args.get("duration_seconds", 8)),
                        )
                    elif name == "desktop_clear_overlay":
                        from . import vision_session
                        result = await vision_session.clear_overlay()
                    elif name == "desktop_capture_screen":
                        from . import vision_session
                        frame = await vision_session.request_screen_capture(reason=str(args.get("reason", "Inspection")))
                        if frame:
                            result = "Screen captured successfully. Frame received from Windows sidecar."
                        else:
                            result = "Could not capture screen (PC sidecar offline or sensitive window active)."
                    elif name == "desktop_run_command":
                        from . import vision_session
                        result = await vision_session.run_desktop_command(
                            command=str(args.get("command", "")),
                            cwd=args.get("cwd") or None,
                            timeout_seconds=int(args.get("timeout_seconds", 15)),
                        )
                    elif name == "desktop_read_clipboard":
                        from . import vision_session
                        result = await vision_session.read_desktop_clipboard()
                    elif name == "desktop_set_clipboard":
                        from . import vision_session
                        result = await vision_session.set_desktop_clipboard(
                            text=str(args.get("text", "")),
                        )
                    elif name == "desktop_workspace_status":
                        from . import vision_session
                        result = await vision_session.get_desktop_workspace_status(
                            workspace_dir=args.get("workspace_dir") or None,
                        )
                    elif name == "mark_task_done":
                        task_id = args.get("task_id")
                        if task_id is None:
                            result = "Error: task_id is required."
                        else:
                            success = await tasks_module.mark_done(int(task_id))
                            if success:
                                result = f"Successfully marked task #{task_id} as done."
                            else:
                                result = f"Error: Could not mark task #{task_id} as done (not found or already done)."
                    elif name == "create_task":
                        desc = args.get("description")
                        due = args.get("due_time")
                        if not desc or not due:
                            result = "Error: description and due_time are required."
                        else:
                            try:
                                task_id = await tasks_module.create_task(desc, due)
                                result = f"Successfully created task #{task_id} ('{desc}') due at {due}."
                            except Exception as e:
                                result = f"Error creating task: {e}"
                    elif name == "list_tasks":
                        pending = await tasks_module.list_pending()
                        if not pending:
                            result = "No pending tasks."
                        else:
                            result = "Pending tasks:\n" + "\n".join(f"- #{t['id']}: '{t['description']}' due {t['due_time']}" for t in pending)
                    elif name == "check_pc_presence":
                        from . import orchestrator_context
                        result = await orchestrator_context._ctx_pc_presence()
                    elif name == "check_git_log":
                        from . import orchestrator_context
                        result = orchestrator_context._ctx_git_log()
                    else:
                        result = f"Error: unknown function {name}"
                except Exception as e:
                    result = f"Error executing tool: {e}"
                
                current_messages.append({
                    "role": "tool",
                    "name": name,
                    "content": str(result),
                    "tool_call_id": call["id"]
                })
            
    # Fallback if too many tool calls
    text, _ = await llm.chat(system, current_messages)
    if user_text:
        return await _verify_and_refine_draft(text, user_text, system, current_messages)
    return _clean_asterisks(text)


