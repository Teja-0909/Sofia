import asyncio
import logging
import re

logger = logging.getLogger(__name__)
_CHARS_PER_TOKEN = 4
_MAX_CONTEXT_TOKENS = 120_000  # conservative ceiling for Gemini Flash
FALLBACK_MESSAGE = "give me a second, having some trouble connecting"
TRACES_MODE = False
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "sofia_search_web",
            "description": "Searches the web and returns a list of titles, snippets, and URLs. Use this first to find relevant links.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query (e.g. 'F1 race results 2024')"},
                    "deep_research": {"type": "boolean", "description": "Set to true for complex research questions to use the ReAct research loop."}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_webpage",
            "description": "Reads the full content of a specific webpage URL. Use this to dive deeper into a link found via search_web.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The full URL of the page to read (e.g. 'https://en.wikipedia.org/wiki/...')"}
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_proactive_message",
            "description": "Schedules a message that you will autonomously send to Teja at a future time.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "What you want to say to him."},
                    "due_time": {"type": "string", "description": "The ISO 8601 UTC time to send the message (YYYY-MM-DDTHH:MM:SSZ)."}
                },
                "required": ["message", "due_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_point_at",
            "description": "Points an animated glowing target arrow at coordinate (x, y) on Teja's screen with an optional text badge. Coordinates are normalized from 0 (top/left) to 1000 (bottom/right).",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Normalized X coordinate 0-1000 (0=left, 500=center, 1000=right)"},
                    "y": {"type": "integer", "description": "Normalized Y coordinate 0-1000 (0=top, 500=center, 1000=bottom)"},
                    "label": {"type": "string", "description": "Short label badge to display beside arrow (e.g. 'Look at this error')"},
                    "duration_seconds": {"type": "integer", "description": "How long the pointer pulses on screen (default 5s)"}
                },
                "required": ["x", "y"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_doodle",
            "description": "Doodles a visual shape (heart, star, crown, circle_error, underline) at coordinate (x, y) on Teja's monitor for playful interaction or visual highlighting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "shape": {"type": "string", "enum": ["heart", "star", "crown", "circle_error", "underline"], "description": "Shape to doodle"},
                    "x": {"type": "integer", "description": "Normalized X coordinate 0-1000 (default 500)"},
                    "y": {"type": "integer", "description": "Normalized Y coordinate 0-1000 (default 500)"},
                    "duration_seconds": {"type": "integer", "description": "Duration in seconds (default 6s)"}
                },
                "required": ["shape"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_sticky_note",
            "description": "Places a floating translucent sticky note or thought bubble on Teja's monitor.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Message content to display on screen"},
                    "position": {"type": "string", "enum": ["top_right", "bottom_right", "top_left", "bottom_left", "center"], "description": "Screen position for the note"},
                    "duration_seconds": {"type": "integer", "description": "Duration in seconds (default 8s)"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_clear_overlay",
            "description": "Clears all active visual markers, arrows, and doodles from Teja's screen.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_capture_screen",
            "description": "Captures a fresh live screenshot of Teja's monitor right now so you can inspect what he is doing or see what he is pointing at.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "description": "Why you are inspecting his screen"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_read_clipboard",
            "description": "Reads whatever text or code snippet Teja currently has copied on his Windows clipboard.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_set_clipboard",
            "description": "Writes text or code directly to Teja's Windows clipboard so he can immediately paste it with Ctrl+V.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The exact text or code to copy to his clipboard"}
                },
                "required": ["text"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_workspace_status",
            "description": "Checks Teja's active git repository status, active branch, uncommitted modified files, latest commit, and PC CPU/RAM utilization to quickly see where work was left off.",
            "parameters": {
                "type": "object",
                "properties": {
                    "workspace_dir": {"type": "string", "description": "Optional workspace directory path (default: current workspace)"}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mark_task_done",
            "description": "Marks a scheduled task or reminder as completed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer", "description": "The numeric ID of the task to mark as done."}
                },
                "required": ["task_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": "Creates a new task or scheduled reminder.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "The task description or what needs to be reminded."},
                    "due_time": {"type": "string", "description": "The due time in ISO 8601 UTC format (YYYY-MM-DDTHH:MM:SSZ)."}
                },
                "required": ["description", "due_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "Lists all pending tasks and scheduled reminders.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_pc_presence",
            "description": "Checks if Teja is currently active on his PC, what window is focused, and if he is away or idle.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_git_log",
            "description": "Checks the recent git commits of your own source code (Sofia's brain updates) to see what Teja has recently changed.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    }
]
LAZY_CODE_PATTERNS = [
    re.compile(r"//\s*(?:implement|todo|add|write)\s+(?:logic|code|here|rest|later)", re.IGNORECASE),
    re.compile(r"#\s*(?:implement|todo|add|write)\s+(?:logic|code|here|rest|later)", re.IGNORECASE),
    re.compile(r"/\*\s*(?:implement|todo|add|write)\s+.*?\*/", re.IGNORECASE),
    re.compile(r"\b(?:remaining code is straightforward|you can implement the rest|fill in the rest|left as an exercise)\b", re.IGNORECASE)
]
_tool_lock = asyncio.Lock()

