#!/usr/bin/env python3
"""
FORGE - a task queue GUI for a local llama-server model. One file, standard library + tkinter only.

AUTHOR     Dminx
LICENSE    See LICENSE / LICENSE.md next to this file: forks and changes are allowed for
           anyone, but the original work may not be presented as your own.

RUN        python3 forge.py          (Kubuntu: sudo apt install python3-tk  if tkinter is missing)

WHAT IT DOES
  Queue tab    type tasks, drag order with the arrow buttons, press Start. EVERY task (and every repair cycle of a task)
               starts a brand-new chat: system prompt + that one task (+ a short note of what earlier cycles tried).
               A task is Done when the model called finish_task AND the after-cycle hooks and the verify command pass.
  Stopping     "Finish run, then stop" lets the current run complete (checks included) and then stops the queue.
               "Stop now" cancels at once and (setting) restores the files that run edited.
  Model tab    port, binary, model file, context, extra flags (or one custom command) + Start / Stop model.
  Tools tab    every tool and hook with an on/off box (a tool that is off costs the model zero context),
               plus command hooks (auto lint / format) you can add without writing code.
  Tool Maker   describe a tool or hook in plain words, the model writes a plugin file following a strict spec,
               Forge checks it, you read it, press Save and it is live.

EXTENDING (the whole contract, also what the Tool Maker prompt teaches the model)
  Drop any .py file into forge_plugins/ that defines   TOOLS = [ {...} ]   and/or   HOOKS = [ {...} ]   (see EXAMPLE_PLUGIN below).
  Files whose name starts with _ are ignored. Settings are one line each in the SETTINGS table: the forms are generated from it.

Files next to this script: forge_config.json (all settings + queue), forge_plugins/ (plugins).
Per-task backups of every edited file: <project>/.forge_backup/<timestamp-and-id>/  (ONE folder per task, so two
tasks never share a backup folder; Revert selected task restores them).
"""
import ast, fnmatch, http.client, json, os, queue, re, shlex, shutil, signal, socket, subprocess, sys, tempfile, threading, time, types, uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, font as tkfont
except ImportError:
    tk = None

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "forge_config.json"
PLUGIN_DIR = HERE / "forge_plugins"
EVENTS = ("before_task", "after_cycle", "after_task")
PARAM_TYPES = ("string", "integer", "number", "boolean", "array", "object")

# ═════════════════════════ prompts ═════════════════════════

DEFAULT_TASK_PROMPT = (
    "You are an expert software engineer working inside the user's project folder. You get ONE task and have no memory of earlier tasks. "
    "Use the tools to find and read the relevant files, then implement the task with minimal, precise edits. "
    "Prefer replace_in_file over write_file. Always read a file before editing it. "
    "Never invent APIs: check how things are used elsewhere with search_project when unsure. "
    "Do not touch unrelated code. The task only counts as done when the checks after your edits pass. "
    "When finished, call finish_task with success=true and a 1-3 sentence summary. "
    "If the task cannot be done safely, call finish_task with success=false and say why instead of guessing."
)

# {max_tasks} and {files} are filled in when the prompt is used.
DEFAULT_REFINE_PROMPT = (
    "You turn one rough request into an ordered list of coding tasks for a small local AI model that edits files in the user's project folder.\n\n"
    "HOW THE TASKS ARE RUN\n"
    "- Every task starts in a brand-new chat with NO memory of earlier tasks. The model sees only that task's text and can read the project files.\n"
    "- A task only counts as done when the project still builds and runs afterwards, so every task must leave the project in a working state.\n\n"
    "RULES FOR THE LIST\n"
    "1. First decide, silently, ONE technical plan: the technology (use what the project already uses; if the folder is empty and the request names none, "
    "use plain HTML + CSS + JavaScript with no build step, started by opening index.html), the exact file names, and the names of the main data structures and functions. "
    "Then repeat the parts a task needs inside that task, so no task depends on reading another task's text.\n"
    "2. Order by dependency: project skeleton and data first, then the core features one by one, then secondary features, then polish (look, feel, sound, achievements).\n"
    "3. One task = one small, testable goal that touches at most 3 files and needs no more than about 15 tool calls. Split anything bigger. Never make a task that only says 'test' or 'review'.\n"
    "4. Write at most {max_tasks} tasks, fewer when the request is small. If the request is bigger than that, put the most important parts first and merge the least important ones.\n"
    "5. Keep EVERY requirement the user stated (numbers, names, examples, mood). Do not add features they did not ask for. Fix spelling mistakes silently. "
    "When something is ambiguous, pick the most sensible reading and write that reading into the task. Never ask questions.\n"
    "6. Task text: plain text, 3-8 sentences: the goal first, then the exact files, names and behaviour, then what 'done' looks like. No markdown.\n\n"
    "OUTPUT\n"
    "Reply with ONLY a JSON array of strings, one string per task, in running order. No text before or after it, no code fence.\n\n"
    "CURRENT PROJECT FILES\n{files}\n"
)

DEFAULT_DIAGNOSE_PROMPT = (
    "You are the troubleshooter of Forge, a task runner that drives a small local AI model through file tools (read_file, replace_in_file, write_file, "
    "list_files, search_project, finish_task, plus plugins). Every task runs in a fresh chat; after each cycle a verify command and hooks check the result.\n"
    "You get ONE failure report: what happened, the error text, the task, the relevant Forge settings and the last log lines.\n"
    "Reply in plain text, under 250 words, in exactly this shape:\n"
    "CAUSE: one or two sentences naming the real reason.\n"
    "FIX: concrete steps. When a Forge setting helps, name it and a value (settings: Context size, Tool turns per cycle, Repair cycles per task, Max read chars, "
    "Max tool output chars, Context budget, Thinking, Verify command). When the task is the problem, say how to reword or split it.\n"
    "PREVENT: one line, optional.\n"
    "Hints: 'request exceeds the context window' means the chat grew past the server's -c value: raise Context size, lower Max read chars / Max tool output chars, "
    "or split the task smaller. 'Model made no edits' usually means the task was vague or the files were not found. Repeated failing checks mean the verify command "
    "output points at something the model cannot see: say what to look at. No apologies, no markdown tables."
)

ANALYZE_PROMPT = (
    "You study error records written by Forge, a task runner that drives a small local AI model with file tools. "
    "The records show where the MODEL goes wrong (bad tool arguments, edits that do not match, repeated calls, giving up, failing checks). "
    "You get counts and the newest records. Reply in plain text, under 300 words: "
    "1) WEAKNESSES: the 3 to 5 most important patterns, each with the evidence (counts, tool names). "
    "2) PROMPT CHANGES: concrete sentences to add to the task system prompt so the model avoids them. "
    "3) TOOL CHANGES: a tool or a setting that would remove a pattern. Stay with what the data shows; say so when there is too little data."
)

# {existing} and {example} are filled in when the prompt is used.
DEFAULT_TOOL_SPEC = '''You write plugin files for Forge, a local task runner. A plugin is ONE Python file that defines TOOLS (functions the AI model can call while it works on a task) and/or HOOKS (functions Forge runs at fixed moments, for example auto lint).

OUTPUT RULES
- Reply with exactly one ```python code block that contains the complete file. No text before or after it.
- Python 3 standard library only. Imports at the top. No top-level code except imports, helper functions, constants, TOOLS and HOOKS.
- run() must never raise: catch errors and return a string that starts with "ERROR: ".

FILE LAYOUT
TOOLS = [ {tool}, ... ]      HOOKS = [ {hook}, ... ]      (define only what you need)

A tool is a dict with exactly these keys:
  "name"         snake_case: lowercase letters, digits, underscore. Unique. Must not be one of: {existing}
  "description"  1-2 short sentences saying WHEN to use it. The model reads it on every request, keep it under 200 characters.
  "parameters"   dict: parameter name -> {"type": "string"|"integer"|"number"|"boolean"|"array"|"object", "description": "...", "required": True or False}
                 An "array" parameter SHOULD also carry "items": {"type": <one of the same types>, "description": "..."} - the schema of ONE
                 item. Without it a strict chat template cannot type the list and the model sends empty or unusable values. Optional on other types.
  "write"        True if it modifies files, else False
  "run"          the name of a top-level function  def run_x(ctx, args) -> str
                 args is the dict the model sent. Keys can be missing or have the wrong type: check and convert them.
                 Return a short result (under 4000 characters).

A hook is a dict with exactly these keys:
  "name", "description" (what it does), "run", and
  "event"        "before_task" | "after_cycle" | "after_task"
  run is  def run_x(ctx, payload) -> str or None
  payload: before_task {"task": text} | after_cycle {"files": [project-relative paths the model edited]} | after_task {"state": "Done" or "Failed", "summary": text}
  Return None when all is fine, a short text to just log it, or text starting with "ERROR" to report a problem
  (for after_cycle a problem is sent back to the model so it can repair it).
  Formatters and auto lint belong on after_cycle, never on a moment while the model is still editing.

THE ctx OBJECT (use only these)
  ctx.root                              pathlib.Path of the project folder
  ctx.resolve(rel)                      Path inside the allowed folders, raises ValueError otherwise
  ctx.read_text(rel)                    file text, raises ValueError or OSError on problems
  ctx.write_text(rel, text)             writes a file WITH backup and change tracking. Always use this to modify files, never open(..., "w")
  ctx.files(pattern="*", folder=".")    sorted project-relative paths, hidden folders and noise already filtered out
  ctx.log(text)                         shows a line in the Forge log
  ctx.settings                          dict of the Forge settings (read only)

SAFETY RULES
- No network access. Run external programs only when that is the whole purpose of the tool: pass a list (never shell=True with text that came from the model) and set a timeout.
- Keep results short, clip long text. Never touch files outside ctx.root.

EXAMPLE OF A CORRECT FILE
{example}
'''

EXAMPLE_PLUGIN = r'''"""Example plugin: one tool and one hook. Names starting with _ are ignored; copy to a new name to use it."""
import re


def run_count_lines(ctx, args):
    path = str(args.get("path", "")).strip()
    if not path:
        return "ERROR: path is required"
    try:
        text = ctx.read_text(path)
    except (ValueError, OSError) as e:
        return "ERROR: " + str(e)
    return path + ": " + str(len(text.splitlines())) + " lines"


def run_strip_whitespace(ctx, payload):
    fixed = []
    for rel in payload.get("files", []):
        if not rel.endswith((".py", ".cs", ".js", ".ts", ".txt", ".md")):
            continue
        try:
            text = ctx.read_text(rel)
        except (ValueError, OSError):
            continue
        clean = re.sub(r"[ \t]+(?=\r?\n)", "", text)
        if clean != text:
            ctx.write_text(rel, clean)
            fixed.append(rel)
    return ("stripped trailing whitespace in: " + ", ".join(fixed)) if fixed else None


TOOLS = [
    {
        "name": "count_lines",
        "description": "Count the lines of one text file. Use it before reading a big file to decide whether to read it in parts.",
        "parameters": {"path": {"type": "string", "description": "Project-relative file path", "required": True}},
        "write": False,
        "run": run_count_lines,
    }
]

HOOKS = [
    {
        "name": "strip_trailing_whitespace",
        "description": "After each fix cycle, remove trailing spaces and tabs from the edited files.",
        "event": "after_cycle",
        "run": run_strip_whitespace,
    }
]
'''

# ═════════════════════════ settings: ONE line each, the forms are generated from this table ═════════════════════════
# ("#", section)  or  (key, label, kind, default, help).   kind: str int float bool dir file text choice:a|b|c

DEFAULT_HIDDEN_EXT = (".meta,.png,.jpg,.jpeg,.gif,.bmp,.tga,.tif,.psd,.exr,.hdr,.fbx,.blend,.wav,.mp3,.ogg,.mp4,.ttf,.otf,.dll,.so,.pyc,"
                      ".asset,.mat,.prefab,.unity,.anim,.controller,.zip,.gguf,.safetensors")

SETTINGS = [
    ("#", "Project"),
    ("project_root", "Project folder", "dir", str(HERE), "The folder the model works in."),
    ("allowed_folders", "Allowed folders", "str", ".", "Comma separated folders (relative to the project folder) that tools may read and write. '.' = the whole project. Unity example: Assets"),
    ("hidden_dirs", "Hidden folders", "str", ".git,node_modules,Library,Temp,obj,bin,__pycache__,.forge_backup", "Folder names (any depth) that listings and searches skip. Folders ending in ~ are always skipped."),
    ("hidden_exts", "Hidden extensions", "str", DEFAULT_HIDDEN_EXT, "Extensions that listings and searches never show (unless the pattern names the extension)."),
    ("#", "Server"),
    ("host", "Host", "str", "127.0.0.1", "Where the model server listens / is reached."),
    ("port", "Port", "int", 9931, "Used for the launch command AND for the requests."),
    ("server_url", "Server URL override", "str", "", "Empty = http://host:port/v1/chat/completions. Set it for PolyStrata or a remote server."),
    ("model_name", "Model id", "str", "", "Leave empty unless the server is in router mode and needs a model id."),
    ("api_key", "API key", "str", "", "Sent as a Bearer token when set."),
    ("request_timeout", "Request timeout (s)", "int", 1800, "Thinking can take minutes at 30 tok/s, keep this high."),
    ("#", "Model launch"),
    ("custom_launch", "Custom launch command", "str", "", "If set, this exact shell command starts the model and the fields below are ignored. {port} and {host} are replaced. Use it for PolyStrata or a start script."),
    ("llama_binary", "llama-server binary", "file", "llama-server", "Path or command name."),
    ("model_path", "Model file (.gguf)", "file", "", "For split models point at the 00001-of-0000N file."),
    ("ctx_size", "Context size", "int", 32768, "-c value. 0 = do not pass it."),
    ("gpu_layers", "GPU layers", "int", 99, "-ngl value. -1 = do not pass it."),
    ("extra_args", "Extra arguments", "str", "--jinja", "Appended as written, e.g. --jinja --flash-attn on --n-cpu-moe 30. Tool calling needs --jinja."),
    ("stop_model_on_exit", "Stop model when this window closes", "bool", True, "Only affects a model that Forge started."),
    ("#", "Sampling"),
    ("thinking", "Thinking", "choice:on|off|server_default", "on", "on/off sends enable_thinking to the chat template, server_default sends nothing."),
    ("use_server_sampling", "Use the server's own sampling", "bool", True, "ON: temperature, top_p, top_k and max_tokens are not sent, the server's startup flags apply."),
    ("temperature", "Temperature", "float", 0.6, "Only used when the box above is off."),
    ("top_p", "Top P", "float", 0.95, "Only used when the box above is off."),
    ("top_k", "Top K", "int", 20, "Only used when the box above is off."),
    ("max_tokens", "Max tokens", "int", 6000, "Only used when the box above is off. Must fit the server context."),
    ("#", "Run limits"),
    ("max_cycles", "Repair cycles per task", "int", 4, "Edit -> checks -> repair rounds per task before it is marked Failed. Every cycle is a fresh chat."),
    ("max_turns", "Tool turns per cycle", "int", 20, "Model replies per cycle before giving up."),
    ("refine_max_tasks", "Refine: max tasks", "int", 12, "The Refine button never adds more tasks than this from one prompt (the refine prompt is also told this number)."),
    ("refine_insert", "Refine: where new tasks go", "choice:end|top", "end", "end = after the tasks already in the queue, top = in front of them (they run first)."),
    ("context_budget_chars", "Context budget (chars)", "int", 0, "0 = auto: half of the server's n_ctx read from /props. Old tool outputs are trimmed past it."),
    ("max_read_chars", "Max read chars", "int", 12000, "read_file cuts its output here."),
    ("max_search_results", "Max search hits", "int", 40, "search_project stops here."),
    ("search_context_lines", "Search context lines", "int", 0, "Default lines of context around each search hit (0 = only the hit line). A search can still ask for its own amount, up to 5."),
    ("list_limit", "Max listed files", "int", 300, "list_files shows at most this many paths."),
    ("max_tool_output_chars", "Max tool output chars", "int", 16000, "Hard cap on what any tool (built-in or plugin) may put back into the chat: the middle is cut out with a note. Stops one big result from stalling every later turn. Keep it above Max read chars, or full reads get cut too."),
    ("log_tool_output", "Log: tool results", "choice:errors_only|short|full", "errors_only", "How much of a tool's result the log shows. errors_only = only failures, short = the first 100 characters, full = everything. The model always gets the full result either way."),
    ("log_full_thinking", "Log: full thinking text", "bool", False, "OFF: the log says 'Thinking about <subjects>' and the length only. ON: the whole reasoning text is logged as well."),
    ("stop_on_failure", "Stop queue on a failed task", "bool", True, "OFF: mark it Failed and carry on with the next task."),
    ("revert_on_stop", "Revert files on Stop now", "choice:never|loops_only|always", "loops_only", "What Stop now does with the files the cancelled run edited. loops_only = restore them only when the task was looping. 'Finish run, then stop' never reverts anything."),
    ("max_loop_failures", "Loop: max failed runs in a row", "int", 3, "A looping task that fails this many times in a row stops looping and stays Failed (0 = never, even when Stop queue on a failed task is off; 1 = stop after the first failure)."),
    ("protected_paths", "Protected paths", "str", "", "Comma separated globs of files no tool, plugin or command hook may ever write (generated files, vendored code, LICENSE). Matched against the project-relative path."),
    ("auto_diagnose", "Diagnose failures automatically", "bool", True, "When a task fails or the queue stops on a server error, a brand-new chat explains the cause and a fix in Log -> Diagnosis (at most 3 per queue run)."),
    ("backup_edits", "Back up edited files", "bool", True, "Earliest copy of every edited file per task goes to <project>/.forge_backup/ (needed for Revert)."),
    ("verify_command", "Verify command", "str", "", "Runs in the project folder after each cycle, exit code 0 = good. Examples: dotnet build -nologo -v q | pytest -q | ruff check . | (empty = skip)"),
    ("verify_timeout", "Command timeout (s)", "int", 300, "Applies to the verify command and to command hooks."),
    ("#", "Git"),
    ("git_auto_commit", "Commit after each task", "choice:done|always|off", "done", "done = commit the files a task edited when it ends Done. always = also when it Failed (the partial edits become a version you can look at or undo). off = never. Only the files the task edited are committed, your other changes stay untouched."),
    ("git_auto_init", "Create the repository if the project has none", "bool", False, "ON: the first commit runs git init (and writes a .gitignore when there is none) so every task becomes a version. OFF: Forge only commits when the folder is already a repository."),
    ("#", "ComfyUI"),
    ("comfy_url", "ComfyUI address", "str", "http://127.0.0.1:8188", "Where the running ComfyUI listens (the address you open in the browser)."),
    ("comfy_wf_dir", "Workflow folder", "dir", str(HERE / "forge_workflows"), "Folder that holds your 3 exported workflow JSON files."),
    ("comfy_wf_edit", "Image Editing workflow", "str", "image_edit.json", "File name inside the workflow folder, or a full path. If it is not found, any *edit*.json in the folder is used."),
    ("comfy_wf_generate", "Image Generation workflow", "str", "generate-image.json", "File name or full path. Fallback: any *generat*.json that is not a video or edit file."),
    ("comfy_wf_video", "Video Generation workflow", "str", "video-generator.json", "File name or full path. Fallback: any *video*.json."),
    ("comfy_models_dir", "Models folder", "dir", str(Path.home() / "comfyui-models"), "Where your ComfyUI models live. Used to list model files when ComfyUI cannot be asked."),
    ("comfy_out_dir", "Save results to", "dir", str(HERE / "forge_outputs"), "Every generated picture/video is copied here (one sub-folder per page) so Forge can show it again after a restart."),
    ("comfy_free_after", "Free ComfyUI memory after each run", "bool", True, "Asks ComfyUI to unload its models when a generation ends, which keeps VRAM free for the language model."),
    ("comfy_timeout", "Max wait per generation (s)", "int", 7200, "Give up waiting for one generation after this long."),
    ("comfy_dir", "ComfyUI folder", "dir", str(Path.home() / "comfyui"), "Working folder for the launch command below (optional)."),
    ("comfy_launch", "ComfyUI launch command", "str", "", "Optional. Fill in your usual ComfyUI command (python3 main.py plus your flags) to get Start/Stop buttons on the ComfyUI tab. Empty = you start ComfyUI yourself."),
    ("#", "Appearance"),
    ("theme", "Theme", "choice:midnight|graphite|light|nord|forest|paper", "midnight", "Colour scheme. Applied immediately."),
    ("ui_font_size", "Font size", "int", 10, "Interface font size, 7 to 24. Applied immediately."),
    ("#", "Prompts"),
    ("task_prompt", "Task system prompt", "text", DEFAULT_TASK_PROMPT, "Used for every task. Each task starts a new chat with this prompt."),
    ("refine_prompt", "Refine prompt", "text", DEFAULT_REFINE_PROMPT, "What the model is told when the Refine button splits a rough request into tasks. {max_tasks} and {files} are filled in automatically."),
    ("diagnose_prompt", "Diagnosis prompt", "text", DEFAULT_DIAGNOSE_PROMPT, "What the model is told when it explains a failure (Log -> Diagnosis)."),
    ("tool_spec", "Tool Maker prompt", "text", DEFAULT_TOOL_SPEC, "What the model is told when it writes a plugin. {existing} and {example} are filled in automatically."),
]

DEFAULT_COMMAND_HOOKS = [
    {"name": "ruff format", "enabled": False, "glob": "*.py", "command": "ruff format {files}", "blocking": False},
    {"name": "black", "enabled": False, "glob": "*.py", "command": "black {files}", "blocking": False},
    {"name": "dotnet format", "enabled": False, "glob": "*.cs", "command": "dotnet format --include {files}", "blocking": False},
    {"name": "prettier", "enabled": False, "glob": "*.js,*.ts,*.json,*.css,*.html", "command": "npx prettier --write {files}", "blocking": False},
]


# key -> declared kind ("str int float bool dir file text choice:a|b|c"), the same table the forms are built from
SETTING_KIND = {item[0]: item[2] for item in SETTINGS if item[0] != "#"}


def valid_setting(k, v):
    """A4: a saved setting is accepted only when it matches its declared kind.
    A kind table, not type() matching: bools pass isinstance(x, int) but must never land in an int/float
    setting, and a bool/int saved into a bool setting is kept as a real bool."""
    kind = SETTING_KIND.get(k)
    if kind is None:
        return False  # queue / disabled_tools / disabled_hooks / command_hooks are checked below
    if kind.startswith("choice:"):
        return isinstance(v, str) and v in kind.split(":", 1)[1].split("|")
    if kind == "bool":
        return isinstance(v, bool)
    if kind == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if kind == "float":
        return isinstance(v, (float, int)) and not isinstance(v, bool)
    return isinstance(v, str)  # str / dir / file / text


def load_config():
    cfg = {item[0]: item[3] for item in SETTINGS if item[0] != "#"}
    cfg.update(queue=[], disabled_tools=[], disabled_hooks=[], command_hooks=json.loads(json.dumps(DEFAULT_COMMAND_HOOKS)), git_repos=[])
    try:
        saved = json.loads(CONFIG_PATH.read_text("utf-8"))
        saved = saved if isinstance(saved, dict) else {}
        for k, v in saved.items():
            if valid_setting(k, v):
                cfg[k] = v
        for k in ("disabled_tools", "disabled_hooks"):
            v = saved.get(k)
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                cfg[k] = v
        v = saved.get("command_hooks")
        if isinstance(v, list) and all(isinstance(h, dict) and isinstance(h.get("command"), str) for h in v):
            cfg["command_hooks"] = v
        v = saved.get("git_repos")
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            cfg["git_repos"] = v
        q = saved.get("queue")
        if isinstance(q, list):
            cfg["queue"] = [t for t in q if isinstance(t, dict) and isinstance(t.get("prompt"), str) and isinstance(t.get("state"), str)]
    except (OSError, ValueError):
        pass
    for t in cfg["queue"]:  # a crash may have left a task Running
        if t.get("state") == "Running":
            t["state"] = "Pending"
        t.setdefault("loop_left", 0)
        t.setdefault("loop_fails", 0)
    return cfg


class Cancelled(Exception):
    pass


def loop_text(t):
    """Shown in the Loop column: runs left while the task keeps looping."""
    left = t.get("loop_left", 0)
    if left == 0 and t["state"] != "Loop":
        return ""
    return "infinite" if left < 0 else str(left)


def clip(t, n):
    t = str(t)
    return t if len(t) <= n else t[:n] + "..."


def final_stats(s):
    """Raw running totals of one task -> the small dict that is stored in the task."""
    gen = s.get("gen_s", 0.0)
    return {"turns": s.get("turns", 0), "cycles": s.get("cycles", 0), "tool_calls": s.get("tool_calls", 0), "out_tok": s.get("out_tok", 0),
            "tps": round(s.get("out_tok", 0) / gen, 1) if gen > 0 else 0, "ctx_peak": s.get("ctx_peak", 0), "seconds": round(s.get("seconds", 0.0), 1)}


