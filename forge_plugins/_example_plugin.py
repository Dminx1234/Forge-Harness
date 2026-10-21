"""Example plugin: one tool and one hook. Names starting with _ are ignored; copy to a new name to use it."""
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
