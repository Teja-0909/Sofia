"""
Installs Sofia Desktop Presence Sidecar into Windows Startup
Runs silently in the background (0% CPU, invisible window, starts automatically on PC boot).
"""

import os
import pathlib
import sys

STARTUP_DIR = os.path.join(
    os.environ.get("APPDATA", ""),
    r"Microsoft\Windows\Start Menu\Programs\Startup"
)

PYTHONW_PATH = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
SIDECAR_PATH = str(pathlib.Path(__file__).resolve().parent / "sidecar.py")
VBS_PATH = os.path.join(STARTUP_DIR, "SofiaSidecar.vbs")

VBS_CONTENT = f'''Set WshShell = CreateObject("WScript.Shell")
WshShell.Run """{PYTHONW_PATH}"" ""{SIDECAR_PATH}""", 0, False
'''


def install():
    if not os.path.exists(STARTUP_DIR):
        print(f"Error: Startup directory not found at {STARTUP_DIR}")
        return False

    with open(VBS_PATH, "w", encoding="utf-8") as f:
        f.write(VBS_CONTENT)

    print("Successfully installed Sofia Sidecar to Windows Startup!")
    print(f"Pythonw: {PYTHONW_PATH}")
    print(f"Sidecar: {SIDECAR_PATH}")
    print(f"Startup Script: {VBS_PATH}")
    return True


if __name__ == "__main__":
    install()