def stats_text(s):
    """The Cost column: what one finished task cost (turns, tokens, speed, time)."""
    if not isinstance(s, dict) or not s.get("turns"):
        return ""
    def k(n):
        return "%.1fk" % (n / 1000.0) if n >= 1000 else str(int(n))
    secs = int(s.get("seconds", 0))
    dur = "%dh%02dm" % (secs // 3600, secs % 3600 // 60) if secs >= 3600 else "%dm%02ds" % (secs // 60, secs % 60) if secs >= 60 else "%ds" % secs
    bits = ["%d turn%s" % (s["turns"], "" if s["turns"] == 1 else "s")]
    if s.get("cycles", 1) > 1:
        bits.append("%d cycles" % s["cycles"])
    bits.append("%s tok" % k(s.get("out_tok", 0)))
    if s.get("tps"):
        bits.append("%.1f tps" % s["tps"])
    if s.get("ctx_peak"):
        bits.append("ctx %s" % k(s["ctx_peak"]))
    bits.append(dur)
    return " | ".join(bits)


_FILE_RX = re.compile(r"[\w./-]+\.(?:py|js|ts|tsx|jsx|cs|json|html|css|md|txt|yml|yaml|toml|cfg|ini|sh|cpp|hpp|c|h|java|kt|rs|go|lua|gd|shader|xml|unity|prefab)\b")


def think_subjects(text, tool_names=()):
    """What the model is thinking ABOUT, without the thinking itself: file names, tool names and `identifiers`
    it mentioned (first appearance order). When it named none, the first sentence."""
    found = []
    def add(x):
        x = x.strip()
        if x and x not in found:
            found.append(x)
    text = str(text or "")
    for m in _FILE_RX.findall(text)[:12]:
        add(clip(m, 40))
    for n in tool_names:
        if re.search(r"\b%s\b" % re.escape(n), text):
            add(n)
    for m in re.findall(r"`([A-Za-z_][\w.()]{2,30})`", text)[:12]:
        add(m)
    if found:
        return ", ".join(found[:8])
    first = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
    return clip(" ".join(first.split()), 80) or "(no subject)"


LOG_DIR = HERE / "forge_logs"
LOG_MAX = 2_000_000
LOG_SECTIONS = ("Queue", "Model", "ComfyUI", "Errors", "Diagnosis")  # the sections of the Log tab, in tab order
_LOG_LOCK = threading.Lock()


def file_log(section, text):
    """Appends timestamped lines to forge_logs/<section>.log. The file is cut to its newest half when it passes LOG_MAX."""
    try:
        with _LOG_LOCK:
            LOG_DIR.mkdir(exist_ok=True)
            p = LOG_DIR / (section.lower() + ".log")
            if p.exists() and p.stat().st_size > LOG_MAX:
                data = p.read_bytes()[-(LOG_MAX // 2):]
                nl = data.find(b"\n")
                p.write_bytes(data[nl + 1:] if nl >= 0 else data)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S  ")
            with open(p, "a", encoding="utf-8") as f:
                for line in str(text).rstrip("\n").split("\n"):
                    f.write(stamp + line + "\n")
    except OSError:
        pass


ERROR_FILE = HERE / "forge_logs" / "errors.jsonl"
ERROR_MAX = 4_000_000


def read_error_records(limit=1500):
    out = []
    try:
        with open(ERROR_FILE, "rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - 1_500_000))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return out
    for ln in lines[-limit:]:
        try:
            r = json.loads(ln)
            if isinstance(r, dict):
                out.append(r)
        except ValueError:
            continue
    return out


def error_summary(recs):
    """Counts by kind, by tool and by repeated message: the part of the weakness analysis that needs no model."""
    from collections import Counter
    kinds = Counter(str(r.get("kind", "?")) for r in recs)
    tools = Counter("%s / %s" % (r["tool"], r.get("kind", "?")) for r in recs if r.get("tool"))
    texts = Counter(clip(re.sub(r"\d+", "N", str(r.get("detail", ""))).replace("\n", " "), 90) for r in recs)
    lines = ["%d record(s), %s to %s" % (len(recs), recs[0].get("time", "?"), recs[-1].get("time", "?")), "", "By kind:"]
    lines += ["  %-16s %d" % kv for kv in kinds.most_common()]
    if tools:
        lines += ["", "By tool:"] + ["  %-40s %d" % kv for kv in tools.most_common(8)]
    rep_txt = [(t, n) for t, n in texts.most_common(6) if n > 1]
    if rep_txt:
        lines += ["", "Most repeated messages:"] + ["  %dx  %s" % (n, t) for t, n in rep_txt]
    return "\n".join(lines)


def msg_chars(m):
    """Size of one chat message in characters, INCLUDING the arguments of its tool calls (a big write_file can be most of a turn)."""
    n = len(m.get("content") or "")
    for tc in m.get("tool_calls") or []:
        a = (tc.get("function") or {}).get("arguments")
        n += len(a) if isinstance(a, str) else len(json.dumps(a or {}))
    return n


def trim_old_outputs(msgs, budget):
    """Over budget: old tool results go first, then the arguments of old tool calls. The newest message is never touched."""
    total = sum(msg_chars(m) for m in msgs)
    for i in range(2, len(msgs) - 1):
        if total <= budget:
            return
        m = msgs[i]
        if m["role"] == "tool" and len(m["content"]) > 150:
            total -= len(m["content"]) - 24
            m["content"] = "[old output trimmed]"
    for i in range(2, len(msgs) - 1):
        if total <= budget:
            return
        for tc in msgs[i].get("tool_calls") or []:
            fn = tc.get("function") or {}
            a = fn.get("arguments")
            if isinstance(a, str) and len(a) > 300:
                total -= len(a) - 40
                fn["arguments"] = '{"note": "old arguments trimmed"}'


def cap_out(t, n):
    """P8: hard cap on what any tool may put back into the chat. The middle is cut out and
    marked, so the head (usually the answer) and the tail (the last lines of a log) both survive,
    and one oversized result can never stall every later turn."""
    t = str(t)
    if len(t) <= n:
        return t
    n = max(200, n)
    keep = (n - 60) // 2
    return t[:keep] + "\n...[%d characters cut out to fit the tool output limit]\n" % (len(t) - 2 * keep) + t[-keep:]


def arg_int(args, key, default):
    try:
        return int(float(args.get(key, default)))
    except (TypeError, ValueError):
        return default


def yes(args, key):
    """A boolean model argument: some models send "false"/"0"/"no" as text instead of a bool."""
    v = args.get(key)
    if v is None:
        return False
    return str(v).strip().lower() not in ("", "false", "0", "no", "off")


def symlink_component(base, target):
    """First component on the way from base (exclusive) down to target (inclusive) that
    is a symlink, or None. Must be run on the path the caller ASKED FOR: Path.resolve()
    happily follows links, so the resolved path it returns never shows them."""
    chain, p = [], Path(target)
    while p != base and p != p.parent:
        chain.append(p)
        p = p.parent
    for c in reversed(chain):
        if os.path.islink(str(c)):
            return c
    return None


def same_content(a, b):
    """Byte-for-byte compare of two files (used to verify staged copies)."""
    try:
        if Path(a).stat().st_size != Path(b).stat().st_size:
            return False
        with open(str(a), "rb") as fa, open(str(b), "rb") as fb:
            while True:
                x, y = fa.read(65536), fb.read(65536)
                if x != y:
                    return False
                if not x:
                    return True
    except OSError:
        return False


# ═════════════════════════ ctx: what every tool and hook gets ═════════════════════════

class Ctx:
    def __init__(self, engine, backup_dir):
        self.engine = engine
        self.settings = engine.cfg
        self.root = Path(engine.cfg["project_root"]).resolve()
        self.backup_dir = backup_dir
        self.changed = []        # project-relative paths written in this cycle
        self.finish = None       # (success, summary) once the model calls finish_task

    def log(self, msg):
        self.engine.log(str(msg))

    def rel(self, full):
        try:
            return str(Path(full).resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return str(full)

    def resolve(self, rel):
        if not rel:
            raise ValueError("path is required")
        raw = str(rel).replace("\\", "/")
        full = (self.root / raw).resolve()
        for f in str(self.settings["allowed_folders"]).split(","):
            f = f.strip()
            if not f:
                continue
            a = self.root / f
            allowed = a.resolve()
            if not (full == allowed or allowed in full.parents):
                continue
            # A2: the containment above only sees the path resolve() hands back, and resolve()
            # follows links, so the links themselves never appear in `full` and a route can leave
            # the allowed folder (or the project) and come back to make it look clean. Two things
            # that hole costs:
            #  * allowed=src and src/out a link to /tmp: "src/out/notes.txt" passes the check above,
            #    write_text clobbers /tmp/notes.txt, _backup copies that file to .forge_backup/src/out
            #    and Revert restores THOSE BYTES onto src/out itself - the wrong place.
            #  * allowed=src and src itself a link to /tmp/x: rel() cannot make the resolved path
            #    project-relative, so it returns an ABSOLUTE path, and Path(backup_dir) / "/tmp/x/..."
            #    throws the backup folder away (a leading "/" resets it) - dst ends up being the very
            #    file being edited, so no backup is taken at all.
            # Both come down to the same thing: after resolve(), `full` is the only place the tools
            # can still act on, and it must be inside the project. That single check closes both, and
            # it is what keeps rel()/_backup/Revert agreeing about where a file lives.
            # On top of it, a link on the route the CALLER asked for is refused unless the user set it
            # up (the allowed folder itself, or a folder on the way to it): a symlink is not a plain
            # file/folder, so writing through one is rejected with a message the model can act on, and
            # it names the real target instead. A route with no link at all costs nothing extra, so a
            # project without symlinks behaves exactly as it did before.
            if not (full == self.root or self.root in full.parents):
                raise ValueError("path resolves outside the project folder: " + raw)
            # normpath collapses the "." / ".." components the caller may have used (Path does not)
            # and keeps links where they are, which is exactly what the walk below needs.
            link = symlink_component(self.root, Path(os.path.normpath(str(self.root / raw))))
            if link is not None and not (link == a or link in a.parents):
                raise ValueError("path goes through a symlink and is not a plain file/folder inside "
                                 "the allowed folders: " + raw)
            return full
        raise ValueError("path must be inside: " + str(self.settings["allowed_folders"]))

    def read_text(self, rel):
        full = self.resolve(rel)
        if not full.is_file():
            raise ValueError("file not found: " + str(rel))
        with open(full, encoding="utf-8", errors="replace", newline="") as f:
            return f.read()

    def _backup(self, full):
        if not self.settings["backup_edits"] or not self.backup_dir:
            return
        dst = Path(self.backup_dir) / self.rel(full)
        marker = Path(str(dst) + ".new")
        if dst.exists() or marker.exists():
            return  # keep the earliest copy of this task
        dst.parent.mkdir(parents=True, exist_ok=True)
        if full.exists():
            shutil.copy2(full, dst)
        else:
            marker.write_text("")

    def protected(self, r):
        """T11: protected_paths is a comma separated list of globs nothing may ever write
        (generated files, vendored code, LICENSE). Checked here because write_text is the ONE
        place every file change goes through: built-in tools, plugin tools and plugin hooks all
        respect it, so no tool has to remember the rule itself."""
        for g in str(self.settings.get("protected_paths", "")).split(","):
            g = g.strip()
            if g and (fnmatch.fnmatch(r, g) or fnmatch.fnmatch(Path(r).name, g)
                      or r.startswith(g.rstrip("/") + "/")):
                return g
        return None

    def write_text(self, rel, text):
        full = self.resolve(rel)
        r = self.rel(full)
        if r.endswith(".meta") or any(p in (".git", ".forge_backup") or p.endswith("~") for p in r.split("/")):
            raise ValueError("this file or folder is read-only for tools: " + r)
        g = self.protected(r)
        if g is not None:
            raise ValueError("this file is protected and cannot be written: " + r + " (matches " + g + ")")
        self._backup(full)
        full.parent.mkdir(parents=True, exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if r not in self.changed:
            self.changed.append(r)

    def files(self, pattern="*", folder="."):
        base = self.resolve(folder)
        if not base.is_dir():
            raise ValueError("folder not found: " + str(folder))
        hd = {x.strip().lower() for x in str(self.settings["hidden_dirs"]).split(",") if x.strip()}
        hx = [x.strip().lower() for x in str(self.settings["hidden_exts"]).split(",") if x.strip()]
        out = []
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d.lower() not in hd and not d.endswith("~")]  # only folders INSIDE the searched one count
            for fn in fns:
                rel = self.rel(Path(dp) / fn)
                if not (fnmatch.fnmatch(fn, pattern) or fnmatch.fnmatch(rel, pattern)):
                    continue
                low = fn.lower()
                if any(low.endswith(e) and e not in pattern.lower() for e in hx):
                    continue
                out.append(rel)
        return sorted(out)


# ═════════════════════════ built-in tools (same contract as plugins) ═════════════════════════

def t_read_file(ctx, args):
    try:
        full = ctx.resolve(args.get("path"))
    except ValueError as e:
        return "ERROR: " + str(e)
    if not full.is_file():
        return "ERROR: file not found"
    try:  # read the bytes ONCE: the NUL sniff and the decode share the same buffer (A8)
        raw = full.read_bytes()
    except OSError as e:
        return "ERROR: " + str(e)
    if b"\0" in raw[:1024]:
        return "ERROR: binary file, not readable as text"
    lines = raw.decode("utf-8", errors="replace").splitlines()
    start = max(1, arg_int(args, "start_line", 1))
    end = min(len(lines), arg_int(args, "end_line", len(lines)))
    out, size = [], 0
    for i in range(start, end + 1):
        line = str(i) + ": " + lines[i - 1] + "\n"
        if size + len(line) > ctx.settings["max_read_chars"]:
            out.append("...[truncated at line %d of %d, call again with start_line=%d]" % (i - 1, len(lines), i))
            break
        out.append(line)
        size += len(line)
    return "".join(out)


def t_replace_in_file(ctx, args):
    path, old, new = args.get("path"), str(args.get("old_str", "")), str(args.get("new_str", ""))
    try:
        text = ctx.read_text(path)
    except (ValueError, OSError) as e:
        return "ERROR: " + str(e) + " (use write_file for new files)"
    if not old:
        return "ERROR: old_str is empty"
    if "\r\n" in text and "\r\n" not in old:
        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
    n = text.count(old)
    if n == 0:
        return "ERROR: old_str not found. Re-read the file and copy the text exactly (whitespace matters, no line-number prefixes)."
    if n > 1:
        return "ERROR: old_str matches more than once, include more surrounding lines."
    try:
        ctx.write_text(path, text.replace(old, new, 1))
    except (ValueError, OSError) as e:
        return "ERROR: " + str(e)
    return "OK"


def t_write_file(ctx, args):
    try:
        ctx.write_text(args.get("path"), str(args.get("content", "")))
    except (ValueError, OSError) as e:
        return "ERROR: " + str(e)
    return "OK"


def t_list_files(ctx, args):
    """T6: depth (1 = only the folder itself, 0/empty = as deep as it goes) and details
    (size, modified time, line count) on one line each. One tool with two optional
    parameters instead of separate tree / metadata / glob tools."""
    depth = arg_int(args, "depth", 0)
    if depth < 0:
        depth = 0
    try:
        base = ctx.resolve(str(args.get("folder") or "."))
        files = ctx.files(str(args.get("pattern") or "*"), str(args.get("folder") or "."))
    except ValueError as e:
        return "ERROR: " + str(e)
    if not files:
        return "no files match (hidden folders and noise extensions are filtered out; pass a pattern that names an extension to see it)"
    if depth:
        # depth counts levels BELOW the searched folder, not below the project root
        base_n = 0 if base == ctx.root else len(ctx.rel(base).split("/"))
        files = [f for f in files if len(f.split("/")) - base_n <= depth]
    if not files:
        return "no files that shallow (use depth=0 for the whole folder)"
    if not yes(args, "details"):
        keep = max(10, ctx.settings["list_limit"])
        body = "\n".join(files[:keep])
        if len(files) > keep:
            body += "\n...[%d more not shown: narrow the folder or the pattern]" % (len(files) - keep)
        return body
    out = []
    for rel in files[:max(10, ctx.settings["list_limit"])]:
        full = ctx.root / rel
        try:
            st = full.stat()
        except OSError:
            continue
        lines = "-"
        if st.st_size <= 2_000_000:
            try:
                raw = full.read_bytes()
                if b"\0" not in raw[:1024]:
                    lines = str(raw.count(b"\n") + (1 if raw and not raw.endswith(b"\n") else 0))
            except OSError:
                pass
        out.append("%s  %s  %s  %s" % (rel, "%dkB" % round(st.st_size / 1024, 1) if st.st_size >= 1024 else str(st.st_size) + "B",
                                       datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                                       lines + (" lines" if lines != "-" else " binary")))
    body = "\n".join(out)
    if len(files) > len(out):
        body += "\n...[%d more not shown: narrow the folder or the pattern]" % (len(files) - len(out))
    return body


def t_search_project(ctx, args):
    """T5: context_lines (surrounding lines per hit), regex, case_sensitive, whole_word.
    Whole-word search covers most of what a 'find references' tool asks for, so no
    language server is needed."""
    q = str(args.get("query", ""))
    if not q:
        return "ERROR: query required"
    ctx_lines = max(0, min(5, arg_int(args, "context_lines", ctx.settings.get("search_context_lines", 0))))
    cs, rg, ww = yes(args, "case_sensitive"), yes(args, "regex"), yes(args, "whole_word")
    try:
        if rg:
            rx = re.compile(q, 0 if cs else re.IGNORECASE)
        else:
            word = r"\b%s\b" % re.escape(q) if ww else re.escape(q)
            rx = re.compile(word, 0 if cs else re.IGNORECASE)
    except re.error as e:
        return "ERROR: bad regex: %s" % e
    try:
        files = ctx.files(str(args.get("pattern") or "*"), str(args.get("folder") or "."))
    except ValueError as e:
        return "ERROR: " + str(e)
    hits, out = 0, []
    sep = "\n" if not ctx_lines else "\n\n"  # without context this stays exactly as it was before
    for rel in files:
        full = ctx.root / rel
        try:
            if full.stat().st_size > 2_000_000:
                continue
            with open(full, "rb") as f:
                if b"\0" in f.read(1024):
                    continue
            lines = full.read_text("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        hit = [i for i, line in enumerate(lines) if rx.search(line)]
        if not hit:
            continue
        start = 0
        while start < len(hit) and hits < ctx.settings["max_search_results"]:
            stop = start + 1
            # hits whose context windows touch each other share one block (like grep -C)
            while stop < len(hit) and hit[stop] - hit[stop - 1] <= 2 * ctx_lines:
                stop += 1
            grp = set(hit[start:stop])
            lo, hi = max(0, hit[start] - ctx_lines), min(len(lines), hit[stop - 1] + ctx_lines + 1)
            out.append("\n".join("%s:%d%s %s" % (rel, j + 1, ":" if j in grp else "- ", clip(lines[j].strip(), 200))
                                 for j in range(lo, hi)))
            hits += stop - start
            start = stop
        if hits >= ctx.settings["max_search_results"]:
            return sep.join(out) + "\n...[result limit reached]"
        if len(out) >= 400:
            return sep.join(out) + "\n...[context too large: lower context_lines or narrow the folder]"
    return sep.join(out) if out else "no matches"


def t_finish_task(ctx, args):
    ctx.finish = (str(args.get("success", "true")).lower() != "false", str(args.get("summary", "")))
    return "OK"


def _p(kind, desc, required=False, items=None):
    """Parameter spec. K3: an array parameter may carry items={'type': ..., 'description': ...}."""
    d = {"type": kind, "description": desc, "required": required}
    if items is not None:
        d["items"] = items
    return d


def param_schema(p):
    """K3: one parameter spec -> its JSON schema. The optional "items" spec of an array
    parameter is emitted for real. Strict chat templates need the item type: without it a
    list of strings comes back empty or full of nothing usable, and every tool that takes a
    list (read_files, multi-edit, add_tasks) is pointless until this is carried through."""
    s = {"type": p["type"], "description": p.get("description", "")}
    it = p.get("items")
    if isinstance(it, dict):
        s["items"] = {"type": it.get("type", "string"), "description": it.get("description", "")}
    return s


BUILTIN_TOOLS = [
    {"name": "read_file", "source": "built-in", "write": False, "run": t_read_file,
     "description": "Read a text file. Lines are prefixed 'N: ' for reference only, never include that prefix in edits.",
     "parameters": {"path": _p("string", "Project-relative path", True), "start_line": _p("integer", "First line (1-based)"), "end_line": _p("integer", "Last line")}},
    {"name": "replace_in_file", "source": "built-in", "write": True, "run": t_replace_in_file,
     "description": "Replace exactly one occurrence of old_str with new_str. old_str must match the file text exactly and be unique.",
     "parameters": {"path": _p("string", "Project-relative path", True), "old_str": _p("string", "Exact existing text", True), "new_str": _p("string", "Replacement text", True)}},
    {"name": "write_file", "source": "built-in", "write": True, "run": t_write_file,
     "description": "Create a new file or fully overwrite one. Use only for new files or complete rewrites.",
     "parameters": {"path": _p("string", "Project-relative path", True), "content": _p("string", "Full file content", True)}},
    {"name": "list_files", "source": "built-in", "write": False, "run": t_list_files,
     "description": "List files under a folder. Hidden folders and noise files are skipped. Long lists are cut with a count: narrow the folder or pattern.",
     "parameters": {"folder": _p("string", "Folder, default project root"), "pattern": _p("string", "Glob, default *"),
                    "depth": _p("integer", "How deep to look: 1 = only this folder, 2 = it plus one level of subfolders. 0 or empty = as deep as it goes"),
                    "details": _p("boolean", "true adds the size, modified time and line count of each file")}},
    {"name": "search_project", "source": "built-in", "write": False, "run": t_search_project,
     "description": "Case-insensitive text search across files. Returns path:line: text. Optional context_lines, whole_word, regex, case_sensitive.",
     "parameters": {"query": _p("string", "Text to find (a pattern when regex=true)", True), "folder": _p("string", "Folder, default project root"), "pattern": _p("string", "Glob, default *"),
                    "context_lines": _p("integer", "Lines of context around each hit (0-5, default from settings)"),
                    "whole_word": _p("boolean", "match whole words only - covers most find-references needs"),
                    "regex": _p("boolean", "query is a regular expression instead of plain text"),
                    "case_sensitive": _p("boolean", "match the exact case")}},
    {"name": "finish_task", "source": "built-in", "write": False, "run": t_finish_task,
     "description": "Call when the task is complete (success=true) or cannot be done (success=false). This ends the task.",
     "parameters": {"success": _p("boolean", "true when done, false when it cannot be done", True), "summary": _p("string", "1-3 sentences: what changed, or why not", True)}},
]


# ═════════════════════════ git: one wrapper for the engine, the tools and the Git tab ═════════════════════════

_REV_RX = re.compile(r"^[A-Za-z0-9_./@{}~^:-]{1,100}$")
_BRANCH_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")


def safe_rev(v):
    """A rev (hash, branch, HEAD~2 ...) that cannot be mistaken for an option, or None."""
    v = str(v or "").strip()
    return v if v and not v.startswith("-") and _REV_RX.match(v) else None


class Git:
    """Thin wrapper around the git command line. Arguments are always a list, never shell=True. Every method returns instead of raising."""

    def __init__(self, path):
        self.path = Path(path).expanduser()

    @staticmethod
    def available():
        return shutil.which("git") is not None

    def run(self, *args, timeout=30, env=None, both=False, strip=True):
        """(exit code, text). The text is stdout, plus stderr when the command failed (or both=True)."""
        e = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        if env:
            e.update(env)
        try:
            p = subprocess.run(["git", "-c", "core.quotepath=false", "-C", str(self.path)] + list(args), capture_output=True, timeout=timeout,
                               env=e, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return 127, "git is not installed"
        except subprocess.TimeoutExpired:
            return 124, "git timed out after %d s" % timeout
        except OSError as ex:
            return 127, str(ex)
        out = p.stdout.decode("utf-8", "replace")
        if p.returncode != 0 or both:
            out += ("\n" if out and p.stderr else "") + p.stderr.decode("utf-8", "replace")
        return p.returncode, (out.rstrip("\n") if strip else out)

    # ---- repository
    def top(self):
        rc, out = self.run("rev-parse", "--show-toplevel")
        return Path(out.strip()) if rc == 0 and out.strip() else None

    def init(self, ignore_text=None):
        rc, out = self.run("init", both=True)
        gi = self.path / ".gitignore"
        if rc == 0 and ignore_text and not gi.exists():
            try:
                gi.write_text(ignore_text, "utf-8")
            except OSError:
                pass
        return rc, out

    def ensure_exclude(self):
        """Keeps Forge's backup folder out of git status without touching the project's own .gitignore."""
        rc, out = self.run("rev-parse", "--git-path", "info/exclude")
        if rc != 0 or not out.strip():
            return
        p = Path(out.strip())
        p = p if p.is_absolute() else self.path / p
        try:
            old = p.read_text("utf-8") if p.exists() else ""
            if ".forge_backup/" not in old.splitlines():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(old + ("" if not old or old.endswith("\n") else "\n") + ".forge_backup/\n", "utf-8")
        except OSError:
            pass

    def branch(self):
        rc, out = self.run("symbolic-ref", "--short", "-q", "HEAD")
        if rc == 0 and out.strip():
            return out.strip()
        rc, out = self.run("rev-parse", "--short", "HEAD")
        return "(detached %s)" % out.strip() if rc == 0 else "(no commits yet)"

    def ahead_behind(self):
        rc, out = self.run("rev-list", "--left-right", "--count", "@{u}...HEAD")
        try:
            behind, ahead = out.split()
            return int(ahead), int(behind)
        except ValueError:
            return None

    def remotes(self):
        rc, out = self.run("remote", "-v")
        return out if rc == 0 else ""

    def branches(self):
        """[(name, is_current, upstream)]"""
        rc, out = self.run("branch", "--format=%(refname:short)%09%(HEAD)%09%(upstream:short)")
        rows = []
        for ln in out.splitlines() if rc == 0 else []:
            p = ln.split("\t")
            if len(p) >= 2 and p[0]:
                rows.append((p[0], p[1].strip() == "*", p[2] if len(p) > 2 else ""))
        return rows

    # ---- changes
    def status(self, limit=3000):
        """[{'x': index state, 'y': worktree state, 'path': repo-relative path}] (?? = untracked)"""
        rc, out = self.run("status", "--porcelain=v1", "-z", "--untracked-files=all")
        res, parts, i = [], out.split("\0") if rc == 0 else [], 0
        while i < len(parts) and len(res) < limit:
            e = parts[i]
            i += 1
            if len(e) < 4:
                continue
            if e[0] in "RC":
                i += 1  # the old name follows
            res.append({"x": e[0], "y": e[1], "path": e[3:]})
        return res

    def diff(self, path=None, staged=False, rev=None, stat=False):
        a = ["diff", "--no-color", "--no-ext-diff"] + (["--stat"] if stat else []) + (["--cached"] if staged else []) + ([rev] if rev else [])
        return self.run(*(a + (["--", path] if path else [])))

    def diff_untracked(self, path):
        return self.run("diff", "--no-color", "--no-index", "--", os.devnull, path)[1]

    def log(self, path=None, n=40):
        """[(full hash, short hash, date, author, subject)], newest first"""
        a = ["log", "-n", str(n), "--date=format:%Y-%m-%d %H:%M", "--format=%H%x1f%h%x1f%ad%x1f%an%x1f%s"]
        rc, out = self.run(*(a + (["--follow", "--", path] if path else [])))
        return [tuple(ln.split("\x1f")) for ln in out.splitlines() if rc == 0 and ln.count("\x1f") == 4]

    def show_commit(self, rev):
        return self.run("show", "--no-color", "--format=fuller", "--stat", "--patch", rev)

    def show_file(self, rev, path):
        """(rc, file text exactly as it was at rev)"""
        return self.run("show", "%s:%s" % (rev, path), strip=False)

    # ---- changing things
    def ident_env(self):
        env = {}
        if not self.run("config", "user.name")[1].strip():
            env.update(GIT_AUTHOR_NAME="Dminx", GIT_COMMITTER_NAME="Dminx")
        if not self.run("config", "user.email")[1].strip():
            env.update(GIT_AUTHOR_EMAIL="dminx@users.noreply.github.com", GIT_COMMITTER_EMAIL="dminx@users.noreply.github.com")
        return env

    def commit(self, subject, body="", paths=None, all_tracked=False):
        """(rc, output). With paths: commits exactly those files (new and changed ones are added first, ignored ones skipped) and
        leaves everything else alone. Without: the staged changes (all_tracked=True: git commit -a)."""
        args = ["commit", "-m", subject] + (["-m", body] if body else [])
        if paths:
            use = [p for p in paths if self.run("check-ignore", "-q", "--", p)[0] != 0]
            if not use:
                return 1, "all of those files are ignored by git"
            rc, out = self.run("add", "-A", "--", *use)
            if rc != 0:
                return rc, out
            if self.run("diff", "--cached", "--quiet", "--", *use)[0] == 0:
                return 1, "nothing to commit: no change in those files"
            args += ["--"] + use
        elif all_tracked:
            args.insert(1, "-a")
        return self.run(*args, env=self.ident_env(), both=True)

    def stage(self, paths):
        return self.run("add", "-A", "--", *paths)

    def unstage(self, paths):
        return self.run("reset", "-q", "--", *paths)

    def discard(self, path, untracked=False):
        return self.run("clean", "-f", "--", path) if untracked else self.run("checkout", "HEAD", "--", path)

    def checkout(self, name):
        return self.run("checkout", name, both=True)

    def new_branch(self, name):
        return self.run("checkout", "-b", name, both=True)

    def stash(self, message=""):
        return self.run(*(["stash", "push", "--include-untracked"] + (["-m", message] if message else [])), both=True)

    def stash_pop(self):
        return self.run("stash", "pop", both=True)

    def undo_last_commit(self):
        return self.run("reset", "--soft", "HEAD~1", both=True)

    def revert_commit(self, rev):
        return self.run("revert", "--no-edit", rev, both=True, env=self.ident_env())

    def fetch(self):
        return self.run("fetch", "--all", "--prune", both=True, timeout=120)

    def pull(self):
        return self.run("pull", "--ff-only", both=True, timeout=120)

    def push(self):
        return self.run("push", both=True, timeout=120)


# ---- git tools for the model (same contract as every built-in tool)
def _git_repo(ctx):
    if not Git.available():
        return None, "ERROR: git is not installed"
    top = Git(ctx.root).top()
    if top is None:
        return None, "ERROR: the project folder is not a git repository (the user can create one in the Git tab)"
    return Git(top), None


def _git_rel(ctx, g, rel):
    """A project path as the model wrote it -> the path relative to the repository root (ValueError when it is outside the allowed folders)."""
    return os.path.relpath(str(ctx.resolve(rel)), str(g.path)).replace("\\", "/")


def t_git_status(ctx, args):
    g, e = _git_repo(ctx)
    if e:
        return e
    rc, out = g.run("status", "--short", "--branch", "--untracked-files=all")
    return out if rc == 0 else "ERROR: " + out


def t_git_diff(ctx, args):
    g, e = _git_repo(ctx)
    if e:
        return e
    rel = rev = None
    try:
        if args.get("path"):
            rel = _git_rel(ctx, g, args["path"])
    except ValueError as ex:
        return "ERROR: " + str(ex)
    if args.get("rev"):
        rev = safe_rev(args["rev"])
        if rev is None:
            return "ERROR: bad rev"
    rc, out = g.diff(rel, yes(args, "staged"), rev, yes(args, "stat"))
    if rc != 0:
        return "ERROR: " + out
    return out or "no differences (new untracked files are not part of a diff, see git_status)"


def t_git_log(ctx, args):
    g, e = _git_repo(ctx)
    if e:
        return e
    try:
        rel = _git_rel(ctx, g, args["path"]) if args.get("path") else None
    except ValueError as ex:
        return "ERROR: " + str(ex)
    rows = g.log(rel, max(1, min(50, arg_int(args, "limit", 15))))
    return "\n".join("%s  %s  %s  %s" % (h[1], h[2], h[3], clip(h[4], 100)) for h in rows) or "no commits"


def t_git_show(ctx, args):
    g, e = _git_repo(ctx)
    if e:
        return e
    rev = safe_rev(args.get("rev"))
    if rev is None:
        return "ERROR: rev is required (a commit hash, a branch, HEAD, HEAD~1 ...)"
    if args.get("path"):
        try:
            rc, out = g.show_file(rev, _git_rel(ctx, g, args["path"]))
        except ValueError as ex:
            return "ERROR: " + str(ex)
        if rc != 0:
            return "ERROR: " + out
        lim = ctx.settings["max_read_chars"]
        return out if len(out) <= lim else out[:lim] + "\n...[cut at %d characters]" % lim
    rc, out = g.show_commit(rev)
    return out if rc == 0 else "ERROR: " + out


def t_git_commit(ctx, args):
    g, e = _git_repo(ctx)
    if e:
        return e
    msg = str(args.get("message", "")).strip()
    if not msg:
        return "ERROR: message is required"
    given = args.get("paths")
    files = [str(p) for p in given if str(p).strip()] if isinstance(given, list) else list(ctx.changed)
    if not files:
        return "ERROR: nothing to commit: pass paths, or edit files first"
    try:
        rels = [_git_rel(ctx, g, f) for f in files]
    except ValueError as ex:
        return "ERROR: " + str(ex)
    subject, _, body = msg.partition("\n")
    rc, out = g.commit(subject.strip(), body.strip(), paths=rels)
    if rc != 0:
        return "ERROR: " + clip(out.replace("\n", " "), 300)
    return "committed " + g.run("rev-parse", "--short", "HEAD")[1].strip() + ": " + clip(subject, 80)


def t_git_restore_file(ctx, args):
    g, e = _git_repo(ctx)
    if e:
        return e
    rev = safe_rev(args.get("rev") or "HEAD")
    if rev is None:
        return "ERROR: bad rev"
    try:
        rc, text = g.show_file(rev, _git_rel(ctx, g, args.get("path")))
        if rc != 0:
            return "ERROR: " + clip(text, 300)
        if "\0" in text:
            return "ERROR: binary file, not restored"
        ctx.write_text(args.get("path"), text)
    except (ValueError, OSError) as ex:
        return "ERROR: " + str(ex)
    return "OK: %s is back to its state at %s" % (args.get("path"), rev)


_P_PATH = _p("string", "Project-relative file path")
GIT_TOOLS = [
    {"name": "git_status", "source": "built-in", "write": False, "run": t_git_status, "parameters": {},
     "description": "Show the git branch and which files are modified, staged or untracked."},
    {"name": "git_diff", "source": "built-in", "write": False, "run": t_git_diff,
     "description": "Show what changed: the working tree against HEAD, or staged changes, or against any rev. Untracked files are not included (see git_status).",
     "parameters": {"path": _P_PATH, "rev": _p("string", "Compare with this commit or branch instead of HEAD"),
                    "staged": _p("boolean", "true = only the staged changes"), "stat": _p("boolean", "true = only the list of changed files with line counts")}},
    {"name": "git_log", "source": "built-in", "write": False, "run": t_git_log,
     "description": "List recent commits (hash, date, author, message), newest first. With path: only the commits that touched that file.",
     "parameters": {"path": _P_PATH, "limit": _p("integer", "How many commits, default 15, at most 50")}},
    {"name": "git_show", "source": "built-in", "write": False, "run": t_git_show,
     "description": "Show one commit (message and changes), or with path the file exactly as it was at that commit.",
     "parameters": {"rev": _p("string", "Commit hash, branch, HEAD, HEAD~1 ...", True), "path": _P_PATH}},
    {"name": "git_commit", "source": "built-in", "write": True, "run": t_git_commit,
     "description": "Commit files with a message. Without paths it commits the files you edited in this cycle. Only the named files are committed.",
     "parameters": {"message": _p("string", "Commit message: a short first line, optional details after a blank line", True),
                    "paths": _p("array", "Project-relative files to commit", False, {"type": "string", "description": "One project-relative file path"})}},
    {"name": "git_restore_file", "source": "built-in", "write": True, "run": t_git_restore_file,
     "description": "Put one file back to how it was at HEAD (or another rev), discarding your edits to it. The file is backed up first.",
     "parameters": {"path": _p("string", "Project-relative file path", True), "rev": _p("string", "Commit or branch, default HEAD")}},
]
BUILTIN_TOOLS.extend(GIT_TOOLS)


# ═════════════════════════ plugin loading and checking ═════════════════════════

def validate_tool(t):
    if not isinstance(t, dict):
        return "a TOOLS entry is not a dict"
    if not isinstance(t.get("name"), str) or not re.fullmatch(r"[a-z][a-z0-9_]*", t["name"]):
        return "tool name must be snake_case (lowercase letters, digits, _): %r" % (t.get("name"),)
    n = t["name"]
    if not isinstance(t.get("description"), str) or not t["description"].strip():
        return "tool '%s': description is required" % n
    if not isinstance(t.get("parameters"), dict):
        return "tool '%s': parameters must be a dict (use {} for none)" % n
    for pn, pv in t["parameters"].items():
        if not isinstance(pv, dict) or pv.get("type") not in PARAM_TYPES:
            return "tool '%s': parameter '%s' needs {'type': one of %s, 'description': ..., 'required': ...}" % (n, pn, "/".join(PARAM_TYPES))
        # K3: an optional "items" spec on an array parameter ({"type": ..., "description": ...}).
        # It is validated here so a plugin with a bad item type is refused with a message the
        # Tool Maker can act on, and emitted in tool_schema so strict chat templates actually
        # get the item type instead of an untyped list.
        it = pv.get("items")
        if it is None or "items" not in pv:
            continue
        if pv["type"] != "array":
            return "tool '%s': parameter '%s': items is only allowed on an array parameter" % (n, pn)
        if not isinstance(it, dict):
            return "tool '%s': parameter '%s': items must be a dict (the type of one array item)" % (n, pn)
        if it.get("type") not in PARAM_TYPES:
            return "tool '%s': parameter '%s': items needs {'type': one of %s, 'description': ...}" % (n, pn, "/".join(PARAM_TYPES))
    if not callable(t.get("run")):
        return "tool '%s': run must be a function" % n
    return None


def validate_hook(h):
    if not isinstance(h, dict):
        return "a HOOKS entry is not a dict"
    if not isinstance(h.get("name"), str) or not h["name"].strip():
        return "hook name is required"
    if h.get("event") not in EVENTS:
        return "hook '%s': event must be one of %s" % (h["name"], ", ".join(EVENTS))
    if not isinstance(h.get("description"), str):
        return "hook '%s': description is required" % h["name"]
    if not callable(h.get("run")):
        return "hook '%s': run must be a function" % h["name"]
    return None


def load_plugin_source(src, label, taken_names=()):
    """Executes a plugin file. Returns (tools, hooks, error)."""
    mod = types.ModuleType("forge_plugin_" + re.sub(r"\W", "_", label))
    try:
        exec(compile(src, label, "exec"), mod.__dict__)
    except Exception as e:
        return [], [], "%s: %s" % (type(e).__name__, e)
    tools, hooks, seen = [], [], set(taken_names)
    for t in getattr(mod, "TOOLS", []) or []:
        err = validate_tool(t)
        if err:
            return [], [], err
        if t["name"] in seen:
            return [], [], "tool name '%s' already exists" % t["name"]
        seen.add(t["name"])
        tools.append(dict(t, source=label, write=bool(t.get("write"))))
    for h in getattr(mod, "HOOKS", []) or []:
        err = validate_hook(h)
        if err:
            return [], [], err
        hooks.append(dict(h, source=label, id=label + ":" + h["name"]))
    if not tools and not hooks:
        return [], [], "the file defines neither TOOLS nor HOOKS"
    return tools, hooks, None


def check_plugin_source(src):
    """Static check only (nothing is executed). Returns an error text or None."""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return "SyntaxError on line %s: %s" % (e.lineno, e.msg)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    found = {}
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id in ("TOOLS", "HOOKS"):
                    found[t.id] = n.value
    if not found:
        return "no top-level TOOLS = [...] or HOOKS = [...] list found"
    for kind, val in found.items():
        if not isinstance(val, ast.List):
            return kind + " must be a list literal"
        for el in val.elts:
            if not isinstance(el, ast.Dict):
                continue
            keys = {k.value for k in el.keys if isinstance(k, ast.Constant)}
            need = {"name", "description", "run"} | ({"parameters"} if kind == "TOOLS" else {"event"})
            if need - keys:
                return "an entry in %s is missing the key(s): %s" % (kind, ", ".join(sorted(need - keys)))
            for k, v in zip(el.keys, el.values):
                if isinstance(k, ast.Constant) and k.value == "run":
                    if isinstance(v, ast.Name):
                        fn = funcs.get(v.id)
                        if fn is None:
                            return "run refers to '%s' but no top-level function with that name is defined" % v.id
                        if len(fn.args.args) != 2:
                            return "function '%s' must take exactly two arguments (ctx, %s)" % (v.id, "args" if kind == "TOOLS" else "payload")
    return None


def extract_code(text):
    m = re.search(r"```(?:python|py)?[ \t]*\n(.*?)```", text, re.S)
    if m:
        return m.group(1).strip("\n") + "\n"
    return text.strip() + "\n" if "TOOLS" in text or "HOOKS" in text else ""


def new_task(prompt):
    """The one place a queue entry is built (Add as new task and Refine both use it)."""
    return {"state": "Pending", "prompt": prompt, "result": "", "backup": "", "loop_left": 0, "loop_fails": 0}


def parse_task_list(text):
    """Refine: the model's reply -> (list of task texts, error or None).
    Accepts a bare JSON array, one inside a code fence, one with chatter around it, or {"tasks": [...]}."""
    s = re.sub(r"```(?:json)?", "", text or "").strip()
    data = None
    for cand in (s, s[s.find("["):s.rfind("]") + 1] if "[" in s and "]" in s else ""):
        try:
            data = json.loads(cand)
            break
        except ValueError:
            continue
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), None)
    if not isinstance(data, list):
        return [], "the reply was not a JSON array of strings"
    out = []
    for x in data:
        if isinstance(x, dict):
            x = x.get("task") or x.get("prompt") or x.get("description")
        if isinstance(x, str) and x.strip():
            out.append(x.strip())
    if not out:
        return [], "the array held no task texts"
    return out, None




# ═════════════════════════ engine: everything that is not drawing ═════════════════════════

class Engine:
    def __init__(self):
        self.cfg = load_config()
        self.events = queue.Queue()      # worker threads -> GUI
        self.lock = threading.Lock()
        self.cancel = False
        self.running = False
        self.stop_after_run = False      # set by "Finish run, then stop"
        self.conn = None
        self.proc = None
        self.health = None
        self.ctx_info, self.recent, self._diag_pending, self.n_ctx = {}, [], [], 0  # error logger / diagnosis / real context size
        self.plugin_tools, self.plugin_hooks, self.plugin_errors = [], [], []
        self._tools_sorted, self._tool_by_name, self._schema_cache = None, None, {}  # A9/B3 caches, rebuilt only on plugin reload
        self.load_plugins()
        threading.Thread(target=self._health_loop, daemon=True).start()

    # ---- basics
    def emit(self, kind, payload=None):
        if kind == "log":
            file_log("queue", payload)
            self.recent.append(str(payload))
            del self.recent[:-80]
        elif kind == "modellog":
            file_log("model", payload)
        self.events.put((kind, payload))

    def log(self, msg):
        self.emit("log", msg)

    def save(self):
        with self.lock:
            try:
                CONFIG_PATH.write_text(json.dumps(self.cfg, indent=1), "utf-8")
            except (OSError, TypeError, ValueError):
                pass

    def server_url(self):
        c = self.cfg
        return c["server_url"].strip() or "http://%s:%s/v1/chat/completions" % (c["host"], c["port"])

    def base_url(self):
        u = self.server_url()
        i = u.find("/v1/")
        return (u[:i] if i >= 0 else u).rstrip("/")

    def _http(self, method, url, body=None, timeout=10, track=False):
        u = urlparse(url)
        https = u.scheme == "https"
        conn = (http.client.HTTPSConnection if https else http.client.HTTPConnection)(u.hostname, u.port or (443 if https else 80), timeout=timeout)
        if track:
            self.conn = conn
        headers = {"Content-Type": "application/json"}
        if self.cfg["api_key"]:
            headers["Authorization"] = "Bearer " + self.cfg["api_key"]
        try:
            conn.request(method, u.path + ("?" + u.query if u.query else ""), body=body, headers=headers)
            r = conn.getresponse()
            return r.status, r.read().decode("utf-8", "replace")
        finally:
            conn.close()
            if track:
                self.conn = None

    def request_cancel(self):
        self.cancel = True
        c = self.conn
        if c is not None:
            try:
                c.sock.shutdown(socket.SHUT_RDWR)  # wakes up the blocked read in the worker thread
            except Exception:
                pass
            try:
                c.close()
            except Exception:
                pass
        self.log("Cancel requested...")

    def request_soft_stop(self):
        """Stop the queue AFTER the current run has finished (checks included). Nothing is cancelled mid-request."""
        if not self.running:
            return
        self.stop_after_run = True
        self.emit("softstop")
        self.log("Queue will stop after the current run finishes (checks included). Press Stop now to cancel immediately.")

    def stop_loop_after_run(self, task):
        """End one task's loop after its current run finishes. The run keeps its work and the queue carries on."""
        task["stop_loop"] = True
        self.log("Loop will be switched off after the current run finishes.")

    def _after_stop(self, task, was_loop):
        """After Stop now: apply a pending 'stop loop' and, per the revert_on_stop setting, restore the files the cancelled run edited."""
        if task.pop("stop_loop", False):
            task["loop_left"] = 0
        mode = self.cfg.get("revert_on_stop", "loops_only")
        if mode == "never" or (mode == "loops_only" and not was_loop):
            return
        bk = task.get("backup") or ""
        if not bk or not Path(bk).is_dir():
            return  # the cancelled run had not edited anything yet
        self.revert_task(task, force=True)
        if str(task.get("result", "")).startswith("Reverted"):
            task["result"] = "Cancelled. " + task["result"]
            self.save()
            self.emit("queue")

    def check_cancel(self):
        if self.cancel:
            raise Cancelled()

    def _health_loop(self):
        while True:
            try:
                st, _ = self._http("GET", self.base_url() + "/health", timeout=2)
                ok = st == 200
            except Exception:
                ok = False
            if ok != self.health:
                self.health = ok
                self.emit("health", ok)
            time.sleep(3)

    # ---- plugins, tools, hooks
    def load_plugins(self):
        self.plugin_tools, self.plugin_hooks, self.plugin_errors = [], [], []
        self._tools_sorted, self._tool_by_name, self._schema_cache = None, None, {}  # caches die with the plugins that made them (A9/B3)
        PLUGIN_DIR.mkdir(exist_ok=True)
        example = PLUGIN_DIR / "_example_plugin.py"
        if not example.exists():
            example.write_text(EXAMPLE_PLUGIN, "utf-8")
        taken = {t["name"] for t in BUILTIN_TOOLS}
        for f in sorted(PLUGIN_DIR.glob("*.py")):
            if f.name.startswith("_"):
                continue
            tools, hooks, err = load_plugin_source(f.read_text("utf-8"), f.name, taken)
            if err:
                self.plugin_errors.append("%s: %s" % (f.name, err))
                continue
            taken.update(t["name"] for t in tools)
            self.plugin_tools += tools
            self.plugin_hooks += hooks

    # ---- tools and hooks
    def all_tools(self):
        """The tool list is sorted ONCE per plugin reload (A9/B3): the same list object and
        byte-identical schema JSON keep llama-server's prompt cache valid, while every tool
        call and every turn reuses the cache instead of re-sorting and re-building the schemas."""
        if self._tools_sorted is None:
            self._tools_sorted = sorted(BUILTIN_TOOLS + self.plugin_tools, key=lambda t: t["name"])
            self._tool_by_name = {t["name"]: t for t in self._tools_sorted}
        return self._tools_sorted

    def tool_schema(self, t):
        s = self._schema_cache.get(t["name"])
        if s is None:
            props = {n: param_schema(p) for n, p in t["parameters"].items()}  # K3: keeps "items" for arrays
            req = [n for n, p in t["parameters"].items() if p.get("required")]
            schema = {"type": "object", "properties": props}
            if req:
                schema["required"] = req
            s = {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": schema}}
            self._schema_cache[t["name"]] = s
        return s

    def tool_enabled(self, t):
        return t["name"] == "finish_task" or t["name"] not in self.cfg["disabled_tools"]

    def loop_tools(self):
        return [self.tool_schema(t) for t in self.all_tools() if self.tool_enabled(t)]

    def exec_tool(self, ctx, name, args):
        if getattr(ctx, "finish", None) is not None:
            return "ERROR: the task already ended with finish_task; tools are refused"
        self.all_tools()  # makes sure the name dict exists
        t = self._tool_by_name.get(name)
        if t is None or not self.tool_enabled(t):
            self.err("unknown_tool", name, tool=name)
            return "ERROR: unknown tool " + name
        try:
            # P8: one hard cap for every tool, built-in or plugin, so a runaway result
            # (a plugin that dumps a whole file, a huge listing) cannot eat the context
            # of every later turn.
            res = cap_out(str(t["run"](ctx, args)), max(500, self.cfg["max_tool_output_chars"]))
        except Exception as e:
            self.err("tool_crash", "%s: %s" % (type(e).__name__, e), tool=name, args=clip(json.dumps(args), 300))
            return "ERROR: tool crashed: %s: %s" % (type(e).__name__, e)
        if res.startswith("ERROR"):
            self.err("tool_error", res, tool=name, args=clip(json.dumps(args), 300))
        return res

    def listing(self):
        c, rows = self.cfg, []
        for t in self.all_tools():
            rows.append(dict(iid="tool:" + t["name"], kind="tool (writes)" if t.get("write") else "tool", on=self.tool_enabled(t), name=t["name"], source=t["source"], desc=t["description"]))
        for h in self.plugin_hooks:
            rows.append(dict(iid="hook:" + h["id"], kind="hook: " + h["event"], on=h["id"] not in c["disabled_hooks"], name=h["name"], source=h["source"], desc=h["description"]))
        for i, h in enumerate(c["command_hooks"]):
            rows.append(dict(iid="cmd:%d" % i, kind="hook: after_cycle", on=bool(h.get("enabled")), name=h["name"], source="command", desc=h["command"]))
        return rows

    def toggle(self, iid):
        c = self.cfg
        kind, _, key = iid.partition(":")
        if kind == "cmd":
            h = c["command_hooks"][int(key)]
            h["enabled"] = not h.get("enabled")
        else:
            lst = c["disabled_tools"] if kind == "tool" else c["disabled_hooks"]
            if key == "finish_task":
                return
            lst.remove(key) if key in lst else lst.append(key)
        self.save()

    def shell(self, cmd, cwd):
        """Runs a command, returns (exit code, output). Polls the cancel flag, so Stop now ends a running verify/hook command at once."""
        try:
            p = subprocess.Popen(cmd, shell=True, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                 text=True, errors="replace", start_new_session=(os.name == "posix"))
        except Exception as e:
            return 127, str(e)
        deadline = time.time() + max(5, self.cfg["verify_timeout"])
        while True:
            try:
                out, _ = p.communicate(timeout=0.3)
                return p.returncode, (out or "").strip()
            except subprocess.TimeoutExpired:
                if not (self.cancel or time.time() > deadline):
                    continue
                try:
                    os.killpg(p.pid, signal.SIGKILL) if os.name == "posix" else p.kill()
                except Exception:
                    pass
                try:
                    p.communicate(timeout=5)
                except Exception:
                    pass
                if self.cancel:
                    raise Cancelled()
                return 124, "timed out"
            except Exception as e:
                return 127, str(e)

    def run_hooks(self, event, ctx, payload):
        """Plugin hooks for one event. Returns the list of problems (only after_cycle problems matter)."""
        problems = []
        for h in self.plugin_hooks:
            if h["event"] != event or h["id"] in self.cfg["disabled_hooks"]:
                continue
            try:
                out = h["run"](ctx, payload)
            except Exception as e:
                out = "ERROR: hook crashed: %s: %s" % (type(e).__name__, e)
            if out:
                self.log("[hook %s] %s" % (h["name"], clip(out, 300)))
                if str(out).startswith("ERROR") and event == "after_cycle":
                    problems.append("Hook %s reported: %s" % (h["name"], out))
        return problems

    def run_command_hooks(self, ctx, files):
        problems = []
        for h in self.cfg["command_hooks"]:
            if not h.get("enabled"):
                continue
            pats = [p.strip() for p in h.get("glob", "*").split(",") if p.strip()] or ["*"]
            mine = [f for f in files if any(fnmatch.fnmatch(Path(f).name, p) or fnmatch.fnmatch(f, p) for p in pats)]
            if not mine:
                continue
            cmd = h["command"]
            cmds = [cmd.replace("{file}", shlex.quote(f)) for f in mine] if "{file}" in cmd else [cmd.replace("{files}", " ".join(shlex.quote(f) for f in mine))]
            for one in cmds:
                rc, out = self.shell(one, ctx.root)
                self.log("[hook %s] exit %d %s" % (h["name"], rc, clip(out.replace("\n", " "), 200)))
                if rc != 0 and h.get("blocking"):
                    problems.append("Command hook '%s' failed (exit %d):\n%s" % (h["name"], rc, clip(out, 1500)))
        return problems

    # ---- the model
    def chat(self, messages, tools=None, track=True):
        c = self.cfg
        body = {"messages": messages, "stream": False}
        if tools:
            body["tools"], body["tool_choice"] = tools, "auto"
        if not c["use_server_sampling"]:
            body.update(temperature=c["temperature"], top_p=c["top_p"], top_k=c["top_k"], max_tokens=c["max_tokens"])
        if c["thinking"] != "server_default":
            body["chat_template_kwargs"] = {"enable_thinking": c["thinking"] == "on"}
        if c["model_name"]:
            body["model"] = c["model_name"]
        try:
            status, text = self._http("POST", self.server_url(), json.dumps(body), c["request_timeout"], track=track)
        except Exception as e:
            if self.cancel:
                raise Cancelled()
            raise RuntimeError("request failed: %s" % e)
        if self.cancel:
            raise Cancelled()
        if status != 200:
            raise RuntimeError("HTTP %d: %s" % (status, clip(text, 500)))
        j = json.loads(text)
        usage = dict(j.get("usage") or {})
        usage["timings"] = j.get("timings") or {}
        return j["choices"][0]["message"], usage

    @staticmethod
    def split_reasoning(msg):
        content, reasoning = msg.get("content") or "", msg.get("reasoning_content") or ""
        if not reasoning and "</think>" in content:
            k = content.index("</think>")
            reasoning, content = content[:k].replace("<think>", "").strip(), content[k + 8:].strip()
        return content, reasoning

    def ctx_budget(self):
        """Budget in characters for the chat. The server's real n_ctx is read every time (self.n_ctx), even when a manual budget is set."""
        b = self.cfg["context_budget_chars"]
        try:
            st, txt = self._http("GET", self.base_url() + "/props", timeout=5)
            n = json.loads(txt)["default_generation_settings"]["n_ctx"]
            if st == 200 and n > 0:
                self.n_ctx = int(n)
        except Exception:
            pass
        if b > 0:
            return b
        return int(self.n_ctx * 3.0 * 0.5) if self.n_ctx else 22000

    @staticmethod
    def count_turn(stats, usage, secs):
        """Adds one model reply to the running totals of the task (turns, tokens, speed, biggest context)."""
        if stats is None:
            return
        tm = usage.get("timings") or {}
        out = usage.get("completion_tokens") or tm.get("predicted_n") or 0
        stats["turns"] = stats.get("turns", 0) + 1
        stats["out_tok"] = stats.get("out_tok", 0) + out
        stats["gen_s"] = stats.get("gen_s", 0.0) + ((tm.get("predicted_ms") or 0) / 1000.0 or secs)
        stats["ctx_peak"] = max(stats.get("ctx_peak", 0), usage.get("prompt_tokens") or 0)

    def log_thinking(self, reasoning):
        """The log says WHAT the model thinks about, not the thinking itself (setting: Log: full thinking text)."""
        names = [t["name"] for t in self.all_tools()]
        self.log("[thinking] Thinking about: %s  (%d chars)" % (think_subjects(reasoning, names), len(reasoning)))
        if self.cfg.get("log_full_thinking"):
            self.log("[thinking] text: " + clip(reasoning, 1500))

    def log_tool(self, name, args, result):
        mode = self.cfg.get("log_tool_output", "errors_only")
        flat = result.replace("\n", " ")
        if result.startswith("ERROR"):
            tail = clip(flat, 200)
        elif mode == "full":
            tail = flat
        elif mode == "short":
            tail = clip(flat, 100)
        else:
            tail = "ok (%d chars)" % len(result)
        self.log("[tool] %s(%s) -> %s" % (name, clip(json.dumps(args), 120), tail))

    def err(self, kind, detail, **extra):
        """One record of the AI (or the server) going wrong: forge_logs/errors.jsonl for the later analysis, plus a readable line in Log -> Errors."""
        rec = {"time": datetime.now().isoformat(timespec="seconds"), "kind": kind, "detail": clip(str(detail), 1500),
               "model": self.cfg.get("model_name") or Path(str(self.cfg.get("model_path") or "")).name}
        rec.update(self.ctx_info)
        rec.update(extra)
        try:
            with _LOG_LOCK:
                LOG_DIR.mkdir(exist_ok=True)
                if ERROR_FILE.exists() and ERROR_FILE.stat().st_size > ERROR_MAX:
                    data = ERROR_FILE.read_bytes()[-(ERROR_MAX // 2):]
                    nl = data.find(b"\n")
                    ERROR_FILE.write_bytes(data[nl + 1:] if nl >= 0 else data)
                with open(ERROR_FILE, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError):
            pass
        txt = "%-15s cycle %s turn %s  %s%s" % (kind, rec.get("cycle", "-"), rec.get("turn", "-"), (rec["tool"] + ": ") if rec.get("tool") else "",
                                               clip(str(detail).replace("\n", " "), 300))
        file_log("errors", txt)
        self.emit("errlog", txt)

    def diagnose(self, title, detail, task_text):
        """A brand-new chat (no tools, none of the task's history) that explains a failure and what to change. Result: Log -> Diagnosis."""
        if self.cancel:
            return
        c = self.cfg
        knobs = ", ".join("%s=%s" % (k, c.get(k)) for k in ("ctx_size", "max_turns", "max_cycles", "context_budget_chars", "max_read_chars",
                                                          "max_tool_output_chars", "max_tokens", "use_server_sampling", "thinking", "verify_command"))
        user = ("WHAT HAPPENED\n%s\n\nERROR TEXT\n%s\n\nTASK\n%s\n\nSETTINGS\n%s (server n_ctx: %s)\n\nLAST LOG LINES\n%s\n"
                % (title, clip(detail, 1500), clip(task_text, 1200), knobs, self.n_ctx or "unknown", "\n".join(clip(x, 300) for x in self.recent[-40:])))
        try:
            msg, _ = self.chat([{"role": "system", "content": c["diagnose_prompt"]}, {"role": "user", "content": user}], track=False)
            content, _ = self.split_reasoning(msg)
            text = content.strip() or "(the model gave an empty answer)"
        except Cancelled:
            return
        except Exception as e:
            text = "Could not run the diagnosis: %s" % e
        out = "== Diagnosis: %s  (%s) ==\n%s" % (title, datetime.now().strftime("%H:%M:%S"), text)
        file_log("diagnosis", out)
        self.emit("diag", out + "\n\n")

    def analyze_errors(self):
        """Counts first (no model needed), then one fresh chat that names the model's weak spots. Saved as forge_logs/weakness_report.md."""
        recs = read_error_records()
        if not recs:
            self.emit("diag", "== Weakness analysis ==\nNo errors recorded yet (forge_logs/errors.jsonl is empty).\n\n")
            return
        summary = error_summary(recs)
        self.emit("diag", "== Error counts ==\n" + summary + "\n\n")
        newest = "\n".join(json.dumps({k: clip(v, 200) if isinstance(v, str) else v for k, v in r.items() if k not in ("model",)}, ensure_ascii=False)
                           for r in recs[-12:])
        try:
            self.cancel = False
            msg, _ = self.chat([{"role": "system", "content": ANALYZE_PROMPT},
                                {"role": "user", "content": "COUNTS\n%s\n\nNEWEST RECORDS\n%s\n" % (summary, newest)}], track=False)
            content, _ = self.split_reasoning(msg)
            text = content.strip() or "(the model gave an empty answer)"
            (LOG_DIR / "weakness_report.md").write_text("# Weakness report %s\n\n```\n%s\n```\n\n%s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M"), summary, text), "utf-8")
        except Cancelled:
            return
        except Exception as e:
            text = "Could not run the analysis: %s" % e
        out = "== Weakness analysis (%s) ==\n%s" % (datetime.now().strftime("%H:%M:%S"), text)
        file_log("diagnosis", out)
        self.emit("diag", out + "\n\n")

    def model_loop(self, ctx, system, user, stats=None):
        c = self.cfg
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        tools, budget, summary = self.loop_tools(), self.ctx_budget(), ""
        last_content = ""
        recent_calls = []
        for turn in range(c["max_turns"]):
            self.check_cancel()
            trim_old_outputs(msgs, budget)
            self.log("[turn %d] waiting for model%s..." % (turn + 1, " (thinking)" if c["thinking"] == "on" else ""))
            t0 = time.time()
            self.ctx_info["turn"] = turn + 1
            try:
                msg, usage = self.chat(msgs, tools)
            except RuntimeError as e:
                low = str(e).lower()
                if "exceed" not in low or "context" not in low or len(msgs) < 6:
                    raise
                self.err("context_overflow", str(e), messages=len(msgs), chars=sum(msg_chars(m) for m in msgs))
                self.log("[context] the server refused the request as too long: trimming all old output and retrying once")
                trim_old_outputs(msgs, 0)
                msg, usage = self.chat(msgs, tools)
            content, reasoning = self.split_reasoning(msg)
            calls = msg.get("tool_calls") or []
            dt = time.time() - t0
            self.count_turn(stats, usage, dt)
            pt = usage.get("prompt_tokens") or 0
            if self.n_ctx and pt > self.n_ctx * 0.6:  # the server's own token count beats any characters-per-token guess
                tighter = max(4000, int(sum(msg_chars(m) for m in msgs) * self.n_ctx * 0.5 / pt))
                if tighter < budget:
                    budget = tighter
                    self.log("[context] %d of %d tokens used: old tool output is trimmed harder from now on (budget %d chars)" % (pt, self.n_ctx, budget))
            self.log("[model] %.0fs%s%s" % (dt, ", %s tok" % usage["completion_tokens"] if "completion_tokens" in usage else "",
                                            ", ctx %s" % usage["prompt_tokens"] if usage.get("prompt_tokens") else ""))
            if reasoning:
                self.log_thinking(reasoning)
            if content and content != last_content:  # the model can repeat the same text on consecutive turns
                self.log("[model] " + clip(content, 1500))
            last_content = content
            am = {"role": "assistant", "content": content}
            if calls:
                am["tool_calls"] = calls
            msgs.append(am)
            if not calls:
                summary = content
                if not ctx.finish:
                    self.err("no_finish", clip(content, 300) or "(empty reply)")
                break
            for call in calls:
                if ctx.finish:  # finish_task already decided: never run the calls that came with it
                    msgs.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": "ERROR: the task already ended with finish_task; this call was not run"})
                    continue
                fn = call.get("function", {})
                name, raw = fn.get("name", ""), fn.get("arguments")
                try:
                    args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except ValueError:
                    args = {}
                    self.err("bad_arguments", clip(str(raw), 300), tool=name)
                key = name + json.dumps(args, sort_keys=True)
                if recent_calls.count(key) >= 2:
                    self.err("repeated_call", "the same call a third time within the last 6 calls: " + clip(key, 200), tool=name)
                recent_calls = (recent_calls + [key])[-6:]
                result = self.exec_tool(ctx, name, args)
                if stats is not None:
                    stats["tool_calls"] = stats.get("tool_calls", 0) + 1
                self.log_tool(name, args, result)
                msgs.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
            if ctx.finish:
                break
        else:
            self.err("turn_limit", "%d tool turns used without finish_task" % c["max_turns"])
        return summary

    # ---- backup folders: one UNIQUE folder per task (A3)
    def _new_backup_dir(self, root):
        """The per-task backup folder must never be shared with another task.
        A bare second-resolution timestamp collides for tasks run back-to-back inside the same
        second, and the shared folder makes _backup's "keep the earliest copy" check skip straight
        into the PREVIOUS task's backups, so Revert restored the wrong bytes (and a created-then-
        edited file kept a stale .new marker). Timestamp keeps the folder readable, the uuid suffix
        makes it unique, and the is_dir() guard refuses to reuse a folder that is already on disk."""
        base = root / ".forge_backup"
        while True:
            d = base / (datetime.now().strftime("%Y%m%d_%H%M%S") + "-" + uuid.uuid4().hex[:6])
            if not d.is_dir():
                return d

    # ---- one task = fresh context per cycle
    def run_task(self, task):
        c = self.cfg
        root = Path(c["project_root"])
        if not root.is_dir():
            raise RuntimeError("project folder does not exist: %s" % root)
        backup = self._new_backup_dir(root)
        task["backup"] = str(backup)
        text = task["prompt"].strip()
        self.run_hooks("before_task", Ctx(self, backup), {"task": text})
        notes, problems, state, msg = [], [], "Failed", ""
        stats, t_start = {"cycles": 0}, time.time()
        self.ctx_info = {"task": clip(text.replace("\n", " "), 160), "cycle": 0, "turn": 0}
        task.pop("stats", None)
        all_changed = []
        for cycle in range(1, c["max_cycles"] + 1):
            self.check_cancel()
            self.log("== Cycle %d/%d ==" % (cycle, c["max_cycles"]))
            stats["cycles"] = cycle
            self.ctx_info["cycle"] = cycle
            ctx = Ctx(self, backup)
            user = "TASK:\n" + text + "\n"
            if problems:
                user += "\nProblems found after your last edits (fix them):\n" + "\n".join("%d) %s" % (i + 1, p) for i, p in enumerate(problems)) + "\n"
            if notes:
                user += "\nEarlier cycles on this task already did this (do not repeat a failed fix):\n" + "\n".join(notes) + "\n"
            summary = self.model_loop(ctx, c["task_prompt"], user, stats)
            all_changed += [f for f in ctx.changed if f not in all_changed]
            if ctx.finish and not ctx.finish[0]:
                self.err("gave_up", ctx.finish[1])
                state, msg = "Failed", "Gave up: " + ctx.finish[1]
                break
            if not ctx.changed:
                if ctx.finish and cycle == 1:
                    state, msg = "Done", "No changes were needed. " + ctx.finish[1]
                else:
                    self.err("no_edits", "the cycle ended without a single edit")
                    state, msg = "Failed", "Model made no edits (gave up or hit the turn limit). See the log."
                break
            done_text = ctx.finish[1] if ctx.finish else summary
            self.log("Edited: " + ", ".join(ctx.changed))
            problems = self.run_command_hooks(ctx, list(ctx.changed)) + self.run_hooks("after_cycle", ctx, {"files": list(ctx.changed)})
            if c["verify_command"].strip():
                rc, out = self.shell(c["verify_command"], ctx.root)
                self.log("[verify] exit %d %s" % (rc, clip(out.replace("\n", " "), 200)))
                if rc != 0:
                    problems.append("Verify command '%s' failed (exit %d):\n%s" % (c["verify_command"], rc, clip(out, 1500)))
            if not problems:
                state, msg = "Done", "Checks passed after %d cycle(s). %s" % (cycle, clip(done_text, 200))
                break
            for p in problems:
                self.err("check_failed", clip(p, 600), files=list(ctx.changed))
            notes.append("Cycle %d: edited %s. %s" % (cycle, ", ".join(ctx.changed), clip(done_text, 300)))
            state, msg = "Failed", "Problems remain after %d cycle(s): %s" % (cycle, clip(problems[0], 200))
        stats["seconds"] = time.time() - t_start
        task["stats"] = final_stats(stats)
        if stats.get("turns"):
            self.log("[stats] " + stats_text(task["stats"]))
        self.git_commit_task(task, state, msg, all_changed)
        self.run_hooks("after_task", Ctx(self, backup), {"state": state, "summary": msg})
        return state, msg

    def git_ignore_lines(self):
        names = [d.strip() for d in str(self.cfg["hidden_dirs"]).split(",") if d.strip() and d.strip() != ".git"]
        return "\n".join([n.rstrip("/") + "/" for n in names] + ["*.pyc"]) + "\n"

    def git_commit_task(self, task, state, msg, files):
        """After a task: commit exactly the files it edited, so every task is one version in the Git tab (setting: Git -> Commit after each task)."""
        c = self.cfg
        mode = c.get("git_auto_commit", "done")
        if mode == "off" or not files or (mode == "done" and state != "Done"):
            return
        try:
            if not Git.available():
                self.log("[git] git is not installed: no commit")
                return
            root = Path(c["project_root"]).resolve()
            top = Git(root).top()
            if top is None:
                if not c.get("git_auto_init"):
                    self.log("[git] the project folder is not a repository, so no commit (create one in the Git tab, or switch on Git: Create the repository)")
                    return
                rc, out = Git(root).init(self.git_ignore_lines())
                self.log("[git] git init: " + clip(out.replace("\n", " "), 150))
                top = Git(root).top()
                if top is None:
                    return
            g = Git(top)
            g.ensure_exclude()
            paths = [os.path.relpath(str(root / f), str(top)).replace("\\", "/") for f in files]
            try:
                n = c["queue"].index(task) + 1
            except ValueError:
                n = 0
            subject = "Forge task %d: %s%s" % (n, clip(" ".join(task["prompt"].split()), 60), "" if state == "Done" else " [%s]" % state)
            body = "Task:\n%s\n\nResult: %s - %s\nFiles: %s\nCost: %s" % (clip(task["prompt"].strip(), 1500), state, msg, ", ".join(files),
                                                                          stats_text(task.get("stats")) or "-")
            rc, out = g.commit(subject, body, paths=paths)
            if rc == 0:
                rc2, h = g.run("rev-parse", "--short", "HEAD")
                task["commit"] = h.strip() if rc2 == 0 else ""
                self.log("[git] committed %s: %s" % (task["commit"], subject))
            else:
                self.log("[git] no commit: " + clip(out.replace("\n", " "), 200))
        except Exception as e:
            self.log("[git] commit step failed: %s: %s" % (type(e).__name__, e))

    def start_queue(self):
        if self.running:
            return
        self.cancel, self.running = False, True
        self.stop_after_run = False
        self.emit("running", True)
        threading.Thread(target=self._queue_loop, daemon=True).start()

    def _queue_loop(self):
        c = self.cfg
        try:
            while not self.cancel and not self.stop_after_run:
                task = next((t for t in c["queue"] if t["state"] in ("Pending", "Loop") and t["prompt"].strip()), None)
                if task is None:
                    counts = {s: sum(1 for t in c["queue"] if t["state"] == s) for s in ("Pending", "Loop", "Running", "Done", "Failed")}
                    self.log("Queue finished: " + ", ".join("%d %s" % (v, k.lower()) for k, v in counts.items()))
                    break
                used_budget = task["state"] == "Loop"  # only a task coming back from the loop queue spends a run below
                is_loop_run = used_budget or task.get("loop_left", 0) != 0
                task.pop("stop_loop", None)
                if task["state"] == "Loop":
                    task["loop_left"] -= 1
                task["state"], task["result"] = "Running", ""
                task["loop_fails"] = task.get("loop_fails", 0) + 1
                self.save()
                self.emit("queue")
                self.log("##### Task %d/%d%s: %s" % (c["queue"].index(task) + 1, len(c["queue"]),
                         " [loop]" if task.get("loop_left", 0) != 0 else "", clip(task["prompt"].replace("\n", " "), 100)))
                try:
                    state, msg = self.run_task(task)
                except Cancelled:
                    task["state"], task["result"] = "Pending", "Cancelled"
                    if task.get("loop_left", 0) != 0:
                        if used_budget:  # a first run (Pending) never spent a loop run, so there is nothing to give back
                            task["loop_left"] += 1  # the cancelled run did not happen: give the budget back
                        task["loop_fails"] -= 1
                    self.log("Cancelled. The task is back to Pending.")
                    self._after_stop(task, is_loop_run)
                    break
                except Exception as e:  # server problem, not the task's fault: the task stays Pending
                    task["state"], task["result"] = "Pending", clip(str(e), 200)
                    if task.get("loop_left", 0) != 0:
                        if used_budget:
                            task["loop_left"] += 1
                        task["loop_fails"] -= 1
                    self.err("server", str(e))
                    self._diag_pending.append(("The queue stopped with a server / request error", str(e), task["prompt"]))
                    self.log("STOPPED: %s" % e)
                    break
                if task.pop("stop_loop", False):
                    task["loop_left"] = 0
                    self.log("LOOP: switched off as requested (the run was allowed to finish).")
                if state == "Done":
                    task["loop_fails"] = 0
                if task.get("loop_left", 0) != 0 and state == "Done":
                    # loop toggle: send the finished task back to the back of the que
                    task["state"], task["result"] = "Loop", ""
                    c["queue"].append(c["queue"].pop(c["queue"].index(task)))
                    left = task["loop_left"]
                    self.log("LOOP: task sent to the back of the que (%s runs left)"
                             % ("infinite" if left < 0 else left))
                else:
                    if task.get("loop_left", 0) != 0 and state != "Done" and task.get("loop_fails", 0) >= max(1, c["max_loop_failures"]):
                        task["loop_left"] = 0  # a looping task kept failing: stop its loop
                        self.log("LOOP: task failed %d times in a row - the loop is switched off (setting: Loop: max failed runs in a row)." % task["loop_fails"])
                    task["state"], task["result"] = state, clip(msg, 300)
                    self.log("%s: %s" % (state.upper(), msg))
                    if state == "Failed":
                        self._diag_pending.append(("Task failed", msg, task["prompt"]))
                self.save()
                self.emit("queue")
                if state == "Failed" and c["stop_on_failure"]:
                    self.log("Queue stopped because a task failed (setting: Stop queue on a failed task).")
                    break
            if self.stop_after_run and not self.cancel:
                self.log("Queue stopped after the current run, as requested. Press Start to continue.")
        finally:
            self.stop_after_run = False
            self.running = False
            self.save()
            self.emit("queue")
            self.emit("running", False)
            pending, self._diag_pending = self._diag_pending[:3], []
            if pending and c.get("auto_diagnose", True) and not self.cancel:
                threading.Thread(target=lambda: [self.diagnose(*p) for p in pending], daemon=True).start()

    def revert_task(self, task, force=False):
        """Restore a task's files ATOMICALLY (A12).

        Nothing in the project is touched until every backup has been copied
        into a staging folder (inside .forge_backup, same filesystem as the
        project) and verified byte-for-byte. The current content of the target
        files is staged next to it, so a swap that fails half way through is
        rolled back and the backup folder is kept so the revert can be retried.
        """
        if self.running and not force:
            self.log("Stop the queue before reverting.")
            return
        root = Path(self.cfg["project_root"]).resolve()
        d = Path(task.get("backup") or "")
        if not d.is_dir() or Path(str(d.parent)).resolve() != root / ".forge_backup":
            self.log("This task has no backup to restore.")
            return
        stage = None
        try:
            stage = Path(tempfile.mkdtemp(prefix="revert_", dir=str(d.parent)))
            bak, cur = stage / "b", stage / "c"
            bak.mkdir()
            cur.mkdir()
            plan, problems = [], []
            for f in sorted(d.rglob("*")):
                if f.is_dir():
                    continue
                rel = f.relative_to(d)
                is_new = rel.name.endswith(".new")
                s_rel = str(rel)[:-4] if is_new else str(rel)
                target = root / s_rel
                sc = cur / s_rel
                if target.is_file():  # keep the current bytes so a failed swap can be undone
                    sc.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(str(target), str(sc))
                    except OSError as e:
                        problems.append("could not stage the current copy of %s: %s" % (s_rel, e))
                        break
                elif target.exists():
                    problems.append("cannot restore %s: a folder is in the way" % s_rel)
                    break
                sb = None
                if not is_new:
                    sb = bak / s_rel
                    sb.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copy2(str(f), str(sb))
                    except OSError as e:
                        problems.append("could not stage the backup of %s: %s" % (s_rel, e))
                        break
                    if not same_content(f, sb):
                        problems.append("the staged copy of %s does not match its backup" % s_rel)
                        break
                plan.append((is_new, s_rel, target, sb, sc if target.is_file() else None))
            if problems:
                self.log("Revert aborted, nothing was changed: " + " | ".join(problems))
                shutil.rmtree(str(stage), ignore_errors=True)
                return
            done, fail = [], None
            for is_new, s_rel, target, sb, sc in plan:
                try:
                    if is_new:
                        if target.exists():
                            target.unlink()
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            os.replace(str(sb), str(target))  # same filesystem: a rename
                        except OSError:
                            shutil.copy2(str(sb), str(target))  # cross-device fallback
                except OSError as e:
                    fail = "restoring %s failed: %s" % (s_rel, e)
                    break
                done.append((is_new, s_rel, target, sc))
            if fail:  # roll back what was already swapped, keep the backups
                for is_new, s_rel, target, sc in reversed(done):
                    try:
                        if sc is not None:
                            sc.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(str(sc), str(target))
                        elif not is_new and target.exists():
                            target.unlink()  # this file did not exist before the revert
                    except OSError:
                        pass
                self.log("Revert aborted (%s); the backups are still on disk, you can try again." % fail)
                shutil.rmtree(str(stage), ignore_errors=True)
                return
            n = len(plan)
            shutil.rmtree(str(stage), ignore_errors=True)
            shutil.rmtree(str(d), ignore_errors=True)
            task.update(backup="", state="Pending", result="Reverted %d file(s)" % n)
            self.save()
            self.log("Reverted %d file(s). The task is Pending again." % n)
            self.emit("queue")
        except OSError as e:
            if stage is not None:
                shutil.rmtree(str(stage), ignore_errors=True)
            self.log("Revert aborted: %s" % e)

    # ---- the Tool Maker: model writes a plugin, Forge checks it, up to 3 attempts
    def spec_prompt(self):
        existing = ", ".join(t["name"] for t in self.all_tools())
        return self.cfg["tool_spec"].replace("{existing}", existing).replace("{example}", EXAMPLE_PLUGIN)

    def make_plugin(self, request):
        self.cancel = False
        msgs = [{"role": "system", "content": self.spec_prompt()}, {"role": "user", "content": request.strip()}]
        code, err = "", "no answer"
        for attempt in range(1, 4):
            msg, _ = self.chat(msgs)
            content, _ = self.split_reasoning(msg)
            code = extract_code(content)
            err = check_plugin_source(code) if code else "there was no ```python code block in the reply"
            if not err:
                return code, "Checked OK after %d attempt(s). Read the code, then press Save." % attempt
            msgs += [{"role": "assistant", "content": content},
                     {"role": "user", "content": "The file was rejected: %s\nReply again with the complete corrected file in one ```python block." % err}]
        return code or "", "The model could not produce a valid file in 3 attempts (%s). Edit it by hand or try again." % err

    # ---- Refine: the model turns one rough request into an ordered list of self-contained tasks
    def refine_prompt_text(self):
        c = self.cfg
        try:
            files = Ctx(self, None).files()  # the model sees what already exists, so tasks name real files
        except (ValueError, OSError):
            files = []
        keep = max(10, c["list_limit"])
        listing = "(the project folder is empty)"
        if files:
            listing = "\n".join(files[:keep])
            if len(files) > keep:
                listing += "\n...[%d more files not shown]" % (len(files) - keep)
        return c["refine_prompt"].replace("{max_tasks}", str(max(1, c["refine_max_tasks"]))).replace("{files}", listing)

    def refine(self, text):
        """Returns (list of task texts, status message). Up to 3 attempts, like the Tool Maker."""
        self.cancel = False
        limit = max(1, self.cfg["refine_max_tasks"])
        msgs = [{"role": "system", "content": self.refine_prompt_text()}, {"role": "user", "content": "REQUEST:\n" + text.strip()}]
        err = "no answer"
        for attempt in range(1, 4):
            msg, _ = self.chat(msgs)
            content, _ = self.split_reasoning(msg)
            tasks, err = parse_task_list(content)
            if not err:
                extra = "" if len(tasks) <= limit else " (the model wrote %d, only the first %d were kept)" % (len(tasks), limit)
                return tasks[:limit], "Refined into %d task(s)%s. Review them in the queue." % (min(len(tasks), limit), extra)
            msgs += [{"role": "assistant", "content": content},
                     {"role": "user", "content": "That reply was rejected: %s\nReply again with ONLY a JSON array of strings, one string per task." % err}]
        return [], "The model did not return a usable task list in 3 attempts (%s). Try again or rephrase." % err

    # ---- start / stop the model server
    def launch_command(self):
        c = self.cfg
        if c["custom_launch"].strip():
            return c["custom_launch"].replace("{port}", str(c["port"])).replace("{host}", c["host"]), True
        cmd = [c["llama_binary"] or "llama-server"]
        if c["model_path"]:
            cmd += ["-m", c["model_path"]]
        cmd += ["--host", c["host"], "--port", str(c["port"])]
        if c["ctx_size"] > 0:
            cmd += ["-c", str(c["ctx_size"])]
        if c["gpu_layers"] >= 0:
            cmd += ["-ngl", str(c["gpu_layers"])]
        try:
            cmd += shlex.split(c["extra_args"])
        except ValueError:
            pass
        return cmd, False

    def launch_text(self):
        cmd, shell = self.launch_command()
        return cmd if shell else shlex.join(cmd)

    def model_alive(self):
        return self.proc is not None and self.proc.poll() is None

    def start_model(self):
        if self.model_alive():
            self.emit("modellog", "[already running]\n")
            return
        cmd, shell = self.launch_command()
        if not shell and not self.cfg["model_path"]:
            self.emit("modellog", "[set a model file first, or a custom launch command]\n")
            return
        self.emit("modellog", "$ %s\n" % self.launch_text())
        try:
            self.proc = subprocess.Popen(cmd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         text=True, errors="replace", bufsize=1, start_new_session=(os.name == "posix"))
        except Exception as e:
            self.emit("modellog", "[could not start: %s]\n" % e)
            return
        threading.Thread(target=self._pump_model, args=(self.proc,), daemon=True).start()
        self.emit("modelstate")

    def _pump_model(self, p):
        for line in p.stdout:
            self.emit("modellog", line)
        p.wait()
        self.emit("modellog", "[model exited with code %s]\n" % p.returncode)
        self.emit("modelstate")

    def stop_model(self, wait=False):
        p = self.proc
        if p is None or p.poll() is not None:
            return
        def reap():
            try:
                os.killpg(p.pid, signal.SIGTERM) if os.name == "posix" else p.terminate()
            except Exception:
                pass
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(p.pid, signal.SIGKILL) if os.name == "posix" else p.kill()
                except Exception:
                    pass
        t = threading.Thread(target=reap, daemon=True)
        t.start()
        if wait:
            t.join(12)


# ═════════════════════════ GUI ═════════════════════════

# ═════════════════════════ colour themes (one dict per theme, every field is named) ═════════════════════════

THEMES = {
    "midnight": dict(bg="#14161f", panel="#232738", field="#0e1019", border="#343a55", fg="#e6e9f5", muted="#8a91b4",
                     accent="#7c5cff", accent_hi="#9479ff", accent_fg="#ffffff", accent2="#2ee6d6", hover="#303656",
                     good="#3ddc97", bad="#ff5d73", bad_hi="#ff8597", warn="#ffc857", select="#3b3f66",
                     row_run="#3a3320", row_done="#173a2c", row_fail="#43202b"),
    "graphite": dict(bg="#1c1c1e", panel="#2a2a2d", field="#141416", border="#3d3d42", fg="#ececec", muted="#9a9aa0",
                     accent="#ff8a3d", accent_hi="#ffa05e", accent_fg="#1a1206", accent2="#4fc3f7", hover="#3a3a40",
                     good="#6bdc8a", bad="#ff6b6b", bad_hi="#ff8f8f", warn="#ffd166", select="#4a3a2a",
                     row_run="#3b3420", row_done="#1d3a26", row_fail="#432324"),
    "light": dict(bg="#f4f5fb", panel="#e4e7f5", field="#ffffff", border="#c5cae0", fg="#1d2033", muted="#69708f",
                  accent="#5b3df5", accent_hi="#7458ff", accent_fg="#ffffff", accent2="#0a8f84", hover="#d5daf0",
                  good="#1a7f4b", bad="#c62a47", bad_hi="#dd4a65", warn="#a86a00", select="#cfd6ff",
                  row_run="#fff3b0", row_done="#c9f7cf", row_fail="#ffd0d0"),
    "nord": dict(bg="#1b2028", panel="#262d38", field="#141821", border="#3b4557", fg="#e3e9f2", muted="#8fa0b8",
                 accent="#7fc4e8", accent_hi="#a8dcf2", accent_fg="#10161d", accent2="#e0a86a", hover="#333d4d",
                 good="#4fc08a", bad="#e8737f", bad_hi="#f79aa4", warn="#f2c566", select="#38485e",
                 row_run="#3b3422", row_done="#1d3527", row_fail="#46222a"),
    "forest": dict(bg="#141a15", panel="#1f2a21", field="#0e140f", border="#31422f", fg="#e2ead8", muted="#8ba38c",
                   accent="#8ab54e", accent_hi="#add27c", accent_fg="#141d0f", accent2="#d8b464", hover="#2a382a",
                   good="#5fbf7a", bad="#e8686a", bad_hi="#f58f90", warn="#f0cd5e", select="#34492f",
                   row_run="#33301d", row_done="#1c3622", row_fail="#3d2323"),
    "paper": dict(bg="#f6f2ea", panel="#eae3d7", field="#fffdf8", border="#cfc4b4", fg="#2b251c", muted="#6f6656",
                  accent="#b3543c", accent_hi="#cb6f52", accent_fg="#ffffff", accent2="#2f6b5e", hover="#ddd4c3",
                  good="#1e7a4a", bad="#b23246", bad_hi="#cc5060", warn="#9a6b00", select="#e6dcc8",
                  row_run="#f8e9bf", row_done="#d8edda", row_fail="#f7d9d6"),
}
THEME = THEMES["midnight"]  # replaced by App.apply_theme

class Tip:
    def __init__(self, widget, text):
        self.w, self.text, self.tw = widget, text, None
        widget.bind("<Enter>", self.show, add="+")
        widget.bind("<Leave>", self.hide, add="+")

    def show(self, _e=None):
        if self.tw or not self.text:
            return
        self.tw = tk.Toplevel(self.w)
        self.tw.wm_overrideredirect(True)
        self.tw.wm_geometry("+%d+%d" % (self.w.winfo_rootx() + 20, self.w.winfo_rooty() + self.w.winfo_height() + 4))
        tk.Label(self.tw, text=self.text, justify="left", background=THEME["panel"], foreground=THEME["fg"], highlightthickness=1, highlightbackground=THEME["accent"], padx=8, pady=5, wraplength=460).pack()

    def hide(self, _e=None):
        if self.tw:
            self.tw.destroy()
            self.tw = None


def scroll_frame(parent):
    outer = ttk.Frame(parent)
    canvas = tk.Canvas(outer, highlightthickness=0)
    sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas)
    win = canvas.create_window((0, 0), window=inner, anchor="nw")
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
    canvas.configure(yscrollcommand=sb.set)
    canvas.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    def wheel(e):
        canvas.yview_scroll(-1 if (e.num == 4 or e.delta > 0) else 1, "units")
    def enter(_e):
        for ev in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            canvas.bind_all(ev, wheel)
    def leave(_e):
        for ev in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            canvas.unbind_all(ev)
    outer.bind("<Enter>", enter)
    outer.bind("<Leave>", leave)
    return outer, inner


def make_log(parent, height=12):
    box = ttk.Frame(parent)
    t = tk.Text(box, height=height, wrap="word", font="TkFixedFont")
    sb = ttk.Scrollbar(box, orient="vertical", command=t.yview)
    t.configure(yscrollcommand=sb.set)
    t.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return box, t


class App:
    def __init__(self, root):
        self.root, self.eng = root, Engine()
        self.save_job = None
        root.title("Forge")
        root.geometry("1200x860")
        self.health_state = None
        self.apply_theme()
        head = ttk.Frame(root, padding=(16, 10, 16, 8))
        head.pack(fill="x")
        ttk.Label(head, text="FORGE", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="   local task runner", style="Muted.TLabel").pack(side="left", pady=(12, 0))
        self.run_lbl = ttk.Label(head, text="idle", style="Muted.TLabel")
        self.run_lbl.pack(side="right", padx=(16, 0))
        self.health_lbl = ttk.Label(head)
        self.health_lbl.pack(side="right")
        ttk.Frame(root, style="Accent.TFrame", height=2).pack(fill="x")
        self.show_health()
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=10)
        frames = {n: ttk.Frame(self.nb, padding=8) for n in ("Queue", "Git", "ComfyUI", "Model", "Log", "Tools", "Tool Maker", "Settings")}
        for n, f in frames.items():
            self.nb.add(f, text=n)
        self.log_frame = frames["Log"]
        self.build_log(frames["Log"])
        self.build_queue(frames["Queue"])
        self.comfy = ComfyTab(self, frames["ComfyUI"])
        self.gittab = GitTab(self, frames["Git"])
        self.build_model(frames["Model"])
        self.build_tools(frames["Tools"])
        self.build_maker(frames["Tool Maker"])
        outer, inner = scroll_frame(frames["Settings"])
        outer.pack(fill="both", expand=True)
        inner.columnconfigure(1, weight=1)
        self.build_form(inner, ("Project", "Sampling", "Run limits", "Git", "Appearance", "Prompts", "ComfyUI"))
        self.apply_theme()  # second pass paints the plain tk widgets that exist now
        self.refresh_tree()
        self.refresh_tools()
        self.refresh_cmd()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.pump()

    # ---- theme
    def apply_theme(self):
        global THEME
        t = THEME = self.theme = THEMES.get(self.eng.cfg["theme"], THEMES["midnight"])
        try:
            size = max(7, min(24, int(self.eng.cfg["ui_font_size"])))
        except (TypeError, ValueError):
            size = 10
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkFixedFont"):
            tkfont.nametofont(name).configure(size=size)
        fam = tkfont.nametofont("TkDefaultFont").actual("family")
        self.f_bold = tkfont.Font(family=fam, size=size, weight="bold")
        self.f_title = tkfont.Font(family=fam, size=size + 10, weight="bold")
        st = ttk.Style()
        st.theme_use("clam")
        st.configure(".", background=t["bg"], foreground=t["fg"], fieldbackground=t["field"], bordercolor=t["border"], darkcolor=t["bg"], lightcolor=t["bg"],
                     troughcolor=t["panel"], focuscolor=t["bg"], insertcolor=t["fg"], selectbackground=t["select"], selectforeground=t["fg"])
        st.configure("TFrame", background=t["bg"])
        st.configure("Accent.TFrame", background=t["accent"])
        st.configure("TLabel", background=t["bg"], foreground=t["fg"])
        st.configure("Muted.TLabel", foreground=t["muted"])
        st.configure("Bad.TLabel", foreground=t["bad"])
        st.configure("Title.TLabel", foreground=t["accent"], font=self.f_title)
        st.configure("Section.TLabel", foreground=t["accent2"], font=self.f_bold)
        st.configure("TLabelframe", background=t["bg"], bordercolor=t["border"], relief="solid")
        st.configure("TLabelframe.Label", background=t["bg"], foreground=t["accent2"], font=self.f_bold)
        for name, bg, fg, hi in (("TButton", t["panel"], t["fg"], t["hover"]), ("Accent.TButton", t["accent"], t["accent_fg"], t["accent_hi"]),
                                 ("Danger.TButton", t["bad"], "#ffffff", t["bad_hi"])):
            st.configure(name, background=bg, foreground=fg, bordercolor=bg, darkcolor=bg, lightcolor=bg, padding=(14, 7), borderwidth=1, relief="flat", focusthickness=0)
            if name != "TButton":
                st.configure(name, font=self.f_bold)
            st.map(name, background=[("disabled", t["panel"]), ("pressed", hi), ("active", hi)], foreground=[("disabled", t["muted"])],
                   bordercolor=[("disabled", t["panel"]), ("active", hi)], darkcolor=[("active", hi)], lightcolor=[("active", hi)])
        st.configure("TNotebook", background=t["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background=t["panel"], foreground=t["muted"], padding=(22, 9), borderwidth=0, font=self.f_bold)
        st.map("TNotebook.Tab", background=[("selected", t["bg"]), ("active", t["hover"])], foreground=[("selected", t["accent"]), ("active", t["fg"])],
               lightcolor=[("selected", t["accent"])], bordercolor=[("selected", t["accent"])])
        st.configure("TEntry", fieldbackground=t["field"], foreground=t["fg"], bordercolor=t["border"], padding=5, insertcolor=t["accent"])
        st.map("TEntry", bordercolor=[("focus", t["accent"])], lightcolor=[("focus", t["accent"])], darkcolor=[("focus", t["accent"])],
               fieldbackground=[("readonly", t["panel"])], foreground=[("readonly", t["muted"])])
        st.configure("TCombobox", fieldbackground=t["field"], background=t["panel"], foreground=t["fg"], arrowcolor=t["accent"], bordercolor=t["border"], padding=4)
        st.map("TCombobox", fieldbackground=[("readonly", t["field"])], foreground=[("readonly", t["fg"])], selectbackground=[("readonly", t["field"])],
               selectforeground=[("readonly", t["fg"])], bordercolor=[("focus", t["accent"])])
        self.root.option_add("*TCombobox*Listbox.background", t["field"])
        self.root.option_add("*TCombobox*Listbox.foreground", t["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", t["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", t["accent_fg"])
        st.configure("TCheckbutton", background=t["bg"], foreground=t["fg"], indicatorbackground=t["field"], indicatorforeground=t["accent_fg"], indicatormargin=(0, 0, 6, 0))
        st.map("TCheckbutton", background=[("active", t["bg"])], indicatorbackground=[("selected", t["accent"]), ("active", t["hover"])], foreground=[("disabled", t["muted"])])
        st.configure("Treeview", background=t["field"], fieldbackground=t["field"], foreground=t["fg"], rowheight=int(size * 2.6), borderwidth=0, bordercolor=t["border"])
        st.map("Treeview", background=[("selected", t["select"])], foreground=[("selected", t["fg"])])
        st.configure("Treeview.Heading", background=t["panel"], foreground=t["accent2"], relief="flat", padding=(8, 6), borderwidth=0, font=self.f_bold)
        st.map("Treeview.Heading", background=[("active", t["hover"])])
        st.configure("TScrollbar", background=t["panel"], troughcolor=t["bg"], bordercolor=t["bg"], darkcolor=t["panel"], lightcolor=t["panel"], arrowcolor=t["muted"], gripcount=0, relief="flat")
        st.map("TScrollbar", background=[("active", t["hover"])])
        self.root.configure(background=t["bg"])
        self.paint(self.root)
        if hasattr(self, "tree"):
            for state, key in (("Running", "row_run"), ("Loop", "row_run"), ("Done", "row_done"), ("Failed", "row_fail")):
                self.tree.tag_configure(state, background=t[key], foreground=t["fg"])
        for w in list(getattr(self, "log_widgets", {}).values()):
            if w is not None:
                for tag, col in (("task", t["accent"]), ("cycle", t["warn"]), ("tool", t["accent2"]), ("think", t["muted"]), ("model", t["fg"]),
                                 ("hook", t["accent_hi"]), ("verify", t["warn"]), ("good", t["good"]), ("bad", t["bad"]), ("cmd", t["accent"])):
                    w.tag_configure(tag, foreground=col)
                w.tag_configure("task", font=self.f_bold, spacing1=10)
        if hasattr(self, "health_lbl"):
            self.show_health()
        if getattr(self, "comfy", None) is not None:
            self.comfy.restyle()
        if getattr(self, "gittab", None) is not None:
            self.gittab.restyle()

    def paint(self, w):
        t = self.theme
        if isinstance(w, tk.Text):
            w.configure(background=t["field"], foreground=t["fg"], insertbackground=t["accent"], selectbackground=t["select"], selectforeground=t["fg"],
                        inactiveselectbackground=t["select"], relief="flat", borderwidth=0, highlightthickness=1, highlightbackground=t["border"],
                        highlightcolor=t["accent"], padx=8, pady=6)
        elif isinstance(w, tk.Canvas):
            w.configure(background=t["bg"])
        for c in w.winfo_children():
            self.paint(c)

    def show_health(self):
        h, t = self.health_state, self.theme
        self.health_lbl.configure(text="\u25cf  Model server ready" if h else "\u25cb  Model server offline / loading" if h is False else "\u25cb  checking model server...",
                                  foreground=t["good"] if h else t["bad"] if h is False else t["muted"])

    # ---- settings forms generated from SETTINGS
    def set_cfg(self, key, value):
        self.eng.cfg[key] = value
        if self.save_job:
            self.root.after_cancel(self.save_job)
        self.save_job = self.root.after(500, self.eng.save)
        self.refresh_cmd()
        if key in ("theme", "ui_font_size"):
            self.apply_theme()

    def entry_changed(self, key, kind, s):
        try:
            value = int(float(s)) if kind == "int" else float(s) if kind == "float" else s
        except ValueError:
            return  # half typed number: keep the old value until it parses
        self.set_cfg(key, value)

    def build_form(self, parent, sections):
        row, show = 0, False
        parent.columnconfigure(1, weight=1)
        for item in SETTINGS:
            if item[0] == "#":
                show = item[1] in sections
                if show:
                    ttk.Label(parent, text=item[1].upper(), style="Section.TLabel").grid(row=row, column=0, columnspan=3, sticky="w", pady=(18, 4))
                    row += 1
                continue
            if not show:
                continue
            key, label, kind, _default, helptext = item
            val = self.eng.cfg[key]
            lab = ttk.Label(parent, text=label)
            lab.grid(row=row, column=0, sticky="nw", padx=(0, 10), pady=2)
            Tip(lab, helptext)
            if kind == "bool":
                v = tk.BooleanVar(value=bool(val))
                w = ttk.Checkbutton(parent, variable=v, command=lambda k=key, v=v: self.set_cfg(k, v.get()))
                w.grid(row=row, column=1, sticky="w")
            elif kind.startswith("choice:"):
                v = tk.StringVar(value=str(val))
                w = ttk.Combobox(parent, textvariable=v, values=kind[7:].split("|"), state="readonly")
                w.bind("<<ComboboxSelected>>", lambda e, k=key, v=v: self.set_cfg(k, v.get()))
                w.grid(row=row, column=1, sticky="ew")
            elif kind == "text":
                w = tk.Text(parent, height=9, wrap="word")
                w.insert("1.0", str(val))
                w.bind("<KeyRelease>", lambda e, k=key, w=w: self.set_cfg(k, w.get("1.0", "end-1c")))
                w.grid(row=row, column=1, columnspan=2, sticky="ew", pady=2)
            else:
                v = tk.StringVar(value=str(val))
                v.trace_add("write", lambda *_a, k=key, kd=kind, v=v: self.entry_changed(k, kd, v.get()))
                w = ttk.Entry(parent, textvariable=v)
                w.grid(row=row, column=1, sticky="ew")
                if kind in ("dir", "file"):
                    def browse(v=v, kd=kind):
                        p = filedialog.askdirectory() if kd == "dir" else filedialog.askopenfilename()
                        if p:
                            v.set(p)
                    ttk.Button(parent, text="...", width=3, command=browse).grid(row=row, column=2, padx=(4, 0))
            Tip(w, helptext)
            row += 1

    # ---- log tab: one section per source, each also saved to forge_logs/<section>.log
    def build_log(self, f):
        LOG_DIR.mkdir(exist_ok=True)
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 6))
        self.log_bar = bar
        ttk.Button(bar, text="Clear this view", command=self.log_clear).pack(side="left")
        ttk.Button(bar, text="Open log folder", command=lambda: _open_path(LOG_DIR)).pack(side="left", padx=6)
        ttk.Button(bar, text="Analyze errors", command=self.errors_analyze).pack(side="left")
        ttk.Label(bar, text="Every section is also saved in forge_logs/ and the last lines come back when Forge starts.", style="Muted.TLabel").pack(side="left", padx=10)
        self.log_nb = ttk.Notebook(f)
        self.log_nb.pack(fill="both", expand=True)
        self.log_widgets, self.log_frames = {}, {}
        for name in LOG_SECTIONS:
            fr = ttk.Frame(self.log_nb, padding=4)
            self.log_nb.add(fr, text=name)
            box, w = make_log(fr, 20)
            box.pack(fill="both", expand=True)
            self.log_widgets[name], self.log_frames[name] = w, fr
            self.load_log_history(name, w)
        self.log_w, self.mlog_w = self.log_widgets["Queue"], self.log_widgets["Model"]

    def load_log_history(self, name, w):
        p = LOG_DIR / (name.lower() + ".log")
        try:
            size = p.stat().st_size
            with open(p, "rb") as fh:
                fh.seek(max(0, size - 60000))
                lines = fh.read().decode("utf-8", "replace").splitlines()
        except OSError:
            return
        if size > 60000:
            lines = lines[1:]  # the first line is probably cut in half
        lines = lines[-300:]
        if lines:
            w.insert("end", "\n".join(lines) + "\n", "think")
            w.insert("end", "---------------- new session ----------------\n", "cycle")

    def log_clear(self):
        name = self.log_nb.tab(self.log_nb.select(), "text")
        if name in self.log_widgets:
            self.log_widgets[name].delete("1.0", "end")

    def errors_analyze(self):
        if self.eng.running:
            messagebox.showinfo("Busy", "Stop the queue first: the analysis uses the same model connection.")
            return
        self.show_log_tab("Diagnosis")
        threading.Thread(target=self.eng.analyze_errors, daemon=True).start()

    def log_to(self, name, text):
        """One line into a log section and its file. Queue and Model lines come through the engine, which writes their files itself."""
        w = self.log_widgets.get(name)
        if w is None:
            return
        self.append(w, str(text).rstrip("\n") + "\n")
        file_log(name, text)

    def show_log_tab(self, name):
        self.nb.select(self.log_frame)
        if name in self.log_frames:
            self.log_nb.select(self.log_frames[name])

    # ---- queue tab
    def build_queue(self, f):
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 6))
        self.btn_start = ttk.Button(bar, text="\u25b6 Start queue", command=self.start_queue, style="Accent.TButton")
        self.btn_start.pack(side="left")
        self.btn_soft = ttk.Button(bar, text="Finish run, then stop", command=self.eng.request_soft_stop, state="disabled")
        self.btn_soft.pack(side="left", padx=6)
        self.btn_stop = ttk.Button(bar, text="\u25a0 Stop now", command=self.eng.request_cancel, state="disabled", style="Danger.TButton")
        self.btn_stop.pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Revert selected task", command=self.revert_selected).pack(side="left")
        self.queue_info = ttk.Label(bar, text="", style="Muted.TLabel")
        self.queue_info.pack(side="right")
        cols = ("n", "state", "loop", "task", "stats", "result")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=9, selectmode="browse")
        for c, w, t in (("n", 36, "#"), ("state", 80, "State"), ("loop", 70, "Loop"), ("task", 480, "Task"), ("stats", 330, "Cost"), ("result", 380, "Result")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, stretch=(c in ("task", "result")))
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.task_selected)
        ttk.Label(f, text="Loop column: runs left for a looping task (-1 = infinite; set it with the Set loop runs button).").pack(anchor="w", pady=(2, 0))
        ttk.Label(f, text="Task text (self-contained: file names, what to change, how it should behave - every task runs in a brand-new context):").pack(anchor="w", pady=(8, 2))
        self.task_text = tk.Text(f, height=5, wrap="word")
        self.task_text.pack(fill="x")
        rbar = ttk.Frame(f)
        rbar.pack(fill="x", pady=(4, 0))
        self.btn_refine = ttk.Button(rbar, text="Refine into tasks", command=self.task_refine, style="Accent.TButton")
        self.btn_refine.pack(side="left")
        self.refine_lbl = ttk.Label(rbar, text="Splits a rough idea in the box above into ordered, self-contained tasks and adds them to the queue.", style="Muted.TLabel")
        self.refine_lbl.pack(side="left", padx=10)
        row = ttk.Frame(f)
        row.pack(fill="x", pady=4)
        for text, cmd in (("Add as new task", self.task_add), ("Save to selected", self.task_save), ("Remove", self.task_remove), ("\u2191", lambda: self.task_move(-1)),
                          ("\u2193", lambda: self.task_move(1)), ("Reset all to Pending", self.task_reset), ("Remove Done", self.task_remove_done),
                          ("Set loop runs on selected", self.loop_set), ("Stop loop on selected", self.loop_stop)):
            ttk.Button(row, text=text, command=cmd, width=3 if len(text) == 1 else None).pack(side="left", padx=(0, 4))
        self.loop_var = tk.StringVar(value="0")
        ttk.Label(row, text="Loop runs for selected task (-1 = infinite):").pack(side="left", padx=(10, 0))
        ttk.Entry(row, textvariable=self.loop_var, width=6).pack(side="left", padx=(4, 0))

    def sel(self):
        s = self.tree.selection()
        return int(s[0]) if s else None

    def task_selected(self, _e=None):
        i = self.sel()
        if i is not None and i < len(self.eng.cfg["queue"]):
            self.task_text.delete("1.0", "end")
            self.task_text.insert("1.0", self.eng.cfg["queue"][i]["prompt"])

    def queue_changed(self, select=None):
        self.eng.save()
        self.refresh_tree(select)

    def task_add(self):
        text = self.task_text.get("1.0", "end-1c").strip()
        if text:
            self.eng.cfg["queue"].append(new_task(text))
            self.task_text.delete("1.0", "end")
            self.queue_changed(len(self.eng.cfg["queue"]) - 1)

    def task_refine(self):
        text = self.task_text.get("1.0", "end-1c").strip()
        if not text:
            return
        if self.eng.running:
            messagebox.showinfo("Busy", "Stop the queue first: Refine uses the same model connection.")
            return
        self.refine_src = text
        self.btn_refine.configure(state="disabled")
        self.refine_lbl.configure(text="Asking the model to split this into tasks (up to 3 attempts)...")
        def work():
            try:
                tasks, msg = self.eng.refine(text)
                self.eng.emit("refine", {"tasks": tasks, "msg": msg})
            except Cancelled:
                self.eng.emit("refine", {"tasks": [], "msg": "Cancelled."})
            except Exception as e:
                self.eng.emit("refine", {"tasks": [], "msg": "Failed: %s" % e})
        threading.Thread(target=work, daemon=True).start()

    def task_save(self):
        i = self.sel()
        if i is not None:
            t = self.eng.cfg["queue"][i]
            t["prompt"] = self.task_text.get("1.0", "end-1c").strip()
            if t["state"] != "Running":
                t["state"], t["result"] = "Pending", ""
            self.queue_changed(i)

    def task_remove(self):
        i = self.sel()
        if i is not None:
            del self.eng.cfg["queue"][i]
            self.queue_changed()

    def task_move(self, d):
        i, q = self.sel(), self.eng.cfg["queue"]
        if i is not None and 0 <= i + d < len(q):
            q[i], q[i + d] = q[i + d], q[i]
            self.queue_changed(i + d)

    def task_reset(self):
        for t in self.eng.cfg["queue"]:
            if t["state"] != "Running":
                t["state"], t["result"] = "Pending", ""
        self.queue_changed()

    def task_remove_done(self):
        # K1: "Remove Done" must leave Loop tasks alone - they still have runs left,
        # and dropping them silently ends an active loop.
        self.eng.cfg["queue"] = [t for t in self.eng.cfg["queue"] if t["state"] != "Done"]
        self.queue_changed()

    def loop_set(self):
        i = self.sel()
        if i is None:
            return
        try:
            runs = int(self.loop_var.get().strip() or 0)
        except ValueError:
            messagebox.showerror("Loop runs", "Enter a whole number in the box (-1 = infinite, 0 = no loop).")
            return
        t = self.eng.cfg["queue"][i]
        if t["state"] == "Running":
            messagebox.showerror("Busy", "The task is running right now - set the loop before or after a run.")
            return
        t["loop_left"] = runs
        if runs != 0 and t["state"] in ("Done", "Failed"):
            t["state"], t["result"] = "Loop", ""
        self.queue_changed(i)

    def loop_stop(self):
        i = self.sel()
        if i is None:
            return
        t = self.eng.cfg["queue"][i]
        if t["state"] == "Running":
            self.eng.stop_loop_after_run(t)  # the run finishes, then only this loop ends
            return
        t["loop_left"] = 0
        if t["state"] == "Loop":
            t["state"], t["result"] = "Pending", ""
        self.queue_changed(i)

    def revert_selected(self):
        i = self.sel()
        if i is not None and messagebox.askyesno("Revert", "Restore every file this task edited to its state before the task?"):
            self.eng.revert_task(self.eng.cfg["queue"][i])

    def refresh_tree(self, select=None):
        keep = self.sel() if select is None else select
        self.tree.delete(*self.tree.get_children())
        q = self.eng.cfg["queue"]
        for i, t in enumerate(q):
            self.tree.insert("", "end", iid=str(i), values=(i + 1, t["state"], loop_text(t), clip(t["prompt"].replace("\n", " "), 110), stats_text(t.get("stats")), clip(t.get("result", "").replace("\n", " "), 90)), tags=(t["state"],))
        if keep is not None and keep < len(q):
            self.tree.selection_set(str(keep))
        counts = {s: sum(1 for t in q if t["state"] == s) for s in ("Pending", "Loop", "Running", "Done", "Failed")}
        self.queue_info.configure(text="   ".join("%d %s" % (v, k.lower()) for k, v in counts.items()))

    def start_queue(self):
        if not Path(self.eng.cfg["project_root"]).is_dir():
            messagebox.showerror("Project folder", "Set an existing Project folder in Settings first.")
            return
        self.eng.start_queue()

    # ---- model tab
    def build_model(self, f):
        form = ttk.Frame(f)
        form.pack(fill="x")
        self.build_form(form, ("Server", "Model launch"))
        self.cmd_var = tk.StringVar()
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(10, 2))
        ttk.Label(row, text="Launch command:").pack(side="left")
        ttk.Entry(row, textvariable=self.cmd_var, state="readonly").pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="Copy", command=lambda: (self.root.clipboard_clear(), self.root.clipboard_append(self.cmd_var.get()))).pack(side="left")
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="\u25b6 Start model", command=self.eng.start_model, style="Accent.TButton").pack(side="left")
        ttk.Button(bar, text="\u25a0 Stop model", command=self.eng.stop_model, style="Danger.TButton").pack(side="left", padx=6)
        self.model_lbl = ttk.Label(bar, text="not started by Forge", style="Muted.TLabel")
        self.model_lbl.pack(side="left", padx=10)
        ttk.Button(f, text="Show the model's output (Log tab)", command=lambda: self.show_log_tab("Model")).pack(anchor="w", pady=(6, 0))

    def refresh_cmd(self):
        if hasattr(self, "cmd_var"):
            self.cmd_var.set(self.eng.launch_text())

    # ---- tools tab
    def build_tools(self, f):
        cols = ("kind", "on", "name", "source", "desc")
        self.ttree = ttk.Treeview(f, columns=cols, show="headings", height=13, selectmode="browse")
        for c, w, t in (("kind", 150, "Kind"), ("on", 60, "On"), ("name", 190, "Name"), ("source", 170, "From"), ("desc", 560, "Description / command")):
            self.ttree.heading(c, text=t)
            self.ttree.column(c, width=w, stretch=(c == "desc"))
        self.ttree.pack(fill="both", expand=True)
        self.ttree.bind("<Button-1>", self.tool_click)
        self.ttree.bind("<<TreeviewSelect>>", self.tool_selected)
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="Reload plugins", command=self.reload_plugins).pack(side="left")
        ttk.Button(bar, text="Open plugin folder", command=self.open_plugins).pack(side="left", padx=6)
        ttk.Label(bar, text="Click the box in the On column to switch a tool or hook. Off = the model never sees it.", style="Muted.TLabel").pack(side="left", padx=10)
        self.plug_err = ttk.Label(f, text="", style="Bad.TLabel", wraplength=1050, justify="left")
        self.plug_err.pack(fill="x")
        ed = ttk.LabelFrame(f, text="Command hooks (run after each fix cycle on the files the model edited; {files} = all matching files, {file} = once per file)", padding=6)
        ed.pack(fill="x", pady=(6, 0))
        ed.columnconfigure(1, weight=1)
        self.ch_name, self.ch_glob, self.ch_cmd, self.ch_block = tk.StringVar(), tk.StringVar(value="*.py"), tk.StringVar(), tk.BooleanVar()
        for r, (lab, var) in enumerate((("Name", self.ch_name), ("File glob(s)", self.ch_glob), ("Command", self.ch_cmd))):
            ttk.Label(ed, text=lab).grid(row=r, column=0, sticky="w", padx=(0, 8), pady=1)
            ttk.Entry(ed, textvariable=var).grid(row=r, column=1, sticky="ew", pady=1)
        ttk.Checkbutton(ed, text="Blocking: a non-zero exit sends the output back to the model as a problem", variable=self.ch_block).grid(row=3, column=1, sticky="w")
        bt = ttk.Frame(ed)
        bt.grid(row=4, column=1, sticky="w", pady=(4, 0))
        for text, cmd in (("Add command hook", self.cmdhook_add), ("Update selected", self.cmdhook_update), ("Delete selected", self.cmdhook_delete)):
            ttk.Button(bt, text=text, command=cmd).pack(side="left", padx=(0, 4))

    def refresh_tools(self):
        keep = self.ttree.selection()
        self.ttree.delete(*self.ttree.get_children())
        for r in sorted(self.eng.listing(), key=lambda r: (r["kind"].startswith("hook"), r["name"])):
            self.ttree.insert("", "end", iid=r["iid"], values=(r["kind"], "\u25cf on" if r["on"] else "\u25cb off", r["name"], r["source"], clip(r["desc"], 140)))
        if keep and self.ttree.exists(keep[0]):
            self.ttree.selection_set(keep[0])
        self.plug_err.configure(text="\n".join("Plugin error - " + e for e in self.eng.plugin_errors))

    def tool_click(self, e):
        if self.ttree.identify_region(e.x, e.y) == "cell" and self.ttree.identify_column(e.x) == "#2":
            iid = self.ttree.identify_row(e.y)
            if iid:
                self.eng.toggle(iid)
                self.refresh_tools()

    def tool_selected(self, _e=None):
        s = self.ttree.selection()
        if s and s[0].startswith("cmd:"):
            h = self.eng.cfg["command_hooks"][int(s[0][4:])]
            self.ch_name.set(h["name"]); self.ch_glob.set(h.get("glob", "*")); self.ch_cmd.set(h["command"]); self.ch_block.set(bool(h.get("blocking")))

    def selected_cmd(self):
        s = self.ttree.selection()
        return int(s[0][4:]) if s and s[0].startswith("cmd:") else None

    def cmdhook_form(self, enabled):
        return {"name": self.ch_name.get().strip() or "command", "enabled": enabled, "glob": self.ch_glob.get().strip() or "*", "command": self.ch_cmd.get().strip(), "blocking": self.ch_block.get()}

    def cmdhook_add(self):
        if self.ch_cmd.get().strip():
            self.eng.cfg["command_hooks"].append(self.cmdhook_form(True))
            self.eng.save()
            self.refresh_tools()

    def cmdhook_update(self):
        i = self.selected_cmd()
        if i is not None:
            self.eng.cfg["command_hooks"][i] = self.cmdhook_form(bool(self.eng.cfg["command_hooks"][i].get("enabled")))
            self.eng.save()
            self.refresh_tools()

    def cmdhook_delete(self):
        i = self.selected_cmd()
        if i is not None:
            del self.eng.cfg["command_hooks"][i]
            self.eng.save()
            self.refresh_tools()

    def reload_plugins(self):
        self.eng.load_plugins()
        self.refresh_tools()

    def open_plugins(self):
        try:
            os.startfile(str(PLUGIN_DIR)) if os.name == "nt" else subprocess.Popen(["xdg-open", str(PLUGIN_DIR)])
        except Exception as e:
            messagebox.showinfo("Plugin folder", "%s\n\n(%s)" % (PLUGIN_DIR, e))

    # ---- tool maker tab
    def build_maker(self, f):
        ttk.Label(f, text="Describe the tool or hook you want (what it does, what it takes in, when the model should use it):").pack(anchor="w")
        self.maker_req = tk.Text(f, height=5, wrap="word")
        self.maker_req.pack(fill="x", pady=(2, 4))
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        self.btn_gen = ttk.Button(bar, text="Generate with the model", command=self.maker_generate, style="Accent.TButton")
        self.btn_gen.pack(side="left")
        ttk.Button(bar, text="Copy the spec prompt", command=self.maker_copy_spec).pack(side="left", padx=6)
        ttk.Label(bar, text="(the spec prompt can be pasted into any other chat, then paste the reply below)", style="Muted.TLabel").pack(side="left")
        self.maker_status = ttk.Label(f, text="", wraplength=1100, justify="left")
        self.maker_status.pack(fill="x", pady=4)
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Plugin file name:").pack(side="left")
        self.fn_var = tk.StringVar()
        ttk.Entry(row, textvariable=self.fn_var, width=30).pack(side="left", padx=6)
        ttk.Label(row, text=".py   in forge_plugins/").pack(side="left")
        box, self.code_w = make_log(f, 20)
        self.code_w.configure(wrap="none")
        box.pack(fill="both", expand=True, pady=4)
        bt = ttk.Frame(f)
        bt.pack(fill="x")
        ttk.Button(bt, text="Check code", command=self.maker_check).pack(side="left")
        ttk.Button(bt, text="Save && enable", command=self.maker_save, style="Accent.TButton").pack(side="left", padx=6)
        ttk.Label(bt, text="Plugins run with your user rights: read the code before you save it.", style="Muted.TLabel").pack(side="left", padx=10)

    def maker_generate(self):
        req = self.maker_req.get("1.0", "end-1c").strip()
        if not req:
            return
        if self.eng.running:
            messagebox.showinfo("Busy", "Stop the queue first: the Tool Maker uses the same model connection.")
            return
        self.btn_gen.configure(state="disabled")
        self.maker_status.configure(text="Asking the model (up to 3 attempts)...")
        def work():
            try:
                code, msg = self.eng.make_plugin(req)
                self.eng.emit("maker", {"code": code, "msg": msg})
            except Cancelled:
                self.eng.emit("maker", {"code": None, "msg": "Cancelled."})
            except Exception as e:
                self.eng.emit("maker", {"code": None, "msg": "Failed: %s" % e})
        threading.Thread(target=work, daemon=True).start()

    def maker_copy_spec(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.eng.spec_prompt() + "\n\nMY REQUEST:\n" + self.maker_req.get("1.0", "end-1c").strip())
        self.maker_status.configure(text="Spec prompt and your request copied to the clipboard.")

    def maker_check(self):
        src = self.code_w.get("1.0", "end-1c")
        err = check_plugin_source(src)
        self.maker_status.configure(text=("Check failed: " + err) if err else "Static check passed (nothing was executed). Save runs a full load test first.")

    def maker_save(self):
        src = self.code_w.get("1.0", "end-1c")
        name = re.sub(r"[^a-z0-9_]", "_", self.fn_var.get().strip().lower().removesuffix(".py")).strip("_")
        if not name:
            self.maker_status.configure(text="Give the file a name first.")
            return
        err = check_plugin_source(src)
        label = name + ".py"
        if not err:
            taken = {t["name"] for t in BUILTIN_TOOLS} | {t["name"] for t in self.eng.plugin_tools if t["source"] != label}
            _t, _h, err = load_plugin_source(src, label, taken)  # full load test, only after the user pressed Save
        if err:
            self.maker_status.configure(text="Not saved: " + err)
            return
        path = PLUGIN_DIR / label
        if path.exists() and not messagebox.askyesno("Overwrite", "%s already exists. Overwrite it?" % label):
            return
        PLUGIN_DIR.mkdir(exist_ok=True)
        path.write_text(src, "utf-8")
        self.eng.load_plugins()
        self.refresh_tools()
        self.maker_status.configure(text="Saved %s and loaded it. It is switched on in the Tools tab." % label)

    # ---- event pump and shutdown
    LOG_TAGS = (("#####", "task"), ("== ", "cycle"), ("[tool]", "tool"), ("[thinking]", "think"), ("[model]", "model"), ("[hook", "hook"),
                ("[verify]", "verify"), ("[stats]", "good"), ("[git]", "hook"), ("DONE", "good"), ("Edited:", "good"), ("FAILED", "bad"), ("STOPPED", "bad"), ("Cancel", "bad"), ("Queue", "cycle"))

    def append(self, w, s):
        tag = None
        if w is self.log_w:
            tag = next((tg for pre, tg in self.LOG_TAGS if s.startswith(pre)), None)
        elif w is self.mlog_w:
            tag = "cmd" if s.startswith("$ ") else "cycle" if s.startswith("[") else "bad" if "error" in s.lower() else None
        if tag:
            w.insert("end", s, tag)
        else:
            w.insert("end", s)
        w.see("end")
        if int(w.index("end-1c").split(".")[0]) > 4000:
            w.delete("1.0", "1000.0")

    def pump(self):
        try:
            while True:
                kind, p = self.eng.events.get_nowait()
                if kind == "log":
                    self.append(self.log_w, p + "\n")
                    if self.eng.running and p.startswith(("[turn", "[tool]", "== ", "#####", "[thinking]")):
                        self.run_lbl.configure(text=clip(p, 70))
                elif kind == "modellog":
                    self.append(self.mlog_w, p)
                elif kind == "queue":
                    self.refresh_tree()
                    self.gittab.refresh_soon()
                elif kind == "running":
                    self.btn_start.configure(state="disabled" if p else "normal")
                    self.btn_soft.configure(state="normal" if p else "disabled", text="Finish run, then stop")
                    self.btn_stop.configure(state="normal" if p else "disabled")
                    self.run_lbl.configure(text="working..." if p else "idle")
                elif kind == "errlog":
                    self.append(self.log_widgets["Errors"], p + "\n")
                elif kind == "diag":
                    self.append(self.log_widgets["Diagnosis"], p)
                elif kind == "softstop":
                    self.btn_soft.configure(state="disabled", text="Stopping after this run...")
                    self.run_lbl.configure(text="stopping after this run...")
                elif kind == "health":
                    self.health_state = p
                    self.show_health()
                elif kind == "modelstate":
                    self.model_lbl.configure(text="running (started by Forge)" if self.eng.model_alive() else "stopped")
                elif kind == "refine":
                    self.btn_refine.configure(state="normal")
                    self.refine_lbl.configure(text=p["msg"])
                    if p["tasks"]:
                        q = self.eng.cfg["queue"]
                        at = 0 if self.eng.cfg["refine_insert"] == "top" else len(q)
                        q[at:at] = [new_task(t) for t in p["tasks"]]
                        if self.task_text.get("1.0", "end-1c").strip() == getattr(self, "refine_src", None):
                            self.task_text.delete("1.0", "end")  # only when the box still holds what was refined
                        self.tree.selection_set(())  # no selection, so the box is not refilled with some task
                        self.queue_changed()
                        self.eng.log("Refine: added %d task(s) at the %s of the queue." % (len(p["tasks"]), "top" if at == 0 else "end"))
                elif kind == "maker":
                    self.btn_gen.configure(state="normal")
                    self.maker_status.configure(text=p["msg"])
                    if p["code"]:
                        self.code_w.delete("1.0", "end")
                        self.code_w.insert("1.0", p["code"])
                        m = re.search(r"""["']name["']\s*:\s*["']([a-z][a-z0-9_]*)["']""", p["code"])
                        if m and not self.fn_var.get().strip():
                            self.fn_var.set(m.group(1))
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def on_close(self):
        try:
            self.comfy.shutdown()
        except Exception:
            pass
        self.eng.cancel = True
        self.eng.save()
        if self.eng.cfg["stop_model_on_exit"]:
            self.eng.stop_model(wait=True)
        self.root.destroy()


