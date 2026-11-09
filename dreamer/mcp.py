"""`dreamer mcp`: the same memory over the Model Context Protocol (stdio).

For clients that cannot read the repository's files or instruction files -
chat apps such as Claude Desktop - and for any MCP-capable agent.

Scope:
- Started for a repository (`--repo-dir`, or from inside one) that ran
  `dreamer init`: that project's memory, plus personal memory if the
  repository opted in to it.
- Started outside any such repository (the usual chat-app case): personal
  memory only, if it exists.
A repository that did not opt in to personal memory never exposes it.

Pages are read-only here. Writing goes through `memory_note`, which leaves a
note for the next dreaming pass, so new memory is consolidated, de-duplicated,
checked against budgets and validated like everything else instead of
being appended by whichever agent spoke last.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dreamer import __version__, notes, personal, repo, store
from dreamer.config import DreamerConfig

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SEARCH_LIMIT = 4000

INSTRUCTIONS = (
    "This server holds long-term memory distilled from the user's past sessions with AI agents. Read memory_recall "
    "before relying on assumptions about the user or the project; open a linked page with memory_read when its topic "
    "matters. When you learn something a future session would need (a decision, a preference, how a tool really "
    "behaves), save it with memory_note; it is consolidated later. Notes are predictions, not guarantees."
)


class Scope:
    def __init__(self, repo_dir: Optional[Path]):
        self.project_root: Optional[Path] = None
        self.project_dir: Optional[Path] = None
        if repo_dir is not None:
            root = repo.find_root(repo_dir)
            cfg = DreamerConfig.load(root)
            if (root / cfg.memory.dir / store.INDEX).is_file() and not personal.is_root(root):
                self.project_root, self.project_dir = root, root / cfg.memory.dir
        if self.project_root is not None:
            self.personal = personal.enabled(self.project_root)
        else:
            self.personal = (personal.store_dir() / store.INDEX).is_file()

    def stores(self) -> List[Tuple[str, Path]]:
        out = []
        if self.project_dir is not None:
            out.append(("", self.project_dir))
        if self.personal:
            out.append(("personal/", personal.store_dir()))
        return out


def _page_path(scope: Scope, page: str) -> Path:
    page = page.strip().lstrip("/")
    page = re.sub(r"^memories/", "", page)
    if page.startswith("personal/"):
        if not scope.personal:
            raise ValueError("personal memory is not available here")
        base, rel = personal.store_dir(), page[len("personal/"):]
    else:
        if scope.project_dir is None:
            raise ValueError("no project memory here; personal pages start with personal/")
        base, rel = scope.project_dir, page
    tool = store.MemoryTool(base, DreamerConfig().memory)  # used for its path confinement
    return tool._resolve(store.VIRTUAL_ROOT + "/" + rel)


def recall(scope: Scope) -> str:
    parts = []
    if scope.project_dir is not None:
        parts.append(f"# Project memory ({scope.project_root.name})\n"
                     + _strip_h1((scope.project_dir / store.INDEX).read_text(encoding="utf-8")))
    if scope.personal:
        parts.append("# Personal memory (pages: personal/<file>)\n"
                     + _strip_h1((personal.store_dir() / store.INDEX).read_text(encoding="utf-8")))
    return "\n\n".join(parts) or "No memory is available from here yet."


def _strip_h1(text: str) -> str:
    return re.sub(r"\A\s*# [^\n]*\n?", "", text).strip() or "(empty)"


def read(scope: Scope, page: str) -> str:
    p = _page_path(scope, page)
    if not p.is_file():
        raise ValueError(f"{page} does not exist")
    return p.read_text(encoding="utf-8")


def search(scope: Scope, query: str) -> str:
    try:
        rx = re.compile(query, re.IGNORECASE)
    except re.error:
        rx = re.compile(re.escape(query), re.IGNORECASE)
    hits, total = [], 0
    for prefix, base in scope.stores():
        for p in store.iter_files(base):
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hit = f"{prefix}{p.relative_to(base).as_posix()}:{n}: {line.strip()}"
                    hits.append(hit)
                    total += len(hit)
                    if total > SEARCH_LIMIT:
                        return "\n".join(hits)
    return "\n".join(hits) or "No matching lines in memory."


def note(scope: Scope, text: str, about: str, client: str) -> str:
    about = about or ("project" if scope.project_root is not None else "me")
    if about == "me":
        if not scope.personal and scope.project_root is not None:
            raise ValueError("this repository has not opted in to personal memory")
        personal.ensure_store()
        notes.add(personal.home(), text, client)
        return "Saved for the user's personal memory; it is consolidated on the next dreaming pass."
    if scope.project_root is None:
        raise ValueError("no project here; use about=\"me\" for something about the user")
    notes.add(scope.project_root, text, client)
    return "Saved for this project's memory; it is consolidated on the next dreaming pass."


TOOLS = [
    {"name": "memory_recall", "description": "The memory index: the facts and rules learned in past sessions that "
     "matter most often, with links to topic pages. Read it before relying on assumptions about the user or the project.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "memory_read", "description": "Open one memory page linked from the index, e.g. \"build.md\" or "
     "\"personal/style.md\".",
     "inputSchema": {"type": "object", "properties": {"page": {"type": "string"}}, "required": ["page"],
                     "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "memory_search", "description": "Find lines in memory pages matching a regex or text.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
                     "additionalProperties": False},
     "annotations": {"readOnlyHint": True}},
    {"name": "memory_note", "description": "Save something a future session would need: a decision, a preference, "
     "or how a tool or service actually behaves, with why. about=\"me\" for the user themself (shared across their "
     "projects), \"project\" for this project. Never save secrets.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"},
                                                       "about": {"type": "string", "enum": ["me", "project"]}},
                     "required": ["text"], "additionalProperties": False}},
]


def handle(scope: Scope, msg: Dict[str, Any], state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:  # notification
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        params = msg.get("params") or {}
        asked = params.get("protocolVersion")
        state["client"] = re.sub(r"[^A-Za-z0-9_.-]", "", str((params.get("clientInfo") or {}).get("name", "mcp")))[:32]
        return ok({"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                   "capabilities": {"tools": {}}, "serverInfo": {"name": "dreamer", "version": __version__},
                   "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        try:
            if name == "memory_recall":
                text = recall(scope)
            elif name == "memory_read":
                text = read(scope, str(args.get("page", "")))
            elif name == "memory_search":
                text = search(scope, str(args.get("query", "")))
            elif name == "memory_note":
                text = note(scope, str(args.get("text", "")), str(args.get("about", "")), state.get("client", "mcp"))
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
            return ok({"content": [{"type": "text", "text": text}], "isError": False})
        except (ValueError, store.ToolError, OSError) as e:
            return ok({"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def serve(repo_dir: Optional[Path], stdin=None, stdout=None) -> None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    scope, state = Scope(repo_dir), {}
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            out = handle(scope, msg, state) if isinstance(msg, dict) else None
        if out is not None:
            stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
            stdout.flush()


def client_config(repo_dir: Optional[Path]) -> str:
    args = ["mcp"] + (["--repo-dir", str(repo_dir)] if repo_dir else [])
    return json.dumps({"mcpServers": {"dreamer": {"command": "dreamer", "args": args}}}, indent=2)
