"""Tool registrations for live app previews."""
from pathlib import Path

import tools
import preview_runtime


@tools.tool(
    "preview_start",
    "Build and run a website or web app in a disposable isolated Render sandbox, then return a public HTTPS preview link. Automatically detects static HTML, React/Vite, Flask, and FastAPI/Uvicorn projects. Use after creating or editing a runnable web project. You may supply a custom start command, port, or install command when auto-detection is insufficient. Preview links are public to anyone who has them and expire when the sandbox stops.",
    {
        "path?": ("string", "Project directory relative to the current chat workspace; default ."),
        "command?": ("string", "Optional explicit server start command; must bind to 0.0.0.0 and the selected port."),
        "port?": ("integer", "TCP port the web app listens on; default 5173. For custom commands, choose the port used in the command."),
        "install_command?": ("string", "Optional explicit dependency-install command, such as npm install or python -m pip install -r requirements.txt.")
    },
)
def preview_start(path=".", command="", port=5173, install_command=""):
    root = Path(tools.ROOT.get()).resolve()
    return preview_runtime.start_preview(root.name, root, path, command, port, install_command)


@tools.tool(
    "preview_stop",
    "Stop the current chat's live website preview and release its isolated sandbox.",
    {},
)
def preview_stop():
    root = Path(tools.ROOT.get()).resolve()
    return preview_runtime.stop_preview(root.name)