# <<< COMFYUI BEGIN >>>
# ═════════════════════════ ComfyUI tab (added by patch_comfyui.py - running the patch again replaces this whole block) ═════════════════════════
import base64, hashlib, math, mimetypes, random, ssl, webbrowser
from urllib.parse import urlencode
try:
    from PIL import Image, ImageDraw, ImageOps, ImageTk
except ImportError:
    Image = ImageDraw = ImageOps = ImageTk = None

COMFY_STATE_PATH = HERE / "forge_comfy.json"
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")
VID_EXT = (".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v")
AUD_EXT = (".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus")
SEARCH_SKIP = {"node_modules", "__pycache__", "site-packages", "venv", ".venv", "proc", "sys", "dev", "lost+found"}


def comfy_kind(path):
    e = os.path.splitext(str(path))[1].lower()
    return "image" if e in IMG_EXT else "video" if e in VID_EXT else "audio" if e in AUD_EXT else None


def human_size(n):
    for u in ("B", "kB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return ("%d %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0


# ---------- workflow lookup ----------
WF_FUZZY = {"edit": (("*edit*.json",), ("video",)),
            "generate": (("*generat*.json", "*txt2img*.json", "*text*image*.json"), ("video", "edit")),
            "video": (("*video*.json",), ())}


def comfy_find_workflow(cfg, mode):
    """The file named in Settings (inside the workflow folder or as a full path), else a fuzzy match by file name."""
    name = str(cfg.get("comfy_wf_" + mode, "")).strip()
    d = Path(str(cfg.get("comfy_wf_dir", ""))).expanduser()
    if name:
        p = Path(name).expanduser()
        for c in ((p,) if p.is_absolute() else (d / p, HERE / p)):
            if c.is_file():
                return c
    pats, bad = WF_FUZZY[mode]
    for folder in (d, HERE):
        if not folder.is_dir():
            continue
        for pat in pats:
            for f in sorted(folder.glob(pat)):
                if not any(b in f.name.lower() for b in bad):
                    return f
    return None


# ---------- workflow JSON (editor format, subgraphs included) -> API prompt ----------
_WF_SKIP_TYPES = {"MarkdownNote", "Note", "PrimitiveNode"}
_WF_SKIP_KEYS = {"control_after_generate", "control_before_generate", "upload"}
_WF_CONTROL_WORDS = {"fixed", "randomize", "increment", "decrement"}


class _Const:
    def __init__(self, v):
        self.v = v


def comfy_to_api(wf, info=None):
    """Turns a workflow saved from the ComfyUI editor into the API prompt format. Subgraphs are flattened
    (inner node ids look like 459:451), muted nodes are dropped, bypassed nodes pass their input through.
    Widget values come from widgets_values_named. When info (/object_info) is given, widget keys the node does
    not declare are left out. An API-format file is returned unchanged."""
    if not isinstance(wf, dict):
        raise ValueError("the workflow file is not a JSON object")
    if "nodes" not in wf:
        if wf and all(isinstance(v, dict) and "class_type" in v for v in wf.values()):
            return wf
        raise ValueError("this JSON is not a ComfyUI workflow (no 'nodes' list)")
    subs = {s["id"]: s for s in (wf.get("definitions") or {}).get("subgraphs", [])}
    api, seen = {}, {"widgets": False, "named": False}

    def links_of(graph):
        out = {}
        for l in graph.get("links") or []:
            if isinstance(l, dict):
                out[l["id"]] = (l["origin_id"], l["origin_slot"])
            elif isinstance(l, (list, tuple)) and len(l) >= 3:
                out[l[0]] = (l[1], l[2])
        return out

    def scope(graph, prefix, ext):
        return {"graph": graph, "nodes": {n["id"]: n for n in graph.get("nodes", [])}, "links": links_of(graph),
                "prefix": prefix, "ext": ext, "kids": {}}

    def src(sc, link_id):
        o = sc["links"].get(link_id)
        return resolve(sc, o[0], o[1]) if o else None

    def child(sc, n):
        k = sc["kids"].get(n["id"])
        if k is None:
            ext = {}
            for inp in n.get("inputs") or []:
                if inp.get("link") is not None:
                    r = src(sc, inp["link"])
                    if r is not None:
                        ext[inp["name"]] = r
            for key, val in (n.get("widgets_values_named") or {}).items():
                ext.setdefault(key, _Const(val))
            k = scope(subs[n["type"]], sc["prefix"] + str(n["id"]) + ":", ext)
            sc["kids"][n["id"]] = k
        return k

    def resolve(sc, nid, slot, depth=0):
        if depth > 50:
            return None
        if nid == -10:  # the inside of a subgraph asking for one of its own inputs
            names = [i.get("name") for i in sc["graph"].get("inputs") or []]
            return sc["ext"].get(names[slot]) if 0 <= slot < len(names) else None
        n = sc["nodes"].get(nid)
        if n is None or n.get("mode", 0) == 2:
            return None
        t = n.get("type")
        if t in subs:
            kid = child(sc, n)
            outs = kid["graph"].get("outputs") or []
            if slot >= len(outs) or not outs[slot].get("linkIds"):
                return None
            o = kid["links"].get(outs[slot]["linkIds"][0])
            return resolve(kid, o[0], o[1], depth + 1) if o else None
        if n.get("mode", 0) == 4 or t == "Reroute":
            want = ((n.get("outputs") or [])[slot:slot + 1] or [{}])[0].get("type")
            ins = [i for i in n.get("inputs") or [] if i.get("link") is not None]
            pick = next((i for i in ins if i.get("type") == want), ins[0] if ins else None)
            return src(sc, pick["link"]) if pick else None
        return [sc["prefix"] + str(nid), slot]

    def emit(sc):
        for nid, n in sc["nodes"].items():
            t, mode = n.get("type"), n.get("mode", 0)
            if t in _WF_SKIP_TYPES or t == "Reroute" or mode in (2, 4):
                continue
            if t in subs:
                emit(child(sc, n))
                continue
            ins = {}
            for inp in n.get("inputs") or []:
                if inp.get("link") is not None:
                    r = src(sc, inp["link"])
                    if r is not None:
                        ins[inp["name"]] = r.v if isinstance(r, _Const) else r
            named = n.get("widgets_values_named") or {}
            seen["widgets"] = seen["widgets"] or bool(n.get("widgets_values"))
            seen["named"] = seen["named"] or bool(named)
            decl = None
            if info and t in info:
                spec = info[t].get("input") or {}
                decl = set(spec.get("required") or {}) | set(spec.get("optional") or {})
            for k, v in named.items():
                if k in ins or k in _WF_SKIP_KEYS or (isinstance(v, str) and k == v and k in _WF_CONTROL_WORDS):
                    continue
                if decl is not None and "." not in k and k not in decl:
                    continue
                ins[k] = v
            api[sc["prefix"] + str(nid)] = {"class_type": t, "inputs": ins, "_meta": {"title": n.get("title") or t}}

    emit(scope(wf, "", {}))
    if not api:
        raise ValueError("the workflow has no runnable nodes")
    if seen["widgets"] and not seen["named"]:
        raise ValueError("this workflow was saved without widgets_values_named - open it in a current ComfyUI and save it again")
    return api


def _find(api, cls, title=None):
    return [i for i, n in api.items() if n["class_type"] == cls and (not title or title.lower() in n["_meta"]["title"].lower())]


def _put(api, cls, key, val, title=None):
    ids = _find(api, cls, title)
    for i in ids:
        api[i]["inputs"][key] = val
    return len(ids)


def _get(api, cls, key, title=None):
    for i in _find(api, cls, title):
        v = api[i]["inputs"].get(key)
        if v is not None and not isinstance(v, list):
            return v
    return None


def _drop(api, classes):
    gone = {i for i, n in api.items() if n["class_type"] in classes}
    for i in gone:
        del api[i]
    for n in api.values():
        for k in [k for k, v in n["inputs"].items() if isinstance(v, list) and len(v) == 2 and v[0] in gone]:
            del n["inputs"][k]


def _cast(kind, v):
    try:
        if kind in ("int", "combo_i"):
            return int(float(v))
        if kind in ("float", "combo_f"):
            return float(v)
        if kind == "bool":
            return bool(v)
    except (TypeError, ValueError):
        return None
    return str(v)


# ---------- the three pages: what they are, which workflow nodes their controls drive ----------
_KS, _TE, _RS = "KSampler", "TextEncodeQwenImage21", "ResolutionSelector"
_SAMPLERS = ["euler", "euler_ancestral", "heun", "dpmpp_2m", "dpmpp_2m_sde", "res_multistep", "uni_pc", "lcm"]
_SCHEDS = ["simple", "normal", "karras", "exponential", "sgm_uniform", "beta"]

COMFY_MODES = {
    "edit": dict(title="Image Editing", run="Edit image", refs={"image": 10}, tags={"image": "<image{n}>"},
                 blurb="Load the picture you want to change, then describe the change. Picture 1 is the one that gets edited, "
                       "the others are references (a style, a person, a piece of clothing...).",
                 hint="Up to 10 pictures. Click a tag to put it into your prompt."),
    "generate": dict(title="Image Generation", run="Generate image", refs={}, tags={},
                     blurb="Describe a picture and let Qwen Image 2.1 draw it. Native 2K is possible: 1:1 with 4 megapixels.",
                     hint=""),
    "video": dict(title="Video Generation", run="Generate video", refs={"image": 9, "video": 3, "audio": 3},
                  tags={"image": "<Picture {n}>", "video": "<Video {n}>", "audio": "<Audio {n}>"},
                  blurb="MiniMax H3 reference-to-video with sound. Add pictures, videos or voice clips and refer to them in the "
                        "prompt by their tags, in the order shown.",
                  hint="Up to 9 pictures, 3 videos and 3 audio clips. Click a tag to put it into your prompt."),
}

# key, label, kind (int float bool str combo combo_i combo_f), default, options, tip, advanced, [(node class, input, title contains)]
COMFY_FIELDS = {
    "edit": [
        ("resolution", "Resolution budget", "combo_i", 0, [0, 768, 1024, 1536, 2048], "Total pixel budget of the result, aspect ratio is kept. 0 = keep the size of picture 1 (rounded to a multiple of 32).", False, [(_TE, "resolution", None)]),
        ("steps", "Steps", "int", 25, None, "Qwen Image 2.1 officially uses 40-50 with euler, 25 is quicker.", False, [(_KS, "steps", None)]),
        ("seed", "Seed", "int", 0, None, "Random picks a new seed every run; the seed that was used is shown here afterwards.", False, [(_KS, "seed", None)]),
        ("sampler", "Sampler", "combo", "euler", ("info", _KS, "sampler_name"), "", True, [(_KS, "sampler_name", None)]),
        ("scheduler", "Scheduler", "combo", "simple", ("info", _KS, "scheduler"), "", True, [(_KS, "scheduler", None)]),
        ("cfg", "CFG", "float", 1.0, None, "Keep 1 unless you use a negative prompt.", True, [(_KS, "cfg", None)]),
        ("negative", "Negative prompt", "str", "", None, "Only has an effect when CFG is above 1.", True, [(_TE, "negative_prompt", None)]),
        ("custom", "Custom size", "bool", False, None, "Use the width and height below instead of the size of picture 1.", True, [("ComfySwitchNode", "switch", None)]),
        ("cw", "Width", "int", 1024, None, "", True, [("EmptyLatentImage", "width", None)]),
        ("ch", "Height", "int", 1024, None, "", True, [("EmptyLatentImage", "height", None)]),
    ],
    "generate": [
        ("aspect", "Aspect ratio", "combo", "1:1 (Square)", ("info", _RS, "aspect_ratio"), "", False, [(_RS, "aspect_ratio", None)]),
        ("mp", "Megapixels", "combo_f", 1.0, [0.25, 0.5, 1.0, 2.0, 4.0], "1 MP = 1024x1024. 4 MP at 1:1 is the native 2K size (2048x2048).", False, [(_RS, "megapixels", None)]),
        ("steps", "Steps", "int", 25, None, "Qwen Image 2.1 officially uses 40-50 with euler, 25 is quicker.", False, [(_KS, "steps", None)]),
        ("seed", "Seed", "int", 0, None, "Random picks a new seed every run; the seed that was used is shown here afterwards.", False, [(_KS, "seed", None)]),
        ("transparent", "Transparent background", "bool", False, None, "Wraps the prompt in the RGBA wording the model expects. Results are saved as PNG with an alpha channel.", False, []),
        ("sampler", "Sampler", "combo", "euler", ("info", _KS, "sampler_name"), "", True, [(_KS, "sampler_name", None)]),
        ("scheduler", "Scheduler", "combo", "simple", ("info", _KS, "scheduler"), "", True, [(_KS, "scheduler", None)]),
        ("cfg", "CFG", "float", 1.0, None, "Keep 1 unless you use a negative prompt.", True, [(_KS, "cfg", None)]),
        ("negative", "Negative prompt", "str", "", None, "Only has an effect when CFG is above 1.", True, [(_TE, "negative_prompt", None)]),
    ],
    "video": [
        ("duration", "Duration (s)", "float", 5.0, None, "Up to about 15 s. It is turned into a valid frame count.", False, [("PrimitiveFloat", "value", None)]),
        ("aspect", "Aspect ratio", "combo", "16:9 (Widescreen)", ("info", _RS, "aspect_ratio"), "", False, [(_RS, "aspect_ratio", None)]),
        ("mp", "Megapixels", "combo_f", 0.4, [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.98], "0.98 is the official 768p size (1344x768).", False, [(_RS, "megapixels", None)]),
        ("turbo", "Turbo LoRA", "bool", False, None, "Switches to the 4-step turbo LoRA: much faster, a little less detail.", False, [("PrimitiveBoolean", "value", None)]),
        ("steps", "Steps", "int", 20, None, "Used when Turbo is off.", False, [("PrimitiveInt", "value", "Full")]),
        ("tsteps", "Turbo steps", "int", 4, None, "Used when Turbo is on.", False, [("PrimitiveInt", "value", "Lightning")]),
        ("seed", "Seed", "int", 0, None, "Random picks a new seed every run; the seed that was used is shown here afterwards.", False, [("RandomNoise", "noise_seed", None)]),
        ("refsize", "Reference quality", "combo", "match", ["match", "max"], "match scales references to the video size (faster). max keeps up to 2048 px for stronger identity, but is slower.", False, [("MiniMaxH3ReferenceToVideo", "ref_image_size", None)]),
        ("pairaudio", "Use each video's own sound", "bool", False, None, "Also feed the soundtrack of every reference video. Leave it off for silent clips.", False, []),
        ("sampler", "Sampler", "combo", "res_multistep", ("info", "KSamplerSelect", "sampler_name"), "", True, [("KSamplerSelect", "sampler_name", None)]),
        ("scheduler", "Scheduler", "combo", "simple", ("info", "BasicScheduler", "scheduler"), "beta or normal often beat simple for reference-heavy prompts.", True, [("BasicScheduler", "scheduler", None)]),
    ],
}

COMFY_LOADERS = {"UNETLoader": ("unet_name", "Diffusion model"), "UnetLoaderGGUF": ("unet_name", "Diffusion model"),
                 "CLIPLoader": ("clip_name", "Text encoder"), "CLIPLoaderGGUF": ("clip_name", "Text encoder"),
                 "VAELoader": ("vae_name", "VAE"), "LoraLoaderModelOnly": ("lora_name", "LoRA"), "LoraLoader": ("lora_name", "LoRA"),
                 "CheckpointLoaderSimple": ("ckpt_name", "Checkpoint")}


def comfy_options(info, cls, inp):
    """Combo options of one node input as ComfyUI reports them (old and new object_info style), or None."""
    spec = ((info or {}).get(cls) or {}).get("input") or {}
    for sect in ("required", "optional"):
        v = (spec.get(sect) or {}).get(inp)
        if isinstance(v, list) and v:
            if isinstance(v[0], list):
                return [str(x) for x in v[0]]
            if v[0] == "COMBO" and len(v) > 1 and isinstance(v[1], dict) and isinstance(v[1].get("options"), list):
                return [str(x) for x in v[1]["options"]]
    return None


def comfy_scan_models(root, limit=800):
    out, root = [], Path(str(root)).expanduser()
    if not root.is_dir():
        return out
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if not d.startswith(".")]
        for fn in fns:
            if fn.lower().endswith((".safetensors", ".gguf", ".ckpt", ".pt", ".pth", ".bin", ".sft")):
                out.append(os.path.relpath(os.path.join(dp, fn), root).replace(os.sep, "/"))
                if len(out) >= limit:
                    return sorted(out)
    return sorted(out)


