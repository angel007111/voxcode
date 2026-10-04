"""Имя помощника и слово-активатор — из assistant.json (его пишет setup.ps1).

  {"name": "Джарвис", "wake": ["джарвис\\w*", "jarvis\\w*"]}

wake — регулярные выражения (без ^ и \\b) для вариантов, как Whisper может услышать имя.
Нет файла (setup.ps1 не запускали) — имя VoxCode, откликается на «Вокс».
"""
import json
from pathlib import Path

HERE = Path(__file__).parent
FILE = HERE / "assistant.json"
DEFAULT = {"name": "VoxCode", "wake": [r"вокс\w*", r"vox\w*"]}


def load():
    try:
        data = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    name = (data.get("name") or DEFAULT["name"]).strip()
    wake = data.get("wake") or DEFAULT["wake"]
    return {"name": name, "wake": wake}


ASSISTANT = load()
NAME = ASSISTANT["name"]
WAKE_CORE = "|".join(f"(?:{w})" for w in ASSISTANT["wake"])  # для listener.WAKE / WAKE_ANY
