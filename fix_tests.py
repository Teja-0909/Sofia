import os

# 1. test_focus_and_time.py
tf_path = "tests/test_focus_and_time.py"
with open(tf_path, "r", encoding="utf-8") as f:
    text = f.read()

# _ctx_tasks_and_threads and _ctx_time_mood were likely moved to orchestrator_context.py
text = text.replace("orchestrator._ctx_tasks_and_threads", "orchestrator_context._ctx_tasks_and_threads")
text = text.replace("orchestrator._ctx_time_mood", "orchestrator_context._ctx_time_mood")
# also need to import orchestrator_context
if "from app import orchestrator_context" not in text:
    text = text.replace("from app import orchestrator", "from app import orchestrator, orchestrator_context")

with open(tf_path, "w", encoding="utf-8") as f:
    f.write(text)

# 2. test_alisa_core.py
tac_path = "tests/test_alisa_core.py"
with open(tac_path, "r", encoding="utf-8") as f:
    text = f.read()
# test_orchestrator_image_reply patching orchestrator.reply
text = text.replace("app.orchestrator.reply", "app.orchestrator_moa.reply")
with open(tac_path, "w", encoding="utf-8") as f:
    f.write(text)

# 3. test_file_handling.py
tfh_path = "tests/test_file_handling.py"
with open(tfh_path, "r", encoding="utf-8") as f:
    text = f.read()
text = text.replace("app.orchestrator.reply", "app.orchestrator_moa.reply")
with open(tfh_path, "w", encoding="utf-8") as f:
    f.write(text)

# 4. test_triggers_update.py
ttu_path = "tests/test_triggers_update.py"
with open(ttu_path, "r", encoding="utf-8") as f:
    text = f.read()
text = text.replace("app.bot._log_message", "app.bot_core._log_message")
# check if it's in bot_core or bot_globals
with open(ttu_path, "w", encoding="utf-8") as f:
    f.write(text)

# 5. test_vision_tools.py
tvt_path = "tests/test_vision_tools.py"
with open(tvt_path, "r", encoding="utf-8") as f:
    text = f.read()
text = text.replace("app.orchestrator.reply", "app.orchestrator_moa.reply")
text = text.replace("orchestrator.TOOLS", "orchestrator_moa.TOOLS")
if "from app import orchestrator_moa" not in text:
    text = text.replace("from app import orchestrator", "from app import orchestrator, orchestrator_moa")
with open(tvt_path, "w", encoding="utf-8") as f:
    f.write(text)

# check where _log_message actually is
for root, dirs, files in os.walk("app"):
    for file in files:
        if file.endswith(".py"):
            with open(os.path.join(root, file), "r", encoding="utf-8") as f:
                content = f.read()
                if "def _log_message" in content:
                    print(f"_log_message found in {file}")
                if "def _ctx_tasks_and_threads" in content:
                    print(f"_ctx_tasks_and_threads found in {file}")
                if "TOOLS = [" in content:
                    print(f"TOOLS found in {file}")