def comfy_apply(api, mode, snap, seed, upload):
    """Puts the page's prompt, settings and references into the API prompt. upload(path) -> file name inside ComfyUI's input folder."""
    o = snap["opts"]
    prompt = snap["prompt"].strip()
    if mode == "generate" and o.get("transparent"):
        prompt = "This is an RGBA format image with transparency. " + prompt + ". The image has an alpha channel and a transparent background."
    if mode == "video":
        if not _put(api, "PrimitiveStringMultiline", "value", prompt):
            _put(api, "MiniMaxH3ReferenceToVideo", "prompt", prompt)
    else:
        _put(api, _TE, "prompt", prompt)
    for key, _l, kind, _d, _o, _t, _a, targets in COMFY_FIELDS[mode]:
        v = seed if key == "seed" else o.get(key)
        if v is None:
            continue
        for cls, inp, title in targets:
            _put(api, cls, inp, v, title)
    for key, val in (snap.get("models") or {}).items():
        nid, _, inp = key.partition("|")
        if nid in api and val:
            api[nid]["inputs"][inp] = val
    refs = snap.get("refs") or []
    imgs, vids, auds = ([r for r in refs if r["kind"] == k] for k in ("image", "video", "audio"))

    def node(nid, cls, **inputs):
        api[nid] = {"class_type": cls, "inputs": inputs, "_meta": {"title": "Forge " + nid}}
        return nid

    if mode == "edit":
        _drop(api, {"LoadImage", "ImageCompare", "PreviewImage"})
        for te in _find(api, _TE):
            ins = api[te]["inputs"]
            for k in [k for k in ins if k.startswith("images.")]:
                del ins[k]
            for i, r in enumerate(imgs[:10], 1):
                ins["images.image_%d" % i] = [node("forge_img%d" % i, "LoadImage", image=upload(r["path"])), 0]
    elif mode == "video":
        _drop(api, {"LoadImage", "LoadVideo", "LoadAudio", "GetVideoComponents"})
        for mm in _find(api, "MiniMaxH3ReferenceToVideo"):
            ins = api[mm]["inputs"]
            for k in [k for k in ins if k.split(".")[0] in ("ref_images", "ref_videos", "ref_video_audios", "ref_audios")]:
                del ins[k]
            for i, r in enumerate(imgs[:9]):
                ins["ref_images.ref_image_%d" % i] = [node("forge_img%d" % i, "LoadImage", image=upload(r["path"])), 0]
            for i, r in enumerate(vids[:3]):
                lv = node("forge_vid%d" % i, "LoadVideo", file=upload(r["path"]))
                gv = node("forge_vidparts%d" % i, "GetVideoComponents", video=[lv, 0])
                ins["ref_videos.ref_video_%d" % i] = [gv, 0]
                if o.get("pairaudio"):
                    ins["ref_video_audios.ref_video_audio_%d" % i] = [gv, 1]
            for i, r in enumerate(auds[:3]):
                ins["ref_audios.ref_audio_%d" % i] = [node("forge_aud%d" % i, "LoadAudio", audio=upload(r["path"])), 0]
    return api


# ---------- talking to ComfyUI ----------
class ComfyClient:
    """Tiny ComfyUI HTTP client, standard library only."""

    def __init__(self, cfg):
        self.cfg = cfg

    def base(self):
        return (str(self.cfg.get("comfy_url", "")).strip() or "http://127.0.0.1:8188").rstrip("/")

    def req(self, method, path, body=None, headers=None, timeout=30):
        u = urlparse(self.base() + path)
        https = u.scheme == "https"
        conn = (http.client.HTTPSConnection if https else http.client.HTTPConnection)(u.hostname, u.port or (443 if https else 80), timeout=timeout)
        try:
            conn.request(method, u.path + ("?" + u.query if u.query else ""), body=body, headers=headers or {})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def get_json(self, path, timeout=30):
        st, raw = self.req("GET", path, timeout=timeout)
        if st != 200:
            raise RuntimeError("HTTP %d from %s" % (st, path))
        return json.loads(raw.decode("utf-8", "replace"))

    def get_bytes(self, path, timeout=300):
        st, raw = self.req("GET", path, timeout=timeout)
        if st != 200:
            raise RuntimeError("HTTP %d from %s" % (st, path))
        return raw

    def post_json(self, path, obj, timeout=30):
        return self.req("POST", path, json.dumps(obj).encode("utf-8"), {"Content-Type": "application/json"}, timeout)

    def upload(self, path):
        """Copies a local file into ComfyUI's input folder (name is derived from path+size+time, so it never clashes)."""
        p = Path(path)
        st = p.stat()
        name = "forge_" + hashlib.sha1(("%s|%d|%d" % (p, st.st_size, st.st_mtime_ns)).encode()).hexdigest()[:12] + p.suffix.lower()
        b = uuid.uuid4().hex
        head = "".join('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (b, k, v) for k, v in (("type", "input"), ("overwrite", "true")))
        head += '--%s\r\nContent-Disposition: form-data; name="image"; filename="%s"\r\nContent-Type: %s\r\n\r\n' % (b, name, mimetypes.guess_type(name)[0] or "application/octet-stream")
        body = head.encode() + p.read_bytes() + ("\r\n--%s--\r\n" % b).encode()
        code, raw = self.req("POST", "/upload/image", body, {"Content-Type": "multipart/form-data; boundary=" + b}, 900)
        if code != 200:
            raise RuntimeError("uploading %s failed: HTTP %d %s" % (p.name, code, clip(raw.decode("utf-8", "replace"), 200)))
        j = json.loads(raw.decode("utf-8", "replace"))
        return (j["subfolder"] + "/" if j.get("subfolder") else "") + j.get("name", name)

    def submit(self, api, cid):
        code, raw = self.post_json("/prompt", {"prompt": api, "client_id": cid}, 120)
        txt = raw.decode("utf-8", "replace")
        if code == 200:
            return json.loads(txt)["prompt_id"]
        raise RuntimeError(self.explain(txt, code))

    @staticmethod
    def explain(txt, code):
        try:
            j = json.loads(txt)
        except ValueError:
            return "ComfyUI said HTTP %d: %s" % (code, clip(txt, 400))
        lines, err = [], j.get("error")
        if isinstance(err, dict):
            lines.append(str(err.get("message", "")) + (" - " + str(err["details"]) if err.get("details") else ""))
        elif err:
            lines.append(str(err))
        for nid, ne in (j.get("node_errors") or {}).items():
            for e in ne.get("errors") or []:
                lines.append("node %s (%s): %s %s" % (nid, ne.get("class_type", "?"), e.get("message", ""), e.get("details", "")))
        return "\n".join(l for l in lines if l.strip()) or "ComfyUI said HTTP %d: %s" % (code, clip(txt, 400))


class ComfyWS:
    """Minimal WebSocket client (receive only) - used just for the progress bar. If it fails nothing else is affected."""

    def __init__(self, base, path):
        u = urlparse(base)
        secure = u.scheme == "https"
        host, port = u.hostname, u.port or (443 if secure else 80)
        s = socket.create_connection((host, port), 8)
        if secure:
            s = ssl.create_default_context().wrap_socket(s, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                   "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(4096)
            if not c:
                raise OSError("websocket closed during the handshake")
            buf += c
        head, _, self.buf = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise OSError("websocket refused")
        s.settimeout(1.0)
        self.s = s

    def _frame(self):
        b = self.buf
        if len(b) < 2:
            return None
        ln, off = b[1] & 127, 2
        if ln == 126:
            if len(b) < 4:
                return None
            ln, off = int.from_bytes(b[2:4], "big"), 4
        elif ln == 127:
            if len(b) < 10:
                return None
            ln, off = int.from_bytes(b[2:10], "big"), 10
        if len(b) < off + ln:
            return None
        self.buf = b[off + ln:]
        return b[0] & 15, b[off:off + ln]

    def recv(self):
        """Next text message. Raises socket.timeout when nothing arrived, OSError when the socket is closed."""
        while True:
            f = self._frame()
            if f:
                op, payload = f
                if op == 1:
                    return payload.decode("utf-8", "replace")
                if op == 8:
                    raise OSError("websocket closed")
                if op == 9 and len(payload) < 126:  # ping -> masked pong
                    m = os.urandom(4)
                    self.s.sendall(bytes([0x8A, 0x80 | len(payload)]) + m + bytes(c ^ m[i % 4] for i, c in enumerate(payload)))
                continue
            c = self.s.recv(65536)
            if not c:
                raise OSError("websocket closed")
            self.buf += c

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


# ---------- small widgets helpers ----------
def _local_wheel(w):
    """A text box inside a scrolled page scrolls itself, not the page."""
    def f(e):
        w.yview_scroll(-1 if (e.num == 4 or e.delta > 0) else 1, "units")
        return "break"
    for ev in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
        w.bind(ev, f)


def _open_path(path):
    try:
        if os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        messagebox.showinfo("Open", "%s\n\n(%s)" % (path, e))


# ═════════ search dialog: find pictures / videos / audio anywhere on the disk ═════════
class ComfySearch(tk.Toplevel):
    def __init__(self, tab, kinds, on_attach):
        tk.Toplevel.__init__(self, tab.parent)
        self.tab, self.kinds, self.on_attach = tab, set(kinds), on_attach
        self.title("Find files on this computer")
        self.geometry("1040x680")
        self.transient(tab.parent.winfo_toplevel())
        self.configure(background=THEME["bg"])
        self.stop, self.q, self.items, self.photo = threading.Event(), queue.Queue(), [], None
        self.sort_col, self.sort_rev, self.scanning = "date", True, False
        s = tab.state.setdefault("search", {})
        labels = {"image": "Pictures", "video": "Videos", "audio": "Audio"}
        self.type_names = [labels[k] for k in ("image", "video", "audio") if k in self.kinds]
        if len(self.type_names) > 1:
            self.type_names.insert(0, "All of these")
        self.root_var = tk.StringVar(value=s.get("root") or str(Path.home()))
        self.q_var = tk.StringVar(value=s.get("query", ""))
        self.type_var = tk.StringVar(value=s.get("type") if s.get("type") in self.type_names else self.type_names[0])
        self.deep_var = tk.BooleanVar(value=s.get("deep", True))
        top = ttk.Frame(self, padding=(14, 12, 14, 4))
        top.pack(fill="x")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Look in").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
        ttk.Combobox(top, textvariable=self.root_var, values=tab.quick_places()).grid(row=0, column=1, columnspan=4, sticky="ew", pady=3)
        ttk.Button(top, text="Browse...", style="Mini.TButton", command=self.browse).grid(row=0, column=5, padx=(6, 0))
        ttk.Label(top, text="Name contains").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=3)
        e = ttk.Entry(top, textvariable=self.q_var)
        e.grid(row=1, column=1, sticky="ew", pady=3)
        e.bind("<Return>", lambda _e: self.search())
        ttk.Combobox(top, textvariable=self.type_var, values=self.type_names, state="readonly", width=12).grid(row=1, column=2, padx=6)
        ttk.Checkbutton(top, text="Subfolders", variable=self.deep_var).grid(row=1, column=3, padx=6)
        self.btn_go = ttk.Button(top, text="Search", style="Accent.TButton", command=self.search)
        self.btn_go.grid(row=1, column=4, padx=(6, 0))
        self.btn_stop = ttk.Button(top, text="Stop", style="Mini.TButton", command=self.stop.set, state="disabled")
        self.btn_stop.grid(row=1, column=5, padx=(6, 0))
        ttk.Label(top, text="Wildcards work: *.png, IMG_*, *holiday*. Leave the name empty to list everything in the folder.", style="Muted.TLabel").grid(row=2, column=1, columnspan=5, sticky="w")
        mid = ttk.Frame(self, padding=(14, 6))
        mid.pack(fill="both", expand=True)
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)
        cols = ("name", "folder", "size", "date")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        for c, w, t in (("name", 250, "Name"), ("folder", 280, "Folder"), ("size", 80, "Size"), ("date", 150, "Modified")):
            self.tree.heading(c, text=t, command=lambda c=c: self.sort_by(c))
            self.tree.column(c, width=w, stretch=(c in ("name", "folder")))
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        side = ttk.Frame(mid, style="Card.TFrame", padding=10)
        side.grid(row=0, column=2, sticky="ns", padx=(12, 0))
        self.pv = ttk.Label(side, style="Card.TLabel", anchor="center", text="Select a file to preview it")
        self.pv.pack()
        self.pv_info = ttk.Label(side, style="CardMuted.TLabel", wraplength=270, justify="left")
        self.pv_info.pack(anchor="w", pady=(8, 0))
        bot = ttk.Frame(self, padding=(14, 4, 14, 12))
        bot.pack(fill="x")
        self.status = ttk.Label(bot, text="", style="Muted.TLabel")
        self.status.pack(side="left")
        ttk.Button(bot, text="Close", command=self.close).pack(side="right")
        ttk.Button(bot, text="Attach selected", style="Accent.TButton", command=self.attach).pack(side="right", padx=8)
        ttk.Label(bot, text="Ctrl/Shift-click for several. Double-click attaches.", style="Muted.TLabel").pack(side="right", padx=8)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.after_idle(self.preview))
        self.tree.bind("<Double-1>", lambda _e: self.attach())
        self.protocol("WM_DELETE_WINDOW", self.close)
        e.focus_set()
        self.after(80, self.drain)

    def browse(self):
        p = filedialog.askdirectory(parent=self, initialdir=self.root_var.get() or str(Path.home()))
        if p:
            self.root_var.set(p)

    def search(self):
        if self.scanning:
            return
        root = Path(self.root_var.get().strip() or ".").expanduser()
        if not root.is_dir():
            self.status.configure(text="That folder does not exist.")
            return
        sel = self.type_var.get()
        want = {"Pictures": {"image"}, "Videos": {"video"}, "Audio": {"audio"}}.get(sel, self.kinds) & self.kinds
        self.tab.state["search"] = {"root": str(root), "query": self.q_var.get(), "type": sel, "deep": bool(self.deep_var.get())}
        self.tab.save_soon()
        self.tree.delete(*self.tree.get_children())
        self.items = []
        self.stop = threading.Event()
        self.q = queue.Queue()
        self.scanning = True
        self.btn_go.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.status.configure(text="Searching...")
        threading.Thread(target=self.scan, args=(root, self.q_var.get().strip().lower(), want, bool(self.deep_var.get()), self.stop, self.q), daemon=True).start()

    @staticmethod
    def scan(root, pat, kinds, deep, stop, out):
        glob, n = any(c in pat for c in "*?["), 0
        for dp, dns, fns in os.walk(str(root), followlinks=False):
            if stop.is_set():
                break
            dns[:] = [d for d in dns if not d.startswith(".") and d not in SEARCH_SKIP and not d.endswith("~")] if deep else []
            for fn in fns:
                if comfy_kind(fn) not in kinds:
                    continue
                low = fn.lower()
                if pat and not (fnmatch.fnmatch(low, pat) if glob else pat in low):
                    continue
                full = os.path.join(dp, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                out.put({"path": full, "size": st.st_size, "mtime": st.st_mtime})
                n += 1
                if n >= 5000:
                    out.put(None)
                    return
        out.put(None)

    def row_values(self, it):
        return (os.path.basename(it["path"]), os.path.dirname(it["path"]), human_size(it["size"]), datetime.fromtimestamp(it["mtime"]).strftime("%Y-%m-%d %H:%M"))

    def drain(self):
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        done = False
        for _ in range(300):
            try:
                it = self.q.get_nowait()
            except queue.Empty:
                break
            if it is None:
                done = True
                break
            self.items.append(it)
            self.tree.insert("", "end", iid=str(len(self.items) - 1), values=self.row_values(it))
        if self.scanning:
            self.status.configure(text="Searching...  %d found" % len(self.items))
        if done:
            self.scanning = False
            self.btn_go.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.sort_by("date", True)
            self.status.configure(text=("%d file(s) found" % len(self.items)) + (" (stopped early)" if self.stop.is_set() else "") + (" - limit of 5000 reached" if len(self.items) >= 5000 else ""))
        self.after(80, self.drain)

    def sort_by(self, col, rev=None):
        self.sort_rev = (not self.sort_rev if col == self.sort_col else False) if rev is None else rev
        self.sort_col = col
        key = {"name": lambda i: os.path.basename(i["path"]).lower(), "folder": lambda i: os.path.dirname(i["path"]).lower(),
               "size": lambda i: i["size"], "date": lambda i: i["mtime"]}[col]
        self.items.sort(key=key, reverse=self.sort_rev)
        self.tree.delete(*self.tree.get_children())
        for n, it in enumerate(self.items):
            self.tree.insert("", "end", iid=str(n), values=self.row_values(it))

    def preview(self):
        sel = self.tree.selection()
        if not sel:
            return
        it = self.items[int(sel[0])]
        try:
            self.photo = ImageTk.PhotoImage(self.tab.thumb_image(it["path"], 280))
            self.pv.configure(image=self.photo, text="")
        except Exception:
            self.pv.configure(image="", text="(no preview)")
        info = "%s\n%s" % (os.path.basename(it["path"]), human_size(it["size"]))
        if comfy_kind(it["path"]) == "image" and Image is not None:
            try:
                with Image.open(it["path"]) as im:
                    info += "\n%d x %d px" % im.size
            except Exception:
                pass
        if len(sel) > 1:
            info += "\n\n%d files selected" % len(sel)
        self.pv_info.configure(text=info)

    def attach(self):
        paths = [self.items[int(i)]["path"] for i in self.tree.selection()]
        if paths:
            self.on_attach(paths)
            self.close()

    def close(self):
        self.stop.set()
        self.destroy()


# ═════════ one page = one workflow ═════════
class ComfyPage(ttk.Frame):
    def __init__(self, tab, mode):
        ttk.Frame.__init__(self, tab.stack)
        self.tab, self.mode, self.spec = tab, mode, COMFY_MODES[mode]
        self.st = tab.state["modes"][mode]
        self.vars, self.kinds, self.combos, self.model_rows = {}, {}, {}, {}
        self.photos, self.api0, self.wf_error, self.wf_name = [], None, "", ""
        self.shown, self._cur_path, self._cur_im, self._fit_job = 0, None, None, None
        self.load_wf()
        self.build()

    # ---- workflow defaults
    def load_wf(self):
        self.api0, self.wf_error, self.wf_name = None, "", ""
        p = comfy_find_workflow(self.tab.cfg, self.mode)
        if p is None:
            self.wf_error = "No workflow file found. Put your JSON in %s or set it in Settings -> ComfyUI." % self.tab.cfg["comfy_wf_dir"]
            return
        self.wf_name = p.name
        try:
            self.api0 = comfy_to_api(json.loads(p.read_text("utf-8")), self.tab.info)
        except Exception as e:
            self.wf_error = "%s: %s" % (p.name, e)

    def default(self, f):
        key, _l, kind, default, _o, _t, _a, targets = f
        if self.api0:
            for cls, inp, title in targets[:1]:
                v = _get(self.api0, cls, inp, title)
                if v is not None:
                    return v
        return default

    # ---- layout
    def build(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        lcol = ttk.Frame(self)
        lcol.grid(row=0, column=0, sticky="ns", padx=(0, 14))
        runbar = ttk.Frame(lcol)
        runbar.pack(side="bottom", fill="x", pady=(8, 0))
        outer, inner = scroll_frame(lcol)
        outer.winfo_children()[0].configure(width=470)
        outer.pack(side="top", fill="both", expand=True)
        ttk.Label(inner, text=self.spec["blurb"], style="Muted.TLabel", wraplength=450, justify="left").pack(anchor="w", pady=(2, 4))
        self.wf_label = ttk.Label(inner, text="", style="Muted.TLabel", wraplength=450, justify="left")
        self.wf_label.pack(anchor="w")
        ttk.Label(inner, text="PROMPT", style="Section.TLabel").pack(anchor="w", pady=(12, 3))
        self.prompt = tk.Text(inner, height=6, wrap="word", undo=True)
        self.prompt.pack(fill="x")
        self.prompt.insert("1.0", self.st.get("prompt", ""))
        self.prompt.bind("<KeyRelease>", self.prompt_changed)
        self.prompt.bind("<FocusOut>", self.prompt_changed)
        _local_wheel(self.prompt)
        if self.spec["refs"]:
            self.build_refs(inner)
        ttk.Label(inner, text="SETTINGS", style="Section.TLabel").pack(anchor="w", pady=(14, 3))
        form = ttk.Frame(inner)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        self.fields_into(form, False)
        self.readout = ttk.Label(inner, text="", style="Muted.TLabel")
        self.readout.pack(anchor="w", pady=(4, 0))
        adv = self.fold(inner, "Advanced")
        adv.columnconfigure(1, weight=1)
        self.fields_into(adv, True)
        self.models_body = self.fold(inner, "Models")
        self.btn_run = ttk.Button(runbar, text=self.spec["run"], style="Accent.TButton", command=lambda: self.tab.run(self.mode))
        self.btn_run.pack(side="left", fill="x", expand=True)
        ttk.Label(runbar, text="   Runs").pack(side="left")
        self.runs_var = tk.StringVar(value=str(self.st["opts"].get("runs", "1")))
        self.runs_var.trace_add("write", lambda *_a: self.changed("runs"))
        ttk.Entry(runbar, textvariable=self.runs_var, width=4).pack(side="left", padx=(4, 0))
        Tip(self.runs_var and runbar.winfo_children()[-1], "How many images/videos to make one after the other. Without 'Random' every run uses seed + 1.")
        self.build_view()
        self.wf_label.configure(text=("Workflow: " + self.wf_name) if self.api0 else self.wf_error,
                                style="Muted.TLabel" if self.api0 else "Bad.TLabel")
        self.fill_models()
        self.readout_update()
        self.render_refs()
        self.show()

    def fold(self, parent, title):
        body = ttk.Frame(parent)
        btn = ttk.Button(parent, text="\u25b8  " + title)

        def toggle():
            if body.winfo_manager():
                body.pack_forget()
                btn.configure(text="\u25b8  " + title)
            else:
                body.pack(fill="x", after=btn, pady=(4, 0))
                btn.configure(text="\u25be  " + title)
        btn.configure(command=toggle)
        btn.pack(fill="x", pady=(12, 0))
        return body

    def fields_into(self, parent, advanced):
        row = 0
        for f in COMFY_FIELDS[self.mode]:
            key, label, kind, _d, _o, tip, adv, _t = f
            if adv != advanced:
                continue
            lab = ttk.Label(parent, text=label)
            lab.grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)
            cur = self.st["opts"].get(key, self.default(f))
            if kind == "bool":
                v = tk.BooleanVar(value=bool(cur))
                w = ttk.Checkbutton(parent, variable=v)
                w.grid(row=row, column=1, sticky="w")
            elif kind.startswith("combo"):
                v = tk.StringVar(value=str(cur))
                self.vars[key] = v
                w = ttk.Combobox(parent, textvariable=v, width=26)
                w.grid(row=row, column=1, sticky="ew")
                self.combos[key] = (w, f)
                w.configure(values=self.combo_values(f))
            else:
                v = tk.StringVar(value=str(cur))
                w = ttk.Entry(parent, textvariable=v, width=14 if kind != "str" else 30)
                w.grid(row=row, column=1, sticky="ew" if kind == "str" else "w")
            if key == "seed":
                self.rand_var = tk.BooleanVar(value=bool(self.st["opts"].get("randomize", True)))
                self.rand_var.trace_add("write", lambda *_a: self.changed("randomize"))
                ttk.Checkbutton(parent, text="Random", variable=self.rand_var).grid(row=row, column=2, padx=(10, 0))
            self.vars[key], self.kinds[key] = v, kind
            v.trace_add("write", lambda *_a, k=key: self.changed(k))
            if tip:
                Tip(lab, tip)
                Tip(w, tip)
            row += 1

    def combo_values(self, f):
        key, _l, _k, _d, opts, *_r = f
        base = []
        if isinstance(opts, tuple) and opts and opts[0] == "info":
            base = comfy_options(self.tab.info, opts[1], opts[2]) or []
        elif opts:
            base = [str(x) for x in opts]
        cur = self.vars[key].get() if key in self.vars else str(self.default(f))
        return ([cur] if cur and cur not in base else []) + list(base)

    def refresh_options(self):
        for key, (cb, f) in self.combos.items():
            cb.configure(values=self.combo_values(f))
        for key, (cls, inp, cb, badge, v) in self.model_rows.items():
            got = comfy_options(self.tab.info, cls, inp)
            cb.configure(values=got if got is not None else self.tab.folder_models())
            self.model_badge(got, v.get(), badge)

    @staticmethod
    def model_badge(got, cur, badge):
        badge.configure(text="not visible to ComfyUI" if (got is not None and cur and cur not in got) else "")

    def fill_models(self):
        for w in self.models_body.winfo_children():
            w.destroy()
        self.model_rows = {}
        rows = []
        for nid, n in (self.api0 or {}).items():
            spec = COMFY_LOADERS.get(n["class_type"])
            if spec and isinstance(n["inputs"].get(spec[0]), str):
                rows.append((nid, n["class_type"], spec[0], spec[1], n["inputs"][spec[0]]))
        if not rows:
            ttk.Label(self.models_body, text="This workflow has no model loaders.", style="Muted.TLabel").pack(anchor="w")
            return
        ttk.Label(self.models_body, text="Models found by ComfyUI are listed. Your folder: " + str(self.tab.cfg["comfy_models_dir"]),
                  style="Muted.TLabel", wraplength=450, justify="left").pack(anchor="w", pady=(0, 4))
        total, idx = {}, {}
        for r in rows:
            total[r[3]] = total.get(r[3], 0) + 1
        for nid, cls, inp, label, val in rows:
            idx[label] = idx.get(label, 0) + 1
            key = nid + "|" + inp
            v = tk.StringVar(value=self.st["opts"].get("models", {}).get(key, val))
            row = ttk.Frame(self.models_body)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label if total[label] == 1 else "%s %d" % (label, idx[label]), width=16).pack(side="left")
            cb = ttk.Combobox(row, textvariable=v, width=34)
            cb.pack(side="left", fill="x", expand=True)
            badge = ttk.Label(row, text="", style="Bad.TLabel")
            badge.pack(side="left", padx=(6, 0))
            v.trace_add("write", lambda *_a, k=key, v=v: self.model_changed(k, v))
            self.model_rows[key] = (cls, inp, cb, badge, v)
        self.refresh_options()

    def model_changed(self, key, v):
        self.st["opts"].setdefault("models", {})[key] = v.get()
        cls, inp, cb, badge, _v = self.model_rows[key]
        self.model_badge(comfy_options(self.tab.info, cls, inp), v.get(), badge)
        self.tab.save_soon()

    def reload_workflow(self):
        self.load_wf()
        self.wf_label.configure(text=("Workflow: " + self.wf_name) if self.api0 else self.wf_error,
                                style="Muted.TLabel" if self.api0 else "Bad.TLabel")
        self.fill_models()
        self.readout_update()

    # ---- state
    def changed(self, key=None):
        if key == "runs":
            self.st["opts"]["runs"] = self.runs_var.get()
        elif key == "randomize":
            self.st["opts"]["randomize"] = bool(self.rand_var.get())
        elif key in self.vars:
            self.st["opts"][key] = self.vars[key].get()
        self.readout_update()
        self.tab.save_soon()

    def prompt_changed(self, _e=None):
        self.st["prompt"] = self.prompt.get("1.0", "end-1c")
        self.tab.save_soon()

    def readout_update(self):
        parts = []
        if "aspect" in self.vars and "mp" in self.vars:
            m = re.search(r"(\d+(?:\.\d+)?)\s*:\s*(\d+(?:\.\d+)?)", self.vars["aspect"].get())
            try:
                mp = float(self.vars["mp"].get())
            except ValueError:
                mp = 0
            if m and mp > 0 and float(m.group(2)) > 0:
                rw, rh = float(m.group(1)), float(m.group(2))
                mult = int(_get(self.api0 or {}, _RS, "multiple") or 8)
                w = math.sqrt(mp * 1048576 * rw / rh)
                parts.append("%d x %d px" % (max(mult, round(w / mult) * mult), max(mult, round(w * rh / rw / mult) * mult)))
        if "duration" in self.vars:
            try:
                n = max(5, round(float(self.vars["duration"].get()) * 24))
                parts.append("%d frames, %.1f s at 24 fps" % (n + (5 - n % 17) % 17, (n + (5 - n % 17) % 17) / 24.0))
            except ValueError:
                pass
        self.readout.configure(text="Result size:  " + "   |   ".join(parts) if parts else "")

    def snapshot(self):
        o = {k: _cast(self.kinds[k], v.get()) for k, v in self.vars.items()}
        try:
            runs = max(1, min(200, int(float(self.runs_var.get()))))
        except ValueError:
            runs = 1
        return {"prompt": self.prompt.get("1.0", "end-1c"), "opts": o, "runs": runs, "randomize": bool(self.rand_var.get()),
                "seed": o.get("seed"), "models": dict(self.st["opts"].get("models", {})),
                "refs": [{"path": r["path"], "kind": comfy_kind(r["path"])} for r in self.st["refs"] if comfy_kind(r["path"])]}

    def validate(self):
        snap = self.snapshot()
        if not snap["prompt"].strip():
            return "Write a prompt first."
        if self.api0 is None:
            return self.wf_error or "The workflow could not be read."
        if self.mode == "edit" and not any(r["kind"] == "image" for r in snap["refs"]):
            return "Add the picture you want to edit first (it becomes <image1>)."
        for r in snap["refs"]:
            if not Path(r["path"]).is_file():
                return "A reference file is missing: " + r["path"]
        if self.tab.online is False:
            return "ComfyUI is not reachable at %s. Start it first." % self.tab.cfg["comfy_url"]
        return None

    def set_busy(self, busy):
        self.btn_run.configure(state="disabled" if busy else "normal", text="Working..." if busy else self.spec["run"])

    # ---- references
    def build_refs(self, inner):
        hdr = ttk.Frame(inner)
        hdr.pack(fill="x", pady=(14, 3))
        ttk.Label(hdr, text="REFERENCES", style="Section.TLabel").pack(side="left")
        self.ref_count = ttk.Label(hdr, text="", style="Muted.TLabel")
        self.ref_count.pack(side="left", padx=8)
        bar = ttk.Frame(inner)
        bar.pack(fill="x")
        for text, cmd in (("+ Add files", self.add_files), ("Search disk", self.search_disk), ("Clear all", self.clear_refs)):
            ttk.Button(bar, text=text, style="Mini.TButton", command=cmd).pack(side="left", padx=(0, 6))
        ttk.Label(inner, text=self.spec["hint"], style="Muted.TLabel", wraplength=450, justify="left").pack(anchor="w", pady=(4, 4))
        self.strip = ttk.Frame(inner)
        self.strip.pack(fill="x")

    def counts(self):
        c = {"image": 0, "video": 0, "audio": 0}
        for r in self.st["refs"]:
            k = comfy_kind(r["path"])
            if k in c:
                c[k] += 1
        return c

    def add_refs(self, paths, front=False):
        limits, c, skipped, new = self.spec["refs"], self.counts(), [], []
        have = {r["path"] for r in self.st["refs"]}
        for p in paths:
            k = comfy_kind(p)
            if k not in limits:
                skipped.append("%s: this page takes no %s files" % (os.path.basename(p), k or "such"))
            elif c[k] >= limits[k]:
                skipped.append("%s: limit of %d %s file(s) reached" % (os.path.basename(p), limits[k], k))
            elif p in have:
                skipped.append("%s: already added" % os.path.basename(p))
            else:
                c[k] += 1
                have.add(p)
                new.append({"path": p})
        self.st["refs"] = (new + self.st["refs"]) if front else (self.st["refs"] + new)
        self.render_refs()
        self.tab.save_soon()
        if skipped:
            self.tab.set_status("Skipped - " + "; ".join(skipped[:3]), True)
        elif new:
            self.tab.set_status("Added %d reference file(s)." % len(new))

    def add_files(self):
        pats = []
        for k, ext in (("image", IMG_EXT), ("video", VID_EXT), ("audio", AUD_EXT)):
            if k in self.spec["refs"]:
                pats += ["*" + e for e in ext] + ["*" + e.upper() for e in ext]
        paths = filedialog.askopenfilenames(parent=self, title="Add reference files", initialdir=self.tab.state.get("last_dir") or str(Path.home()),
                                            filetypes=[("Media", tuple(pats)), ("All files", "*")])
        if paths:
            self.tab.state["last_dir"] = os.path.dirname(paths[0])
            self.add_refs(list(paths))

    def search_disk(self):
        ComfySearch(self.tab, set(self.spec["refs"]), self.add_refs)

    def clear_refs(self):
        self.st["refs"] = []
        self.render_refs()
        self.tab.save_soon()

    def move_ref(self, i, d):
        r = self.st["refs"]
        if 0 <= i + d < len(r):
            r[i], r[i + d] = r[i + d], r[i]
            self.render_refs()
            self.tab.save_soon()

    def remove_ref(self, i):
        del self.st["refs"][i]
        self.render_refs()
        self.tab.save_soon()

    def insert_tag(self, tag):
        self.prompt.insert("insert", tag + " ")
        self.prompt.focus_set()
        self.prompt_changed()

    def render_refs(self):
        if not self.spec["refs"]:
            return
        for w in self.strip.winfo_children():
            w.destroy()
        self.photos = [p for p in self.photos if False]
        n, per_kind = 0, {"image": 0, "video": 0, "audio": 0}
        for i, r in enumerate(self.st["refs"]):
            kind = comfy_kind(r["path"]) or "image"
            per_kind[kind] += 1
            tag = self.spec["tags"][kind].format(n=per_kind[kind])
            exists = Path(r["path"]).is_file()
            card = ttk.Frame(self.strip, style="Card.TFrame", padding=6)
            card.grid(row=i // 3, column=i % 3, padx=(0, 8), pady=(0, 8), sticky="n")
            ph = self.tab.thumb(r["path"], 112, kind) if exists else self.tab.thumb(None, 112, "missing")
            self.photos.append(ph)
            ttk.Label(card, image=ph or "", style="Card.TLabel").pack()
            tl = ttk.Label(card, text=tag, style="CardTag.TLabel", cursor="hand2")
            tl.pack(anchor="w", pady=(4, 0))
            tl.bind("<Button-1>", lambda _e, t=tag: self.insert_tag(t))
            Tip(tl, "Click to insert " + tag + " into the prompt")
            note = "edit target" if (self.mode == "edit" and i == 0) else kind
            name = clip(os.path.basename(r["path"]), 17)
            ttk.Label(card, text=name if exists else "missing: " + name, style="CardMuted.TLabel" if exists else "CardBad.TLabel").pack(anchor="w")
            ttk.Label(card, text=note, style="CardMuted.TLabel").pack(anchor="w")
            bt = ttk.Frame(card, style="Card.TFrame")
            bt.pack(anchor="w", pady=(4, 0))
            for text, cmd in (("\u25c0", lambda i=i: self.move_ref(i, -1)), ("\u25b6", lambda i=i: self.move_ref(i, 1)), ("\u2715", lambda i=i: self.remove_ref(i))):
                ttk.Button(bt, text=text, width=2, style="Mini.TButton", command=cmd).pack(side="left", padx=(0, 3))
            n += 1
        c = self.counts()
        lim = self.spec["refs"]
        self.ref_count.configure(text="  ".join("%d/%d %s" % (c[k], lim[k], k + ("s" if lim[k] != 1 else "")) for k in ("image", "video", "audio") if k in lim))

    # ---- result viewer
    def build_view(self):
        right = ttk.Frame(self)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        head = ttk.Frame(right)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(head, text="RESULT", style="Section.TLabel").pack(side="left")
        self.caption = ttk.Label(head, text="", style="Muted.TLabel")
        self.caption.pack(side="left", padx=10)
        ring = ttk.Frame(right, style="Ring.TFrame", padding=1)
        ring.grid(row=1, column=0, sticky="nsew")
        self.box = ttk.Frame(ring, style="Preview.TFrame", width=420, height=380)
        self.box.pack(fill="both", expand=True)
        self.box.pack_propagate(False)
        self.view = ttk.Label(self.box, style="Preview.TLabel", anchor="center", justify="center")
        self.view.place(relx=0.5, rely=0.5, anchor="center")
        self.box.bind("<Configure>", lambda _e: self.refit_soon())
        self.prompt_lbl = ttk.Label(right, text="", style="Muted.TLabel", wraplength=640, justify="left")
        self.prompt_lbl.grid(row=2, column=0, sticky="w", pady=(6, 0))
        acts = ttk.Frame(right)
        acts.grid(row=3, column=0, sticky="ew", pady=(6, 4))
        self.act_btns = {}
        for key, text, cmd in (("play", "\u25b6 Play", self.play), ("open", "Open", self.open_cur), ("folder", "Show in folder", self.folder_cur),
                               ("copy", "Copy path", self.copy_cur), ("prompt", "Reuse prompt", self.reuse), ("edit", "Edit this", self.to_edit),
                               ("video", "To video", self.to_video)):
            b = ttk.Button(acts, text=text, style="Mini.TButton", command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.act_btns[key] = b
        hw = ttk.Frame(right)
        hw.grid(row=4, column=0, sticky="ew")
        self.hist = tk.Canvas(hw, height=92, highlightthickness=0)
        hs = ttk.Scrollbar(hw, orient="horizontal", command=self.hist.xview)
        self.hist.configure(xscrollcommand=hs.set)
        self.hist.pack(fill="x")
        hs.pack(fill="x")
        self.hist_in = ttk.Frame(self.hist)
        self.hist.create_window((0, 0), window=self.hist_in, anchor="nw")
        self.hist_in.bind("<Configure>", lambda _e: self.hist.configure(scrollregion=self.hist.bbox("all")))
        self.hist_photos = []

    def add_results(self, paths, prompt, seed):
        h = self.st["history"]
        for p in reversed(paths):
            h.insert(0, {"path": p, "prompt": prompt, "seed": seed, "t": time.time()})
        del h[40:]
        if "seed" in self.vars and seed is not None:
            self.vars["seed"].set(str(seed))
        self.shown = 0
        self.tab.save_soon()
        self.show()

    def cur(self):
        h = self.st["history"]
        return h[self.shown] if h and 0 <= self.shown < len(h) else None

    def show(self, idx=None):
        if idx is not None:
            self.shown = idx
        e = self.cur()
        self._cur_path = e["path"] if e else None
        self._cur_im = None
        if e is None:
            self.view.configure(image="", text="Nothing here yet.\nWrite a prompt and press  %s." % self.spec["run"])
            self.caption.configure(text="")
            self.prompt_lbl.configure(text="")
        else:
            kind = comfy_kind(e["path"])
            bits = [os.path.basename(e["path"])]
            if e.get("seed") is not None:
                bits.append("seed %s" % e["seed"])
            if e.get("t"):
                bits.append(datetime.fromtimestamp(e["t"]).strftime("%Y-%m-%d %H:%M"))
            self.caption.configure(text="   ".join(bits))
            self.prompt_lbl.configure(text=clip(" ".join(str(e.get("prompt", "")).split()), 300))
            self.refit()
        k = comfy_kind(e["path"]) if e else None
        for key, b in self.act_btns.items():
            show = {"play": k == "video", "edit": k == "image", "video": k == "image" and self.mode != "video", "open": True, "folder": True,
                    "copy": True, "prompt": True}[key]
            if show and not b.winfo_manager():
                b.pack(side="left", padx=(0, 6))
            elif not show and b.winfo_manager():
                b.pack_forget()
        if e is None:
            for b in self.act_btns.values():
                b.pack_forget()
        self.render_hist()

    def refit_soon(self):
        if self._fit_job:
            self.after_cancel(self._fit_job)
        self._fit_job = self.after(70, self.refit)

    def refit(self):
        self._fit_job = None
        p = self._cur_path
        if not p or Image is None:
            return
        try:
            if self._cur_im is None:
                self._cur_im = self.tab.big_image(p)
            im = self._cur_im
            aw, ah = max(80, self.box.winfo_width() - 12), max(80, self.box.winfo_height() - 12)
            s = min(aw / im.width, ah / im.height, 2.0)
            im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
            self.photo = ImageTk.PhotoImage(im)
            self.view.configure(image=self.photo, text="")
        except Exception as e:
            self.view.configure(image="", text="Cannot show this file:\n%s\n(%s)" % (p, e))

    def render_hist(self):
        for w in self.hist_in.winfo_children():
            w.destroy()
        self.hist_photos = []
        for i, e in enumerate(self.st["history"]):
            fr = ttk.Frame(self.hist_in, style="Sel.TFrame" if i == self.shown else "Card.TFrame", padding=2)
            fr.pack(side="left", padx=(0, 6), pady=2)
            ph = self.tab.thumb(e["path"], 72, comfy_kind(e["path"]))
            self.hist_photos.append(ph)
            lb = ttk.Label(fr, image=ph or "", style="Card.TLabel", cursor="hand2")
            lb.pack()
            lb.bind("<Button-1>", lambda _e, i=i: self.show(i))

    def open_cur(self):
        if self._cur_path:
            _open_path(self._cur_path)

    play = open_cur

    def folder_cur(self):
        if self._cur_path:
            _open_path(os.path.dirname(self._cur_path))

    def copy_cur(self):
        if self._cur_path:
            self.clipboard_clear()
            self.clipboard_append(self._cur_path)
            self.tab.set_status("Path copied: " + self._cur_path)

    def reuse(self):
        e = self.cur()
        if e:
            self.prompt.delete("1.0", "end")
            self.prompt.insert("1.0", str(e.get("prompt", "")))
            self.prompt_changed()
            if "seed" in self.vars and e.get("seed") is not None:
                self.vars["seed"].set(str(e["seed"]))
                self.rand_var.set(False)

    def to_edit(self):
        if self._cur_path:
            self.tab.pages["edit"].add_refs([self._cur_path], front=True)
            self.tab.select("edit")

    def to_video(self):
        if self._cur_path:
            self.tab.pages["video"].add_refs([self._cur_path])
            self.tab.select("video")

    def restyle(self):
        self.render_refs()
        self.render_hist()
        self.refit()


# ═════════ the tab ═════════
class ComfyTab:
    def __init__(self, app, parent):
        self.app, self.eng, self.parent, self.cfg = app, app.eng, parent, app.eng.cfg
        self.client = ComfyClient(self.cfg)
        self.q = queue.Queue()
        self.state = self.load_state()
        self.info, self.online, self.busy, self.cancel_flag = None, None, False, False
        self.cur = {"run": 1, "runs": 1, "node": "", "step": ""}
        self.proc, self.thumbs, self._save_job, self._models = None, {}, None, None
        self.build(parent)
        self.restyle()
        self.select(self.state.get("mode") if self.state.get("mode") in COMFY_MODES else "generate")
        threading.Thread(target=self.health_loop, daemon=True).start()
        parent.after(80, self.poll)

    # ---- persistent state
    def load_state(self):
        d = {}
        try:
            d = json.loads(COMFY_STATE_PATH.read_text("utf-8"))
        except (OSError, ValueError):
            pass
        d = d if isinstance(d, dict) else {}
        d.setdefault("mode", "generate")
        d.setdefault("last_dir", str(Path.home()))
        if not isinstance(d.get("modes"), dict):
            d["modes"] = {}
        for m in COMFY_MODES:
            s = d["modes"].get(m)
            s = s if isinstance(s, dict) else {}
            s["prompt"] = s["prompt"] if isinstance(s.get("prompt"), str) else ""
            s["opts"] = s["opts"] if isinstance(s.get("opts"), dict) else {}
            s["refs"] = [{"path": r["path"]} for r in (s.get("refs") or []) if isinstance(r, dict) and isinstance(r.get("path"), str)]
            s["history"] = [h for h in (s.get("history") or []) if isinstance(h, dict) and isinstance(h.get("path"), str) and Path(h["path"]).is_file()][:40]
            d["modes"][m] = s
        return d

    def save_state(self):
        self._save_job = None
        try:
            tmp = COMFY_STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1), "utf-8")
            os.replace(str(tmp), str(COMFY_STATE_PATH))
        except (OSError, TypeError, ValueError):
            pass

    def save_soon(self):
        if self._save_job:
            self.parent.after_cancel(self._save_job)
        self._save_job = self.parent.after(400, self.save_state)

    # ---- layout
    def build(self, parent):
        head = ttk.Frame(parent)
        head.pack(fill="x")
        self.dot = ttk.Label(head, text="\u25cb  checking ComfyUI...", style="Muted.TLabel")
        self.dot.pack(side="left")
        for text, cmd in (("Refresh", self.refresh), ("Free VRAM", self.free_mem), ("Open ComfyUI", lambda: webbrowser.open(self.client.base())),
                          ("Stop ComfyUI", self.stop_comfy), ("Start ComfyUI", self.start_comfy)):
            ttk.Button(head, text=text, style="Mini.TButton", command=cmd).pack(side="right", padx=(6, 0))
        pills = ttk.Frame(parent)
        pills.pack(fill="x", pady=(10, 10))
        self.pill = {}
        for m, spec in COMFY_MODES.items():
            self.pill[m] = ttk.Button(pills, text=spec["title"], style="Pill.TButton", command=lambda m=m: self.select(m))
            self.pill[m].pack(side="left", padx=(0, 8))
        bar = ttk.Frame(parent)
        bar.pack(side="bottom", fill="x", pady=(8, 0))
        self.prog = ttk.Progressbar(bar, style="Comfy.Horizontal.TProgressbar", mode="determinate", maximum=100)
        self.prog.pack(fill="x", pady=(0, 4))
        row = ttk.Frame(bar)
        row.pack(fill="x")
        self.status = ttk.Label(row, text="Ready.", style="Muted.TLabel")
        self.status.pack(side="left")
        self.btn_cancel = ttk.Button(row, text="Cancel", style="Danger.TButton", state="disabled", command=self.cancel)
        self.btn_cancel.pack(side="right")
        ttk.Button(row, text="Log", style="Mini.TButton", command=lambda: self.app.show_log_tab("ComfyUI")).pack(side="right", padx=8)
        self.stack = ttk.Frame(parent)
        self.stack.pack(side="top", fill="both", expand=True)
        self.pages = {m: ComfyPage(self, m) for m in COMFY_MODES}

    def select(self, m):
        for k, p in self.pages.items():
            p.pack_forget()
            self.pill[k].configure(style="PillOn.TButton" if k == m else "Pill.TButton")
        self.pages[m].pack(fill="both", expand=True)
        self.state["mode"] = m
        self.save_soon()

    def restyle(self):
        t, st, bold = self.app.theme, ttk.Style(), self.app.f_bold
        st.configure("Pill.TButton", padding=(26, 11), font=bold)
        on = dict(background=t["accent"], foreground=t["accent_fg"], bordercolor=t["accent"], darkcolor=t["accent"], lightcolor=t["accent"])
        st.configure("PillOn.TButton", padding=(26, 11), font=bold, **on)
        st.map("PillOn.TButton", background=[("active", t["accent_hi"]), ("pressed", t["accent_hi"])], foreground=[("active", t["accent_fg"])],
               bordercolor=[("active", t["accent_hi"])], darkcolor=[("active", t["accent_hi"])], lightcolor=[("active", t["accent_hi"])])
        st.configure("Mini.TButton", padding=(9, 4))
        for name, bg in (("Card.TFrame", t["panel"]), ("Sel.TFrame", t["accent"]), ("Preview.TFrame", t["field"]), ("Ring.TFrame", t["border"])):
            st.configure(name, background=bg)
        st.configure("Card.TLabel", background=t["panel"], foreground=t["fg"])
        st.configure("CardMuted.TLabel", background=t["panel"], foreground=t["muted"])
        st.configure("CardTag.TLabel", background=t["panel"], foreground=t["accent2"], font=bold)
        st.configure("CardBad.TLabel", background=t["panel"], foreground=t["bad"])
        st.configure("Preview.TLabel", background=t["field"], foreground=t["muted"])
        st.configure("Comfy.Horizontal.TProgressbar", troughcolor=t["panel"], background=t["accent"], bordercolor=t["panel"],
                     lightcolor=t["accent"], darkcolor=t["accent"], thickness=8)
        self.thumbs.clear()
        self.show_health()
        for p in getattr(self, "pages", {}).values():
            p.restyle()

    # ---- status, log
    def set_status(self, text, bad=False):
        self.status.configure(text=text, style="Bad.TLabel" if bad else "Muted.TLabel")

    def log(self, text):
        self.app.log_to("ComfyUI", text)

    def show_log(self, on):
        return  # the log lives in the Log tab now; errors show in the status line

    def show_health(self):
        t, o = self.app.theme, self.online
        self.dot.configure(text="\u25cf  ComfyUI ready" if o else "\u25cb  ComfyUI offline" if o is False else "\u25cb  checking ComfyUI...",
                           foreground=t["good"] if o else t["bad"] if o is False else t["muted"])

    # ---- connection
    def health_loop(self):
        was = None
        while True:
            try:
                self.client.get_json("/system_stats", timeout=3)
                ok = True
            except Exception:
                ok = False
            if ok != was:
                was = ok
                self.q.put(("health", ok))
                if ok:
                    self.fetch_info_now()
            time.sleep(3)

    def fetch_info_now(self):
        try:
            self.q.put(("info", self.client.get_json("/object_info", timeout=120)))
        except Exception as e:
            self.q.put(("log", "could not read the node list from ComfyUI: %s" % e))

    def refresh(self):
        threading.Thread(target=self.fetch_info_now, daemon=True).start()
        for p in self.pages.values():
            p.reload_workflow()
        self.set_status("Reloaded the workflow files.")

    def free_mem(self):
        def work():
            try:
                code, _ = self.client.post_json("/free", {"unload_models": True, "free_memory": True})
                self.q.put(("status", "Asked ComfyUI to free its memory." if code == 200 else "ComfyUI answered HTTP %d to /free." % code))
            except Exception as e:
                self.q.put(("status", "Could not reach ComfyUI: %s" % e, True))
        threading.Thread(target=work, daemon=True).start()

    def start_comfy(self):
        cmd = str(self.cfg.get("comfy_launch", "")).strip()
        if not cmd:
            self.set_status("No launch command set (Settings -> ComfyUI). Start ComfyUI yourself, Forge only needs its address.", True)
            return
        if self.proc is not None and self.proc.poll() is None:
            self.set_status("ComfyUI was already started from here.")
            return
        cwd = Path(str(self.cfg.get("comfy_dir", ""))).expanduser()
        try:
            self.proc = subprocess.Popen(cmd, shell=True, cwd=str(cwd if cwd.is_dir() else HERE), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, text=True, errors="replace", bufsize=1, start_new_session=(os.name == "posix"))
        except Exception as e:
            self.set_status("Could not start ComfyUI: %s" % e, True)
            return
        self.log("$ " + cmd)
        self.set_status("Starting ComfyUI... (output is in the log)")

        def pump(p):
            for line in p.stdout:
                self.q.put(("log", line))
            self.q.put(("log", "[ComfyUI exited with code %s]" % p.wait()))
        threading.Thread(target=pump, args=(self.proc,), daemon=True).start()

    def stop_comfy(self):
        p = self.proc
        if p is None or p.poll() is not None:
            self.set_status("Forge did not start this ComfyUI, so it will not stop it.", True)
            return
        try:
            os.killpg(p.pid, signal.SIGTERM) if os.name == "posix" else p.terminate()
        except Exception:
            pass
        self.set_status("Stopping ComfyUI...")

    def shutdown(self):
        if self._save_job:
            self.parent.after_cancel(self._save_job)
        self.save_state()
        p = self.proc
        if p is not None and p.poll() is None and self.cfg.get("stop_model_on_exit"):
            try:
                os.killpg(p.pid, signal.SIGTERM) if os.name == "posix" else p.terminate()
            except Exception:
                pass

    # ---- pictures
    def quick_places(self):
        h, out = Path.home(), []
        out.append(str(h))
        out += [str(h / n) for n in ("Pictures", "Videos", "Downloads", "Documents", "Desktop") if (h / n).is_dir()]
        out.append(str(Path(str(self.cfg["comfy_out_dir"])).expanduser()))
        for pat in ("run/media/*/*", "media/*/*", "mnt/*"):
            out += [str(p) for p in sorted(Path("/").glob(pat)) if p.is_dir()]
        return list(dict.fromkeys(out))

    def folder_models(self):
        if self._models is None:
            self._models = comfy_scan_models(self.cfg["comfy_models_dir"])
        return self._models

    def checker(self, im):
        bg = Image.new("RGB", im.size, "#2a2a2a")
        d = ImageDraw.Draw(bg)
        for y in range(0, im.height, 16):
            for x in range(0, im.width, 16):
                if (x // 16 + y // 16) % 2:
                    d.rectangle([x, y, x + 15, y + 15], fill="#3b3b3b")
        bg.paste(im, mask=im.getchannel("A"))
        return bg

    def load_image(self, path):
        im = ImageOps.exif_transpose(Image.open(path))
        if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
            return self.checker(im.convert("RGBA"))
        return im.convert("RGB")

    def video_frame(self, path):
        ff = shutil.which("ffmpeg")
        if not ff:
            return None
        d = Path(str(self.cfg["comfy_out_dir"])).expanduser() / ".thumbs"
        try:
            st = os.stat(path)
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        out = d / (hashlib.sha1(("%s|%d|%d" % (path, st.st_size, st.st_mtime)).encode()).hexdigest()[:16] + ".jpg")
        if out.is_file() and out.stat().st_size > 0:
            return str(out)
        for seek in ("0.4", "0"):
            try:
                subprocess.run([ff, "-y", "-loglevel", "error", "-ss", seek, "-i", path, "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2", str(out)],
                               timeout=25, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
            if out.is_file() and out.stat().st_size > 0:
                return str(out)
        return None

    def placeholder(self, kind, size):
        t = self.app.theme
        im = Image.new("RGB", (size, size), t["field"])
        d, c, s = ImageDraw.Draw(im), t["muted"], size
        if kind == "video":
            d.polygon([(s * .38, s * .28), (s * .38, s * .72), (s * .74, s * .5)], fill=c)
        elif kind == "audio":
            for i, hh in enumerate((.25, .5, .38, .6, .3)):
                x = s * (.24 + i * .11)
                d.rectangle([x, s * (.5 - hh / 2), x + s * .06, s * (.5 + hh / 2)], fill=c)
        else:
            d.rectangle([s * .28, s * .3, s * .72, s * .7], outline=t["bad"] if kind == "missing" else c, width=max(2, s // 40))
            d.line([s * .28, s * .3, s * .72, s * .7], fill=t["bad"] if kind == "missing" else c, width=max(2, s // 40))
        return im

    def play_marker(self, im):
        d, (w, h) = ImageDraw.Draw(im), im.size
        r = max(10, min(w, h) // 9)
        cx, cy = w // 2, h // 2
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill="#000000", outline="#ffffff", width=max(2, r // 8))
        d.polygon([(cx - r * .3, cy - r * .5), (cx - r * .3, cy + r * .5), (cx + r * .55, cy)], fill="#ffffff")
        return im

    def thumb_image(self, path, size, kind=None):
        """Square card picture (PIL). Works for pictures, videos (first frame through ffmpeg when available) and audio."""
        kind = kind or comfy_kind(path)
        base, im, vid = Image.new("RGB", (size, size), self.app.theme["panel"]), None, False
        try:
            if kind == "image":
                im = self.load_image(path)
            elif kind == "video":
                fr = self.video_frame(path)
                im, vid = (self.load_image(fr), True) if fr else (None, False)
        except Exception:
            im = None
        if im is None:
            im = self.placeholder(kind, size)
        im = im.copy()
        im.thumbnail((size, size), Image.LANCZOS)
        if vid:
            im = self.play_marker(im)
        base.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
        return base

    def thumb(self, path, size, kind=None):
        key = (path, size, kind)
        p = self.thumbs.get(key)
        if p is None and Image is not None:
            try:
                p = self.thumbs[key] = ImageTk.PhotoImage(self.thumb_image(path, size, kind))
            except Exception:
                p = None
        return p

    def big_image(self, path):
        if comfy_kind(path) == "video":
            fr = self.video_frame(path)
            return self.play_marker(self.load_image(fr)) if fr else self.placeholder("video", 480)
        return self.load_image(path)

    # ---- running
    def run(self, mode):
        if self.busy:
            return
        page = self.pages[mode]
        err = page.validate()
        if err:
            self.set_status(err, True)
            return
        snap = page.snapshot()
        self.busy, self.cancel_flag = True, False
        for p in self.pages.values():
            p.set_busy(True)
        self.btn_cancel.configure(state="normal")
        self.prog.configure(value=0)
        self.set_status("Starting...")
        threading.Thread(target=self.worker, args=(mode, snap), daemon=True).start()

    def cancel(self):
        if not self.busy:
            return
        self.cancel_flag = True
        self.set_status("Cancelling...")

        def work():
            try:
                self.client.post_json("/interrupt", {})
            except Exception:
                pass
        threading.Thread(target=work, daemon=True).start()

    def ws_listen(self, cid, titles):
        stop = threading.Event()

        def loop():
            try:
                ws = ComfyWS(self.client.base(), "/ws?clientId=" + cid)
            except Exception:
                return
            try:
                while not stop.is_set():
                    try:
                        m = ws.recv()
                    except socket.timeout:
                        continue
                    try:
                        j = json.loads(m)
                    except ValueError:
                        continue
                    t, d = j.get("type"), j.get("data") or {}
                    if t == "progress":
                        self.q.put(("progress", d.get("value", 0), d.get("max", 1), titles.get(str(d.get("node")), "")))
                    elif t == "executing" and d.get("node"):
                        self.q.put(("node", titles.get(str(d["node"]), str(d["node"]))))
            except Exception:
                pass
            finally:
                ws.close()
        threading.Thread(target=loop, daemon=True).start()
        return stop

    @staticmethod
    def exec_error(status):
        for m in (status or {}).get("messages") or []:
            if isinstance(m, list) and len(m) > 1 and isinstance(m[1], dict):
                if m[0] == "execution_error":
                    d = m[1]
                    return "%s (node %s) failed: %s %s" % (d.get("node_type", "?"), d.get("node_id", "?"), d.get("exception_type", ""), d.get("exception_message", ""))
                if m[0] == "execution_interrupted":
                    return "interrupted"
        return "ComfyUI reported an error (see its console)."

    def wait_for(self, pid):
        deadline, miss = time.time() + max(60, int(self.cfg.get("comfy_timeout", 7200))), 0
        while True:
            if self.cancel_flag:
                raise Cancelled()
            if time.time() > deadline:
                raise RuntimeError("Gave up waiting for ComfyUI (Settings -> ComfyUI -> Max wait per generation).")
            try:
                h = self.client.get_json("/history/" + pid, timeout=15)
                miss = 0
            except Exception as e:
                miss += 1
                if miss > 20:
                    raise RuntimeError("Lost the connection to ComfyUI: %s" % e)
                time.sleep(1.5)
                continue
            item = h.get(pid) if isinstance(h, dict) else None
            if item:
                st = item.get("status") or {}
                if st.get("status_str") == "error":
                    raise RuntimeError(self.exec_error(st))
                return item.get("outputs") or {}
            time.sleep(0.8)

    def save_outputs(self, mode, outputs):
        items = []
        for o in (outputs or {}).values():
            for lst in (o or {}).values():
                if isinstance(lst, list):
                    items += [it for it in lst if isinstance(it, dict) and it.get("filename")]
        items = [i for i in items if i.get("type") == "output"] or items
        d = Path(str(self.cfg["comfy_out_dir"])).expanduser() / mode
        d.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, it in enumerate(items):
            data = self.client.get_bytes("/view?" + urlencode({"filename": it["filename"], "subfolder": it.get("subfolder", ""), "type": it.get("type", "output")}))
            p = d / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4] + "_%d" % i + (Path(it["filename"]).suffix or ".png"))
            p.write_bytes(data)
            paths.append(str(p))
        return paths

    def worker(self, mode, snap):
        q, cid = self.q, uuid.uuid4().hex
        try:
            wf_path = comfy_find_workflow(self.cfg, mode)
            if wf_path is None:
                raise RuntimeError("Workflow file not found.")
            wf = json.loads(wf_path.read_text("utf-8"))
            uploaded, runs, seed, made = {}, snap["runs"], snap["seed"], 0

            def upload(path):
                if path not in uploaded:
                    q.put(("status", "Uploading %s ..." % os.path.basename(path)))
                    uploaded[path] = self.client.upload(path)
                return uploaded[path]
            for n in range(runs):
                if self.cancel_flag:
                    raise Cancelled()
                if snap["randomize"] or seed is None:
                    seed = random.randint(0, 2 ** 48)
                elif n > 0:
                    seed += 1
                api = comfy_apply(comfy_to_api(wf, self.info), mode, snap, seed, upload)
                titles = {i: nd["_meta"]["title"] for i, nd in api.items()}
                q.put(("run", n + 1, runs))
                q.put(("status", "Run %d/%d: sending the job to ComfyUI..." % (n + 1, runs)))
                ws_stop = self.ws_listen(cid, titles)
                try:
                    pid = self.client.submit(api, cid)
                    q.put(("log", "queued prompt %s (seed %s)" % (pid, seed)))
                    outputs = self.wait_for(pid)
                finally:
                    ws_stop.set()
                paths = self.save_outputs(mode, outputs)
                if not paths:
                    raise RuntimeError("ComfyUI finished but returned no picture or video.")
                made += len(paths)
                q.put(("result", mode, paths, snap["prompt"], seed))
            q.put(("status", "Done - %d file(s) saved in %s" % (made, Path(str(self.cfg["comfy_out_dir"])).expanduser() / mode)))
        except Cancelled:
            q.put(("status", "Cancelled."))
        except Exception as e:
            q.put(("error", str(e)))
        finally:
            if self.cfg.get("comfy_free_after"):
                try:
                    self.client.post_json("/free", {"unload_models": True, "free_memory": True})
                except Exception:
                    pass
            q.put(("idle",))

    def poll(self):
        try:
            while True:
                try:
                    ev = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.handle(ev)
                except Exception as e:
                    self.log("internal error handling %s: %s: %s" % (ev[0], type(e).__name__, e))
        finally:
            self.parent.after(80, self.poll)

    def run_status(self):
        c = self.cur
        bits = ["Run %d/%d" % (c["run"], c["runs"])] if c["runs"] > 1 else []
        bits.append((c["node"] or "working") + ((" " + c["step"]) if c["step"] else ""))
        self.set_status("  \u00b7  ".join(bits))

    def handle(self, ev):
        k = ev[0]
        if k == "health":
            self.online = ev[1]
            self.show_health()
        elif k == "info":
            self.info = ev[1]
            for p in self.pages.values():
                p.refresh_options()
            self.log("ComfyUI node list loaded (%d node types)." % len(self.info))
        elif k == "log":
            self.log(ev[1])
        elif k == "status":
            self.set_status(ev[1], len(ev) > 2 and ev[2])
        elif k == "run":
            self.cur = {"run": ev[1], "runs": ev[2], "node": "", "step": ""}
            self.prog.configure(value=0)
        elif k == "node":
            self.cur["node"], self.cur["step"] = ev[1], ""
            self.run_status()
        elif k == "progress":
            if ev[3]:
                self.cur["node"] = ev[3]
            self.cur["step"] = "%d/%d" % (ev[1], ev[2])
            self.prog.configure(value=100.0 * ev[1] / max(1, ev[2]))
            self.run_status()
        elif k == "result":
            self.pages[ev[1]].add_results(ev[2], ev[3], ev[4])
        elif k == "error":
            self.set_status(ev[1].split("\n")[0], True)
            self.log("ERROR: " + ev[1])
            self.show_log(True)
        elif k == "idle":
            self.busy = False
            for p in self.pages.values():
                p.set_busy(False)
            self.btn_cancel.configure(state="disabled")
            self.prog.configure(value=0)
# <<< COMFYUI END >>>


# <<< GIT BEGIN >>>
# ═════════════════════════ Git tab (patch 5): repositories on the left, the selected one on the right ═════════════════════════
from tkinter import simpledialog


def diff_box(parent, height=12):
    """A read-only text box with both scrollbars, for diffs."""
    box = ttk.Frame(parent)
    box.columnconfigure(0, weight=1)
    box.rowconfigure(0, weight=1)
    t = tk.Text(box, height=height, wrap="none", font="TkFixedFont", state="disabled")
    sy = ttk.Scrollbar(box, orient="vertical", command=t.yview)
    sx = ttk.Scrollbar(box, orient="horizontal", command=t.xview)
    t.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
    t.grid(row=0, column=0, sticky="nsew")
    sy.grid(row=0, column=1, sticky="ns")
    sx.grid(row=1, column=0, sticky="ew")
    return box, t


class GitTab:
    def __init__(self, app, parent):
        self.app, self.eng, self.parent, self.cfg = app, app.eng, parent, app.eng.cfg
        self.q = queue.Queue()
        self.rows, self.g, self.cur, self.gen = [], None, None, 0
        self.dirty, self.status_by_path, self._job = True, {}, None
        self.build(parent)
        self.restyle()
        app.nb.bind("<<NotebookTabChanged>>", self.tab_changed, add="+")
        parent.after(100, self.poll)
        self.refresh()

    # ---- layout
    def build(self, parent):
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(0, weight=1)
        left = ttk.Frame(parent)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        ttk.Label(left, text="REPOSITORIES", style="Section.TLabel").pack(anchor="w", pady=(0, 4))
        self.rtree = ttk.Treeview(left, columns=("branch", "chg"), show="tree headings", height=16, selectmode="browse")
        for c, w, t in (("#0", 210, "Repository"), ("branch", 95, "Branch"), ("chg", 70, "Changes")):
            self.rtree.heading(c, text=t)
            self.rtree.column(c, width=w, stretch=(c == "#0"))
        self.rtree.pack(fill="both", expand=True)
        self.rtree.bind("<<TreeviewSelect>>", self.repo_selected)
        for text, cmd in (("Add repository...", self.add_repo), ("Find repositories in...", self.find_repos), ("Remove from list", self.remove_repo),
                          ("Refresh", self.refresh)):
            ttk.Button(left, text=text, command=cmd).pack(fill="x", pady=(4, 0))
        ttk.Label(left, text="Every finished Forge task is one commit (Settings -> Git). Select two commits in History to compare them.",
                  style="Muted.TLabel", wraplength=340, justify="left").pack(anchor="w", pady=(10, 0))
        right = ttk.Frame(parent)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(2, weight=1)
        self.title = ttk.Label(right, text="Pick a repository on the left", style="Section.TLabel")
        self.title.grid(row=0, column=0, sticky="w")
        self.sub = ttk.Label(right, text="", style="Muted.TLabel", wraplength=860, justify="left")
        self.sub.grid(row=1, column=0, sticky="w", pady=(0, 6))
        self.tabs = ttk.Notebook(right)
        self.tabs.grid(row=2, column=0, sticky="nsew")
        cf, hf, bf = (ttk.Frame(self.tabs, padding=6) for _ in range(3))
        for fr, name in ((cf, "Changes"), (hf, "History"), (bf, "Branches")):
            self.tabs.add(fr, text=name)
        self.build_changes(cf)
        self.build_history(hf)
        self.build_branches(bf)
        bar = ttk.Frame(right)
        bar.grid(row=3, column=0, sticky="ew", pady=(8, 4))
        self.acts = {}
        for key, text, cmd in (("fetch", "Fetch", lambda: self.act("git fetch", lambda g: g.fetch())),
                               ("pull", "Pull", lambda: self.act("git pull --ff-only", lambda g: g.pull())),
                               ("push", "Push", lambda: self.act("git push", lambda g: g.push())),
                               ("stash", "Stash", lambda: self.act("git stash", lambda g: g.stash())),
                               ("pop", "Stash pop", lambda: self.act("git stash pop", lambda g: g.stash_pop())),
                               ("undo", "Undo last commit", self.undo_last),
                               ("init", "Create repository here", self.init_here)):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.acts[key] = b
        ttk.Button(bar, text="Clear output", command=lambda: self.out_w.delete("1.0", "end")).pack(side="right")
        obox, self.out_w = make_log(right, 5)
        obox.grid(row=4, column=0, sticky="ew")

    def build_changes(self, cf):
        cf.columnconfigure(1, weight=1)
        cf.rowconfigure(0, weight=1)
        lf = ttk.Frame(cf)
        lf.grid(row=0, column=0, sticky="ns", padx=(0, 8))
        self.ftree = ttk.Treeview(lf, columns=("ix", "wt"), show="tree headings", height=14, selectmode="extended")
        for c, w, t in (("#0", 300, "File"), ("ix", 62, "Staged"), ("wt", 82, "Not staged")):
            self.ftree.heading(c, text=t)
            self.ftree.column(c, width=w, stretch=(c == "#0"))
        self.ftree.pack(fill="both", expand=True)
        self.ftree.bind("<<TreeviewSelect>>", self.change_selected)
        row = ttk.Frame(lf)
        row.pack(fill="x", pady=(4, 0))
        for text, cmd in (("Stage", lambda: self.stage_sel(True)), ("Unstage", lambda: self.stage_sel(False)), ("Discard...", self.discard_sel),
                          ("Stage all", lambda: self.act("git add -A", lambda g: g.run("add", "-A")))):
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=(0, 4))
        ttk.Button(lf, text="History of this file", command=self.file_history).pack(anchor="w", pady=(4, 0))
        dbox, self.diff_w = diff_box(cf, 14)
        dbox.grid(row=0, column=1, sticky="nsew")
        cm = ttk.Frame(cf)
        cm.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        cm.columnconfigure(1, weight=1)
        ttk.Label(cm, text="Commit message").grid(row=0, column=0, padx=(0, 8))
        self.msg_var, self.all_var = tk.StringVar(), tk.BooleanVar(value=True)
        ttk.Entry(cm, textvariable=self.msg_var).grid(row=0, column=1, sticky="ew")
        ttk.Checkbutton(cm, text="Include all changes", variable=self.all_var).grid(row=0, column=2, padx=8)
        ttk.Button(cm, text="Commit", style="Accent.TButton", command=self.commit).grid(row=0, column=3)

    def build_history(self, hf):
        hf.columnconfigure(0, weight=1)
        hf.rowconfigure(1, weight=1)
        hf.rowconfigure(2, weight=2)
        top = ttk.Frame(hf)
        top.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(top, text="Only versions of this file:").pack(side="left")
        self.hfile = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.hfile, width=44)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda _e: self.hist_reload())
        ttk.Button(top, text="Show", command=self.hist_reload).pack(side="left")
        ttk.Button(top, text="All files", command=lambda: (self.hfile.set(""), self.hist_reload())).pack(side="left", padx=6)
        ttk.Button(top, text="Revert selected commit", command=self.revert_sel).pack(side="right")
        self.htree = ttk.Treeview(hf, columns=("date", "author", "subject"), show="tree headings", height=9, selectmode="extended")
        for c, w, t in (("#0", 80, "Commit"), ("date", 130, "Date"), ("author", 120, "Author"), ("subject", 640, "Message")):
            self.htree.heading(c, text=t)
            self.htree.column(c, width=w, stretch=(c == "subject"))
        self.htree.grid(row=1, column=0, sticky="nsew")
        self.htree.bind("<<TreeviewSelect>>", self.hist_selected)
        hbox, self.hdiff_w = diff_box(hf, 12)
        hbox.grid(row=2, column=0, sticky="nsew", pady=(6, 0))

    def build_branches(self, bf):
        bf.columnconfigure(0, weight=1)
        bf.rowconfigure(0, weight=1)
        self.btree = ttk.Treeview(bf, columns=("cur", "up"), show="tree headings", height=10, selectmode="browse")
        for c, w, t in (("#0", 280, "Branch"), ("cur", 90, ""), ("up", 240, "Tracks")):
            self.btree.heading(c, text=t)
            self.btree.column(c, width=w, stretch=(c == "#0"))
        self.btree.grid(row=0, column=0, sticky="nsew")
        row = ttk.Frame(bf)
        row.grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Button(row, text="Checkout selected", command=self.checkout_sel).pack(side="left", padx=(0, 6))
        ttk.Button(row, text="New branch...", command=self.new_branch).pack(side="left")

    def restyle(self):
        t = self.app.theme
        for w in (self.diff_w, self.hdiff_w):
            for tag, col in (("add", t["good"]), ("del", t["bad"]), ("hunk", t["accent2"]), ("meta", t["muted"]), ("head", t["accent"])):
                w.tag_configure(tag, foreground=col)
        self.ftree.tag_configure("staged", foreground=t["good"])
        self.ftree.tag_configure("untracked", foreground=t["muted"])

    # ---- small helpers
    def out(self, text):
        self.out_w.insert("end", str(text).rstrip("\n") + "\n")
        self.out_w.see("end")
        if int(self.out_w.index("end-1c").split(".")[0]) > 1000:
            self.out_w.delete("1.0", "300.0")

    def fill(self, w, text):
        """Puts text into a read-only box, diff lines coloured."""
        w.configure(state="normal")
        w.delete("1.0", "end")
        if len(text) > 300000:
            text = text[:300000] + "\n...[cut at 300000 characters]"
        meta = ("diff ", "index ", "--- ", "+++ ", "commit ", "Author", "Date", "Commit", "similarity", "rename ", "new file", "deleted file", "Merge")
        for ln in text.split("\n") if text else []:
            tag = ("head" if ln.startswith("### ") else "meta" if ln.startswith(meta) else "hunk" if ln.startswith("@@")
                   else "add" if ln.startswith("+") else "del" if ln.startswith("-") else None)
            w.insert("end", ln + "\n", tag) if tag else w.insert("end", ln + "\n")
        w.configure(state="disabled")

    def set_acts(self, is_repo):
        for k, b in self.acts.items():
            b.configure(state=("normal" if is_repo else "disabled") if k != "init" else ("disabled" if is_repo else "normal"))

    def rows_now(self):
        rows, seen = [], set()
        proj = str(Path(self.cfg["project_root"]).expanduser())
        for p, label in [(proj, "(project) " + (Path(proj).name or proj))] + [(x, Path(x).name or x) for x in self.cfg.get("git_repos", [])]:
            k = os.path.normpath(p)
            if k not in seen:
                seen.add(k)
                rows.append((p, label))
        return rows

    # ---- repository list
    def refresh(self):
        self.rows = rows = self.rows_now()
        self.rtree.delete(*self.rtree.get_children())
        for i, (p, label) in enumerate(rows):
            self.rtree.insert("", "end", iid=str(i), text=label, values=("...", ""))
        keep = next((i for i, (p, _l) in enumerate(rows) if p == self.cur), 0 if rows else None)

        def work():
            for i, (p, _l) in enumerate(rows):
                if not Git.available():
                    vals = ("git missing", "")
                else:
                    top = Git(p).top()
                    vals = ("not a repo", "") if top is None else (Git(top).branch(), str(len(Git(top).status())))
                self.q.put(("list", i, vals))
        threading.Thread(target=work, daemon=True).start()
        if keep is not None:
            self.rtree.selection_set(str(keep))

    def refresh_all(self):
        self.dirty = False
        self.refresh()

    def refresh_soon(self):
        """Called when a queue task ended: refresh now when the tab is open, otherwise when it is opened next."""
        self.dirty = True
        if self.visible() and not self._job:
            self._job = self.parent.after(400, self._do_soon)

    def _do_soon(self):
        self._job = None
        self.refresh_all()

    def visible(self):
        try:
            return self.app.nb.select() == str(self.parent)
        except tk.TclError:
            return False

    def tab_changed(self, _e=None):
        if self.visible() and self.dirty:
            self.refresh_all()

    def repo_selected(self, _e=None):
        s = self.rtree.selection()
        if s and int(s[0]) < len(self.rows):
            self.cur = self.rows[int(s[0])][0]
            self.load(self.cur)

    def load(self, key):
        self.gen += 1
        gen = self.gen

        def work():
            top = Git(key).top() if Git.available() else None
            if top is None:
                self.q.put(("repo", gen, key, None))
                return
            g = Git(top)
            self.q.put(("repo", gen, key, dict(top=str(top), branch=g.branch(), ab=g.ahead_behind(), remotes=g.remotes(), status=g.status(),
                                              log=g.log(None, 80), branches=g.branches())))
        threading.Thread(target=work, daemon=True).start()

    def render(self, key, data):
        for t in (self.ftree, self.htree, self.btree):
            t.delete(*t.get_children())
        self.status_by_path = {}
        self.fill(self.diff_w, "")
        self.fill(self.hdiff_w, "")
        if data is None:
            self.g = None
            self.title.configure(text=key)
            self.sub.configure(text="This folder is not a git repository. Press 'Create repository here' to start one." if Git.available()
                               else "git is not installed (sudo apt install git).")
            self.set_acts(False)
            return
        self.g = Git(data["top"])
        ab = data["ab"]
        rem = data["remotes"].splitlines()[0] if data["remotes"] else "no remote"
        self.title.configure(text=data["top"])
        self.sub.configure(text="branch %s%s    |    %s    |    %d changed file(s)" % (data["branch"], "" if ab is None else "   (ahead %d, behind %d)" % ab,
                                                                                       clip(rem, 90), len(data["status"])))
        for s in data["status"]:
            self.status_by_path[s["path"]] = s
            tags = ("untracked",) if s["x"] == "?" else ("staged",) if s["x"] != " " else ()
            self.ftree.insert("", "end", iid=s["path"], text=s["path"], tags=tags,
                              values=("-" if s["x"] in " ?" else s["x"], "new" if s["x"] == "?" else "-" if s["y"] == " " else s["y"]))
        self.fill_hist(data["log"])
        for name, cur, up in data["branches"]:
            self.btree.insert("", "end", iid=name, text=name, values=("current" if cur else "", up))
        self.set_acts(True)

    # ---- changes
    def change_selected(self, _e=None):
        sel = self.ftree.selection()
        if not sel or self.g is None:
            return
        path, st, parts = sel[0], self.status_by_path.get(sel[0]), []
        if st and st["x"] == "?":
            parts.append(self.g.diff_untracked(path))
        elif st:
            if st["x"] != " ":
                parts.append("### STAGED\n" + self.g.diff(path, staged=True)[1])
            if st["y"] != " ":
                parts.append("### NOT STAGED\n" + self.g.diff(path)[1])
        self.fill(self.diff_w, "\n\n".join(p for p in parts if p.strip()) or "(no textual difference)")

    def stage_sel(self, stage):
        paths = list(self.ftree.selection())
        if paths:
            self.act("git add" if stage else "git reset", lambda g: g.stage(paths) if stage else g.unstage(paths))

    def discard_sel(self):
        paths = list(self.ftree.selection())
        if not paths:
            return

        def fn(g):
            out = []
            for p in paths:
                rc, o = g.discard(p, untracked=self.status_by_path.get(p, {}).get("x") == "?")
                out.append("%s: %s" % (p, "discarded" if rc == 0 else o))
            return 0, "\n".join(out)
        self.act("discard", fn, confirm="Throw away ALL changes in %d file(s) (staged and not staged)?\nNew files are deleted. This cannot be undone." % len(paths))

    def commit(self):
        msg = self.msg_var.get().strip()
        if not msg:
            self.out("Write a commit message first.")
            return
        every = self.all_var.get()

        def fn(g):
            if every:
                rc, o = g.run("add", "-A")
                if rc != 0:
                    return rc, o
            return g.commit(msg)
        self.act("git commit", fn, then=lambda rc: self.msg_var.set("") if rc == 0 else None)

    def file_history(self):
        sel = self.ftree.selection()
        if sel:
            self.hfile.set(sel[0])
            self.hist_reload()
            self.tabs.select(1)

    # ---- history
    def fill_hist(self, rows):
        self.htree.delete(*self.htree.get_children())
        for h in rows:
            self.htree.insert("", "end", iid=h[0], text=h[1], values=(h[2], h[3], clip(h[4], 100)))

    def hist_reload(self):
        if self.g is None:
            return
        self.fill(self.hdiff_w, "")
        self.fill_hist(self.g.log(self.hfile.get().strip() or None, 80))

    def hist_selected(self, _e=None):
        sel = list(self.htree.selection())
        if not sel or self.g is None:
            return
        path = self.hfile.get().strip()
        order = list(self.htree.get_children())
        sel.sort(key=order.index)  # newest first
        if len(sel) == 1:
            rc, out = self.g.run(*(["show", "--no-color", "--format=fuller", "--stat", "--patch", sel[0]] + (["--", path] if path else [])))
        else:
            newer, older = sel[0], sel[-1]
            rc, out = self.g.run(*(["diff", "--no-color", "--stat", "--patch", older, newer] + (["--", path] if path else [])))
            out = "### From %s to %s\n%s" % (older[:8], newer[:8], out)
        self.fill(self.hdiff_w, out)

    def revert_sel(self):
        sel = self.htree.selection()
        if len(sel) == 1:
            self.act("git revert " + sel[0][:8], lambda g: g.revert_commit(sel[0]),
                     confirm="Undo commit %s with a new commit?" % sel[0][:8])
        else:
            self.out("Select exactly one commit to revert.")

    def undo_last(self):
        self.act("git reset --soft HEAD~1", lambda g: g.undo_last_commit(),
                 confirm="Undo the last commit? Its changes stay in your files (staged).")

    # ---- branches
    def checkout_sel(self):
        s = self.btree.selection()
        if s:
            self.act("git checkout " + s[0], lambda g: g.checkout(s[0]))

    def new_branch(self):
        if self.g is None:
            return
        name = simpledialog.askstring("New branch", "Name of the new branch (created from the current one):", parent=self.parent)
        if name and _BRANCH_RX.match(name.strip()):
            self.act("git checkout -b " + name.strip(), lambda g: g.new_branch(name.strip()))
        elif name:
            self.out("That is not a valid branch name (letters, digits, . _ - /).")

    # ---- running git without blocking the window
    def act(self, label, fn, confirm=None, then=None, repo=True):
        g = self.g if repo else Git(self.cur)
        if g is None or (repo and self.cur is None):
            return
        if confirm and not messagebox.askyesno(label, confirm):
            return
        self.out("$ " + label)

        def work():
            try:
                rc, out = fn(g)
            except Exception as e:
                rc, out = 1, "%s: %s" % (type(e).__name__, e)
            self.q.put(("done", label, rc, out, then))
        threading.Thread(target=work, daemon=True).start()

    def init_here(self):
        if self.cur is None:
            return
        self.act("git init", lambda g: g.init(self.eng.git_ignore_lines()), repo=False)

    # ---- repository list actions
    def add_paths(self, paths):
        have = {os.path.normpath(p) for p, _l in self.rows_now()}
        new = [p for p in paths if os.path.normpath(p) not in have]
        if new:
            self.cfg.setdefault("git_repos", []).extend(new)
            self.eng.save()
            self.cur = new[-1]
            self.refresh()
        return len(new)

    def add_repo(self):
        d = filedialog.askdirectory(parent=self.parent, title="Choose a repository (or a folder to turn into one)")
        if not d:
            return
        top = Git(d).top()
        if top is None:
            if not messagebox.askyesno("Not a repository", "%s is not a git repository.\nCreate one there with git init?" % d):
                return
            rc, o = Git(d).init(self.eng.git_ignore_lines())
            self.out("$ git init " + d + "\n" + o)
            top = Git(d).top() or Path(d)
        if not self.add_paths([str(top)]):
            self.out("%s is already in the list." % top)

    def find_repos(self):
        d = filedialog.askdirectory(parent=self.parent, title="Scan this folder (4 levels deep) for repositories")
        if not d:
            return
        self.out("Scanning %s ..." % d)

        def work():
            found, base = [], len(Path(d).parts)
            for dp, dns, fns in os.walk(d):
                if ".git" in dns or ".git" in fns:
                    found.append(dp)
                    dns[:] = []
                    continue
                dns[:] = [x for x in dns if not x.startswith(".") and x not in SEARCH_SKIP and not x.endswith("~")] if len(Path(dp).parts) - base < 4 else []
                if len(found) >= 200:
                    break
            self.q.put(("found", d, found))
        threading.Thread(target=work, daemon=True).start()

    def remove_repo(self):
        s = self.rtree.selection()
        if not s:
            return
        i = int(s[0])
        if i == 0:
            self.out("The project folder always stays in the list (change it in Settings).")
            return
        p = self.rows[i][0]
        self.cfg["git_repos"] = [x for x in self.cfg.get("git_repos", []) if os.path.normpath(x) != os.path.normpath(p)]
        self.eng.save()
        self.cur = None
        self.refresh()

    # ---- events from the worker threads
    def poll(self):
        try:
            while True:
                try:
                    ev = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.handle(ev)
                except Exception as e:
                    self.out("internal error handling %s: %s: %s" % (ev[0], type(e).__name__, e))
        finally:
            self.parent.after(100, self.poll)

    def handle(self, ev):
        k = ev[0]
        if k == "list":
            if self.rtree.exists(str(ev[1])):
                self.rtree.item(str(ev[1]), values=ev[2])
        elif k == "repo":
            if ev[1] == self.gen:
                self.render(ev[2], ev[3])
        elif k == "done":
            _k, label, rc, out, then = ev
            self.out(out if out.strip() else ("done" if rc == 0 else "failed (exit %d)" % rc))
            if then:
                then(rc)
            self.refresh()
        elif k == "found":
            n = self.add_paths(ev[2])
            self.out("Found %d repositories in %s, %d new." % (len(ev[2]), ev[1], n))
# <<< GIT END >>>


def main():
    if tk is None:
        print("tkinter is missing. On Kubuntu: sudo apt install python3-tk")
        sys.exit(1)
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
