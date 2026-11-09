import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from dreamer import dream, install, notes, personal, repo, store, targets, transcripts
from dreamer.config import DreamerConfig
from dreamer.llm import DreamError


def rec(cwd, kind, content, sid="sess-aaaa1111", ts="2026-11-01T10:00:00Z", **extra):
    return {"type": kind, "cwd": str(cwd), "sessionId": sid, "timestamp": ts, "gitBranch": "main",
            "message": {"role": kind, "content": content}, **extra}


def text(t):
    return NS(type="text", text=t)


def tool(name, inp, i):
    return NS(type="tool_use", id=f"tu{i}", name=name, input=inp)


def resp(*blocks, stop="end_turn"):
    return NS(stop_reason=stop, content=list(blocks),
              usage=NS(input_tokens=100, output_tokens=10, cache_read_input_tokens=50, cache_creation_input_tokens=0))


class FakeClient:
    """Stands in for anthropic.Anthropic(); replays scripted responses."""

    def __init__(self, script, on_call=None):
        self.script = list(script)
        self.requests = []
        self.on_call = on_call
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        if self.on_call:
            self.on_call(len(self.requests))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item

        @contextlib.contextmanager
        def ctx():
            yield NS(get_final_message=lambda: item)
        return ctx()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp()))
        self.claude = self.tmp / "claude"
        self.root = self.tmp / "work" / "proj"
        self.other = self.tmp / "work" / "proj-other"  # slug-prefix collision with root
        for r in (self.root, self.other):
            r.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=r, check=True)
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.claude)})
        self.env.start()
        self.cfg = DreamerConfig.load(self.root)
        self.cfg.dream.min_new_chars = 50
        install.init(self.root, self.cfg, harnesses=["claude", "agents"])

    def tearDown(self):
        self.env.stop()
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def transcript(self, repo_root, name, records, partial=None):
        d = self.claude / "projects" / transcripts.slug(repo_root)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{name}.jsonl"
        with open(p, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
            if partial:
                f.write(partial)
        return p

    def basic_session(self, repo_root, marker, sid="sess-aaaa1111"):
        return [
            rec(repo_root, "user", f"Please fix the build. {marker} <system-reminder>SECRET-REMINDER</system-reminder>", sid),
            rec(repo_root, "assistant", [{"type": "thinking", "thinking": "THINKING-" * 2000},
                                         {"type": "text", "text": f"Found it: {marker} needs pnpm."},
                                         {"type": "tool_use", "id": "x", "name": "Bash", "input": {"command": "pnpm build"}}], sid),
            rec(repo_root, "user", [{"type": "tool_result", "tool_use_id": "x", "content": "OUTPUT " * 5000}], sid),
            rec(repo_root, "user", [{"type": "tool_result", "tool_use_id": "y", "is_error": True, "content": "Error: ENOENT lockfile"}], sid),
            rec(repo_root, "user", "meta text", sid, isMeta=True),
        ]

    def run_pass(self, script, **kw):
        client = FakeClient(script, kw.pop("on_call", None))
        res = dream.run(self.root, self.cfg, client_factory=lambda: client, **kw)
        return res, client

    def state(self):
        return repo.load_state(repo.state_dir(self.root))


class TestTranscripts(Base):
    def test_digest_is_scoped_compact_and_clean(self):
        p = self.transcript(self.root, "s1", self.basic_session(self.root, "MINE"))
        self.transcript(self.other, "s2", self.basic_session(self.other, "THEIRS"))
        # A record from another repo inside this repo's transcript dir is dropped too.
        self.transcript(self.root, "s3", [rec(self.other, "user", "THEIRS-IN-MY-DIR " * 10)])
        b = transcripts.collect(self.root, self.cfg, {}, 100000)
        self.assertIn("MINE", b.text)
        self.assertIn("ENOENT lockfile", b.text)
        self.assertIn("Bash: command=pnpm build", b.text)
        for leaked in ("THEIRS", "SECRET-REMINDER", "THINKING", "meta text"):
            self.assertNotIn(leaked, b.text)
        self.assertLess(len(b.text) * 20, p.stat().st_size, "digest should be far smaller than the raw transcript")

    def test_commit_messages_kept_without_trailers(self):
        msg = "fix: packaging\n\nAdd explicit packages so extra top-level dirs are fine. " + "detail " * 40
        cmd = f"git add -A && git commit -q -F - <<'EOF'\n{msg}\n\nCo-Authored-By: X <x@y>\nEOF"
        self.transcript(self.root, "s1", [rec(self.root, "assistant", [
            {"type": "tool_use", "id": "c", "name": "Bash", "input": {"command": cmd, "description": "Commit"}},
            {"type": "tool_use", "id": "d", "name": "Bash", "input": {"command": "ls " + "x" * 400}}])])
        b = transcripts.collect(self.root, self.cfg, {}, 100000)
        self.assertIn("extra top-level dirs are fine", b.text)
        self.assertIn("detail " * 39 + "detail", b.text)
        self.assertNotIn("Co-Authored-By", b.text)
        self.assertIn("…[+", b.text, "ordinary long commands are still clipped")

    def test_commit_message_extraction(self):
        cm = transcripts.commit_message
        self.assertEqual(cm("cd x && git add a && git commit -qm init"), "init")
        self.assertEqual(cm('git commit -m "feat: a \\"b\\""'), 'feat: a \\"b\\"')
        self.assertEqual(cm("git -C r commit -F - <<'EOF'\nfix: y\n\nbody\nCo-Authored-By: Z\nEOF\n"), "fix: y\n\nbody")
        self.assertEqual(cm("git log --oneline | grep commit"), "")
        self.assertEqual(cm("git commit -m \"$(cat <<'EOF'\nfeat: z\n\nwhy\nEOF\n)\""), "feat: z\n\nwhy")
        self.assertEqual(cm("git commit -qm init && cat > f <<'EOF'\nnot a message\nEOF"), "init")
        # A long setup script that merely contains a short commit keeps only that message.
        script = "mkdir t && " + "echo setup && " * 100 + "git commit -qm init && python3 - <<'PY'\n" + "x=1\n" * 200 + "PY"
        self.transcript(self.root, "s2", [rec(self.root, "assistant", [
            {"type": "tool_use", "id": "e", "name": "Bash", "input": {"command": script}}])])
        b = transcripts.collect(self.root, self.cfg, {}, 100000)
        self.assertLess(len(b.text), 400)

    def test_cursor_is_incremental_and_skips_partial_lines(self):
        self.transcript(self.root, "s1", self.basic_session(self.root, "FIRST"), partial='{"type": "user"')
        b1 = transcripts.collect(self.root, self.cfg, {}, 100000)
        cursors = b1.cursors
        p = self.transcript(self.root, "s1", [])
        with open(p, "a") as f:  # finish the partial line, add a new record
            f.write(', "cwd": "/nowhere"}\n' + json.dumps(rec(self.root, "user", "SECOND message here")) + "\n")
        b2 = transcripts.collect(self.root, self.cfg, cursors, 100000)
        self.assertIn("SECOND", b2.text)
        self.assertNotIn("FIRST", b2.text)

    def test_batch_budget_leaves_rest_pending(self):
        for i in range(5):
            self.transcript(self.root, f"s{i}", [rec(self.root, "user", f"MSG{i} " + "x" * 900, sid=f"sess-{i}")])
        b = transcripts.collect(self.root, self.cfg, {}, 2500)
        self.assertTrue(b.pending)
        self.assertLessEqual(len(b.text), 2600)

    def test_search_is_scoped(self):
        self.transcript(self.root, "s1", self.basic_session(self.root, "NEEDLE-MINE"))
        self.transcript(self.other, "s2", self.basic_session(self.other, "NEEDLE-THEIRS"))
        out = transcripts.search(self.root, self.cfg, "needle")
        self.assertIn("NEEDLE-MINE", out)
        self.assertNotIn("NEEDLE-THEIRS", out)


class TestStore(Base):
    def tool(self):
        return store.MemoryTool(self.root / self.cfg.memory.dir, self.cfg.memory)

    def test_paths_are_confined(self):
        t = self.tool()
        (self.tmp / "outside.md").write_text("x")
        os.symlink(self.tmp, self.root / "memory" / "link")
        for path in ("/memories/../../outside.md", "/etc/passwd", "/memories/link/outside.md", "/memories/.hidden.md"):
            with self.assertRaises(store.ToolError, msg=path):
                t.execute({"command": "create", "path": path, "file_text": "x"})
        with self.assertRaises(store.ToolError):
            t.execute({"command": "create", "path": "/memories/notes.txt", "file_text": "x"})

    def test_budgets_enforced(self):
        t, m = self.tool(), self.cfg.memory
        with self.assertRaises(store.ToolError):
            t.execute({"command": "create", "path": "/memories/MEMORY.md", "file_text": "- x\n" * (m.index_max_lines + 1)})
        with self.assertRaises(store.ToolError):
            t.execute({"command": "create", "path": "/memories/big.md", "file_text": "x" * (m.topic_max_bytes + 1)})
        m.max_files = 3
        t.execute({"command": "create", "path": "/memories/a.md", "file_text": "a"})
        t.execute({"command": "create", "path": "/memories/b.md", "file_text": "b"})
        with self.assertRaises(store.ToolError):
            t.execute({"command": "create", "path": "/memories/c.md", "file_text": "c"})
        with self.assertRaises(store.ToolError):
            t.execute({"command": "delete", "path": "/memories/MEMORY.md"})

    def test_validate_finds_broken_links_and_orphans(self):
        mem = self.root / "memory"
        (mem / "MEMORY.md").write_text("# M\n- [build](build.md)\n- [gone](gone.md#x)\n- [web](https://x.io/a.md)\n")
        (mem / "build.md").write_text("# build\nsee [db](sub/db.md)\n")
        (mem / "sub").mkdir()
        (mem / "sub" / "db.md").write_text("# db\n")
        (mem / "orphan.md").write_text("# lonely\n")
        issues = store.validate(mem, self.cfg.memory)
        self.assertEqual(sorted(issues), sorted([
            "broken link in MEMORY.md: gone.md#x",
            "unreachable page (nothing links to it): orphan.md",
        ]))


class TestDream(Base):
    def edit_script(self):
        return [
            resp(tool("memory", {"command": "create", "path": "/memories/build.md",
                                 "file_text": "# Build\n- Use pnpm, not npm (2026-11-01, s:sess-aaa)\n"}, 1), stop="tool_use"),
            resp(tool("search_transcripts", {"pattern": "pnpm"}, 2),
                 tool("memory", {"command": "str_replace", "path": "/memories/MEMORY.md", "old_str": "# Project memory\n",
                                 "new_str": "# Project memory\n- [build](build.md): pnpm only\n"}, 3), stop="tool_use"),
            resp(text("Added build rule.")),
        ]

    def test_below_threshold_makes_no_api_call(self):
        self.cfg.dream.min_new_chars = 3000
        self.transcript(self.root, "s1", [rec(self.root, "user", "hi")])
        called = []
        res = dream.run(self.root, self.cfg, auto=True, client_factory=lambda: called.append(1))
        self.assertEqual(res.status, "skipped")
        self.assertEqual(called, [])
        self.assertEqual(self.state()["files"], {}, "cursor must not advance so material accumulates")

    def test_applies_snapshots_and_undo(self):
        self.transcript(self.root, "s1", self.basic_session(self.root, "MINE"))
        res, client = self.run_pass(self.edit_script())
        self.assertEqual(res.status, "applied")
        mem = self.root / "memory"
        self.assertIn("pnpm only", (mem / "MEMORY.md").read_text())
        self.assertTrue((mem / "build.md").exists())
        self.assertEqual(store.validate(mem, self.cfg.memory), [])
        # The search tool result reached the model and was scoped to this repo.
        results = client.requests[2]["messages"][-1]["content"]
        self.assertIn("pnpm", results[0]["content"])
        # Cached, stable prefix: system prompt and tools identical across turns.
        self.assertEqual(len({r["system"] for r in client.requests}), 1)
        self.assertNotIn("temperature", client.requests[0])
        st = self.state()
        self.assertTrue(st["files"])
        self.assertEqual(st["runs"][-1]["usage"]["cache_read_input_tokens"], 150)

        # Nothing new -> no pass.
        self.assertEqual(self.run_pass([])[0].status, "skipped")

        self.assertIsNotNone(dream.undo(self.root, self.cfg))
        self.assertEqual((mem / "MEMORY.md").read_text(), "# Project memory\n")
        self.assertFalse((mem / "build.md").exists())
        self.assertEqual(self.state()["files"], {}, "undo rewinds the cursor so the sessions are dreamed again")

    def test_fail_closed_and_backoff(self):
        self.transcript(self.root, "s1", self.basic_session(self.root, "MINE"))
        mem = self.root / "memory"
        before = store.tree_hash(mem)
        failures = [
            [resp(tool("memory", {"command": "create", "path": "/memories/x.md", "file_text": "x"}, 1), stop="tool_use"),
             resp(text("partial"), stop="max_tokens")],
            [RuntimeError("connection reset")],
            [resp(text("no"), stop="refusal")],
        ]
        for script in failures:
            with self.assertRaises(DreamError):
                self.run_pass(script)
            self.assertEqual(store.tree_hash(mem), before)
            self.assertEqual(self.state()["files"], {})
        self.assertEqual(self.state()["failures"], 3)
        res, _ = self.run_pass([], auto=True)
        self.assertEqual(res.status, "skipped")
        self.assertIn("paused", res.message)
        res, _ = self.run_pass(self.edit_script())  # a manual run resets the backoff
        self.assertEqual(res.status, "applied")
        self.assertEqual(self.state()["failures"], 0)

    def test_turn_limit_aborts(self):
        self.transcript(self.root, "s1", self.basic_session(self.root, "MINE"))
        self.cfg.model.max_turns = 2
        loop = [resp(tool("memory", {"command": "view", "path": "/memories"}, i), stop="tool_use") for i in range(3)]
        with self.assertRaises(DreamError):
            self.run_pass(loop)

    def test_concurrent_edit_aborts(self):
        self.transcript(self.root, "s1", self.basic_session(self.root, "MINE"))
        idx = self.root / "memory" / "MEMORY.md"

        def agent_writes(n):
            if n == 2:
                idx.write_text("# Project memory\n- written by a live session\n")
        with self.assertRaises(DreamError):
            self.run_pass(self.edit_script(), on_call=agent_writes)
        self.assertIn("live session", idx.read_text())

    def test_tool_errors_are_reported_not_fatal(self):
        self.transcript(self.root, "s1", self.basic_session(self.root, "MINE"))
        script = [
            resp(tool("memory", {"command": "create", "path": "/memories/../../evil.md", "file_text": "x"}, 1), stop="tool_use"),
            resp(text("Nothing to change.")),
        ]
        res, client = self.run_pass(script)
        self.assertEqual(res.status, "unchanged")
        err = client.requests[1]["messages"][-1]["content"][0]
        self.assertTrue(err["is_error"])
        self.assertFalse((self.tmp / "work" / "evil.md").exists())
        self.assertTrue(self.state()["files"], "an unchanged pass still consumes the material")

    def test_dry_run_changes_nothing(self):
        self.transcript(self.root, "s1", self.basic_session(self.root, "MINE"))
        res, _ = self.run_pass(self.edit_script(), dry_run=True)
        self.assertEqual(res.status, "dry-run")
        self.assertIn("pnpm only", res.message)
        self.assertFalse((self.root / "memory" / "build.md").exists())
        self.assertEqual(self.state()["files"], {})


class TestInstall(Base):
    def snapshot_tree(self, d):
        return {str(p.relative_to(d)): p.read_bytes() for p in d.rglob("*") if p.is_file()} if d.exists() else {}

    def test_init_stays_inside_repo_and_is_idempotent(self):
        home = self.tmp / "home"
        home.mkdir()
        (self.root / "CLAUDE.md").write_text("# Team rules\nKeep it tidy.\n")
        settings = self.root / ".claude" / "settings.local.json"
        settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
        outside_before = {d: self.snapshot_tree(d) for d in (home, self.other, self.claude)}
        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            install.init(self.root, self.cfg, git_hook=True)
            install.init(self.root, self.cfg, git_hook=True)
        for d, snap in outside_before.items():
            self.assertEqual(self.snapshot_tree(d), snap, f"init touched {d}")

        md = (self.root / "CLAUDE.md").read_text()
        self.assertTrue(md.startswith("# Team rules\nKeep it tidy."))
        self.assertEqual(md.count(targets.BLOCK_BEGIN), 1)
        self.assertIn("@memory/MEMORY.md", md)
        data = json.loads(settings.read_text())
        self.assertEqual(data["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(len(data["hooks"]["SessionEnd"]), 1)
        exclude = (self.root / ".git" / "info" / "exclude").read_text()
        self.assertIn(".claude/settings.local.json", exclude)

        install.uninstall(self.root)
        self.assertEqual((self.root / "CLAUDE.md").read_text(), "# Team rules\nKeep it tidy.\n")
        self.assertNotIn("hooks", json.loads(settings.read_text()))
        self.assertFalse((self.root / ".git" / "hooks" / "post-commit").exists())
        self.assertTrue((self.root / "memory" / "MEMORY.md").exists())

    def test_existing_git_hook_is_kept(self):
        hook = self.root / ".git" / "hooks" / "post-commit"
        hook.write_text("#!/bin/sh\necho mine\n")
        out = install.init(self.root, self.cfg, git_hook=True)
        self.assertIn("mine", hook.read_text())
        self.assertTrue(any("skipped" in o for o in out))

    def test_legacy_layout_migrates(self):
        (self.root / "memory" / "MEMORY.md").unlink()
        (self.root / "MEMORY.md").write_text("# Old\n- [db](memory/db.md)\n")
        (self.root / "memory" / "db.md").write_text("# db\n")
        install.init(self.root, self.cfg, harnesses=["claude", "agents"])
        self.assertFalse((self.root / "MEMORY.md").exists())
        self.assertIn("[db](db.md)", (self.root / "memory" / "MEMORY.md").read_text())
        self.assertEqual(store.validate(self.root / "memory", self.cfg.memory), [])


def codex_rec(t, payload, ts="2026-11-09T10:00:00Z"):
    return {"timestamp": ts, "type": t, "payload": payload}


class TestCodex(Base):
    def rollout(self, cwd, sid, items, name=None):
        d = self.codex / "sessions" / "2026" / "11" / "09"
        d.mkdir(parents=True, exist_ok=True)
        p = d / (name or f"rollout-2026-11-09T10-00-00-{sid}.jsonl")
        with open(p, "a", encoding="utf-8") as f:
            for r in [codex_rec("session_meta", {"id": sid, "cwd": str(cwd), "timestamp": "x", "git": {"branch": "dev"}})] + items:
                f.write(json.dumps(r) + "\n")
        return p

    def setUp(self):
        super().setUp()
        self.codex = self.tmp / "codex"
        self.env2 = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex)})
        self.env2.start()

    def tearDown(self):
        self.env2.stop()
        super().tearDown()

    def session(self, marker):
        return [
            codex_rec("turn_context", {"cwd": str(self.root)}),
            codex_rec("response_item", {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "# AGENTS.md instructions for proj\nINJECTED-RULES"},
                {"type": "input_text", "text": "<environment_context><cwd>/x</cwd></environment_context>"},
                {"type": "input_text", "text": f"Use uv, not pip. {marker}"}]}),
            codex_rec("response_item", {"type": "reasoning", "summary": [], "encrypted_content": "REASONING-SECRET"}),
            codex_rec("response_item", {"type": "message", "role": "assistant", "content": [
                {"type": "output_text", "text": f"Switched to uv. {marker}"}]}),
            codex_rec("response_item", {"type": "function_call", "name": "shell", "call_id": "c1",
                                        "arguments": json.dumps({"command": ["bash", "-lc", 'git commit -m "build: use uv because pip resolver breaks"']})}),
            codex_rec("response_item", {"type": "function_call_output", "call_id": "c1",
                                        "output": json.dumps({"output": "error: lockfile stale", "metadata": {"exit_code": 1}})}),
            codex_rec("response_item", {"type": "custom_tool_call", "name": "apply_patch", "call_id": "c2",
                                        "input": "*** Begin Patch\n*** Update File: pyproject.toml\n@@\n-x\n+y\n*** End Patch"}),
            codex_rec("response_item", {"type": "custom_tool_call_output", "call_id": "c2", "output": "Done!"}),
        ]

    def test_codex_digest_is_scoped_and_clean(self):
        self.rollout(self.root, "codex-mine-1", self.session("MINE"))
        foreign = self.rollout(self.other, "codex-other-1", self.session("THEIRS"))
        b = transcripts.collect(self.root, self.cfg, {}, 100000)
        for want in ("USER: Use uv, not pip. MINE", "ASSISTANT: Switched to uv.", "commit message: build: use uv because pip resolver breaks",
                     "✗ error: lockfile stale", "apply_patch: files=pyproject.toml", "· codex", "branch dev"):
            self.assertIn(want, b.text)
        for leaked in ("THEIRS", "INJECTED-RULES", "environment_context", "REASONING-SECRET"):
            self.assertNotIn(leaked, b.text)
        # The other repo's session is marked foreign after its header and never read again.
        entry = b.cursors[str(foreign)]
        self.assertTrue(entry["ctx"]["foreign"])
        self.assertEqual(entry["offset"], foreign.stat().st_size)
        self.assertIn("MINE", transcripts.search(self.root, self.cfg, "uv"))
        self.assertNotIn("THEIRS", transcripts.search(self.root, self.cfg, "uv"))

    def test_codex_cursor_keeps_session_cwd(self):
        p = self.rollout(self.root, "codex-mine-2", self.session("FIRST"))
        b1 = transcripts.collect(self.root, self.cfg, {}, 100000)
        with open(p, "a") as f:  # resumed later: no new header, cwd comes from the cursor
            f.write(json.dumps(codex_rec("response_item", {"type": "message", "role": "user",
                                                           "content": [{"type": "input_text", "text": "SECOND turn"}]})) + "\n")
        b2 = transcripts.collect(self.root, self.cfg, b1.cursors, 100000)
        self.assertIn("SECOND turn", b2.text)
        self.assertNotIn("FIRST", b2.text)


class TestHarnesses(Base):
    def test_agents_md_gets_inline_copy_that_follows_memory(self):
        agents = self.root / "AGENTS.md"
        self.assertTrue(agents.exists(), "init targets AGENTS.md by default")
        mem = self.root / "memory"
        (mem / "MEMORY.md").write_text("# Project memory\n## Rules\n- [build](build.md): use uv\n- [web](https://x.io)\n")
        (mem / "build.md").write_text("# build\n")
        self.assertEqual(targets.stale(self.root, self.cfg), ["AGENTS.md"])
        self.assertEqual(targets.sync(self.root, self.cfg), ["AGENTS.md"])
        text = agents.read_text()
        self.assertIn("[build](memory/build.md): use uv", text)
        self.assertIn("[web](https://x.io)", text)
        self.assertIn("### Rules", text)
        self.assertIn("dreamer note", text)
        claude = (self.root / "CLAUDE.md").read_text()
        self.assertIn("@memory/MEMORY.md", claude)
        self.assertNotIn("use uv", claude, "CLAUDE.md imports instead of copying")
        self.assertEqual(targets.stale(self.root, self.cfg), [])

    def test_gemini_target_and_uninstall(self):
        (self.root / "GEMINI.md").write_text("# Gemini rules\n")
        install.init(self.root, self.cfg, harnesses=["gemini"])
        self.assertIn("@memory/MEMORY.md", (self.root / "GEMINI.md").read_text())
        install.uninstall(self.root)
        self.assertEqual((self.root / "GEMINI.md").read_text(), "# Gemini rules\n")
        self.assertFalse((self.root / "AGENTS.md").exists(), "a file created only for the block is removed")

    def test_notes_feed_passes_are_consumed_and_restored_by_undo(self):
        notes.add(self.root, "Cursor agent: the staging DB needs VPN " + "x" * 80, "cursor")
        self.assertTrue((self.root / ".dreamer" / "inbox").is_dir())
        exclude = (self.root / ".git" / "info" / "exclude").read_text()
        self.assertIn(".dreamer/", exclude)
        script = [
            resp(tool("memory", {"command": "str_replace", "path": "/memories/MEMORY.md", "old_str": "# Project memory\n",
                                 "new_str": "# Project memory\n- Staging DB needs VPN (cursor note)\n"}, 1), stop="tool_use"),
            resp(text("Recorded VPN requirement.")),
        ]
        # A failed pass keeps the note.
        with self.assertRaises(DreamError):
            self.run_pass([RuntimeError("boom")])
        self.assertEqual(len(notes.pending(self.root)), 1)
        res, client = self.run_pass(script)
        self.assertEqual(res.status, "applied")
        prompt = client.requests[0]["messages"][0]["content"]
        self.assertIn("Notes recorded by agents", prompt)
        self.assertIn("cursor) Cursor agent: the staging DB needs VPN", prompt)
        self.assertEqual(notes.pending(self.root), [])
        self.assertIn("Staging DB needs VPN", (self.root / "AGENTS.md").read_text(), "AGENTS.md synced after apply")
        dream.undo(self.root, self.cfg)
        self.assertEqual(len(notes.pending(self.root)), 1, "undo puts consumed notes back")
        self.assertNotIn("Staging DB needs VPN", (self.root / "AGENTS.md").read_text())


class TestAgentDriven(Base):
    def cli(self, *argv, stdin=None):
        from dreamer import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("sys.stdin", io.StringIO(stdin or "")):
            code = cli.main(list(argv) + ["--repo-dir", str(self.root)])
        return code, out.getvalue()

    def test_full_pass_through_cli(self):
        self.transcript(self.root, "s1", self.basic_session(self.root, "MINE"))
        code, out = self.cli("dream", "start", "--force")
        self.assertEqual(code, 0)
        for want in ("dreamer dream memory", "=== SYSTEM PROMPT ===", "=== USER MESSAGE ===", "MINE", "dreamer dream finish"):
            self.assertIn(want, out)
        # Another driver can't start while this pass is open.
        self.assertEqual(dream.run(self.root, self.cfg, client_factory=lambda: 1 / 0).status, "skipped")

        code, out = self.cli("dream", "memory", stdin=json.dumps({"command": "create", "path": "/memories/build.md",
                                                                   "file_text": "# Build\n- use pnpm (2026-11-01, s:sess-aaa)\n"}))
        self.assertEqual((code, out.strip()), (0, "File created successfully at: /memories/build.md"))
        code, out = self.cli("dream", "memory", json.dumps({"command": "create", "path": "/memories/../x.md", "file_text": "x"}))
        self.assertEqual(code, 1)
        self.assertIn("Error", out)
        code, out = self.cli("dream", "search", "pnpm")
        self.assertIn("pnpm build", out)

        # build.md is unreachable -> finish refuses and says why; nothing applied yet.
        code, out = self.cli("dream", "finish")
        self.assertEqual(code, 3)
        self.assertIn("unreachable page", out)
        self.assertFalse((self.root / "memory" / "build.md").exists())

        self.cli("dream", "memory", json.dumps({"command": "str_replace", "path": "/memories/MEMORY.md",
                                                 "old_str": "# Project memory\n", "new_str": "# Project memory\n- [build](build.md)\n"}))
        code, out = self.cli("dream", "finish", "--summary", "added build rule")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.root / "memory" / "build.md").exists())
        self.assertIn("[build](memory/build.md)", (self.root / "AGENTS.md").read_text())
        st = self.state()
        self.assertTrue(st["files"])
        self.assertEqual(st["runs"][-1]["mode"], "agent")
        # Cursor advanced: nothing new to dream.
        code, out = self.cli("dream", "start")
        self.assertIn("skipped", out)

    def test_abort_and_concurrent_edit(self):
        self.transcript(self.root, "s1", self.basic_session(self.root, "MINE"))
        dream.external_start(self.root, self.cfg)
        self.assertTrue(dream.external_abort(self.root))
        with self.assertRaises(DreamError):
            dream.external_memory(self.root, self.cfg, {"command": "view", "path": "/memories"})
        self.assertEqual(self.state()["files"], {})

        dream.external_start(self.root, self.cfg)
        dream.external_memory(self.root, self.cfg, {"command": "str_replace", "path": "/memories/MEMORY.md",
                                                    "old_str": "# Project memory\n", "new_str": "# Project memory\n- a\n"})
        (self.root / "memory" / "MEMORY.md").write_text("# Project memory\n- edited by hand meanwhile\n")
        with self.assertRaises(DreamError):
            dream.external_finish(self.root, self.cfg)
        self.assertIn("by hand", (self.root / "memory" / "MEMORY.md").read_text())
        self.assertEqual(self.state()["files"], {})


class TestSharedAcrossHarnesses(Base):
    """One person, several harnesses (or just one), one memory."""

    def test_single_harness_detection_installs_only_that(self):
        repo2 = self.tmp / "work" / "solo"
        repo2.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo2, check=True)
        home = self.tmp / "home-codex-only"
        (home / ".codex").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch("shutil.which", return_value=None):
            self.assertEqual(install.detect_harnesses(repo2), ["codex"])
            install.init(repo2, DreamerConfig())
        self.assertTrue((repo2 / "AGENTS.md").exists())
        self.assertFalse((repo2 / "CLAUDE.md").exists())
        self.assertFalse((repo2 / ".claude").exists())
        hooks = json.loads((repo2 / ".codex" / "hooks.json").read_text())
        cmd = hooks["hooks"]["Stop"][0]["hooks"][0]
        self.assertTrue(cmd["async"])
        self.assertIn("dreamer run --auto", cmd["command"])
        self.assertIn(".codex/hooks.json", (repo2 / ".git" / "info" / "exclude").read_text())
        install.uninstall(repo2)
        self.assertFalse((repo2 / ".codex" / "hooks.json").exists())

    def test_existing_codex_hooks_are_preserved(self):
        path = self.root / ".codex" / "hooks.json"
        path.parent.mkdir()
        mine = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
                          "PreToolUse": [{"matcher": "shell", "hooks": [{"type": "command", "command": "lint"}]}]}}
        path.write_text(json.dumps(mine))
        install.init(self.root, self.cfg, harnesses=["codex"])
        install.init(self.root, self.cfg, harnesses=["codex"])
        data = json.loads(path.read_text())
        self.assertEqual(len(data["hooks"]["Stop"]), 2)
        install.uninstall(self.root)
        self.assertEqual(json.loads(path.read_text()), mine)

    def cursor_event(self, name, **fields):
        return json.dumps({"conversation_id": "conv-123", "generation_id": "g", "hook_event_name": name,
                           "workspace_roots": [str(self.root)], "transcript_path": None, **fields})

    def test_cursor_hooks_capture_sessions_into_the_digest(self):
        install.init(self.root, self.cfg, harnesses=["cursor"])
        hooks = json.loads((self.root / ".cursor" / "hooks.json").read_text())
        self.assertEqual(set(hooks["hooks"]), set(install.CURSOR_EVENTS))
        self.assertEqual(hooks["version"], 1)
        script = self.root / install.CURSOR_SCRIPT
        self.assertTrue(os.access(script, os.X_OK))
        from dreamer import capture
        with mock.patch.object(capture, "_trigger_dream") as trigger:
            ans = capture.handle("cursor", self.cursor_event("beforeSubmitPrompt", prompt="Deploys go through make ship, never kubectl"), self.root)
            self.assertEqual(json.loads(ans), {"continue": True})
            capture.handle("cursor", self.cursor_event("afterAgentResponse", text="Understood, using make ship."), self.root)
            capture.handle("cursor", self.cursor_event("afterShellExecution", command="make ship", output="Error: exit code 2", duration=3), self.root)
            capture.handle("cursor", self.cursor_event("afterFileEdit", file_path="Makefile", edits=[]), self.root)
            capture.handle("cursor", "not json", self.root)
            capture.handle("cursor", self.cursor_event("sessionEnd", session_id="s", reason="user"), self.root)
            trigger.assert_called_once()
        b = transcripts.collect(self.root, self.cfg, {}, 100000)
        for want in ("USER: Deploys go through make ship", "ASSISTANT: Understood", "→ Shell: command=make ship",
                     "✗ Error: exit code 2", "→ Edit: file_path=Makefile", "· captured"):
            self.assertIn(want, b.text)
        self.assertIn("make ship", transcripts.search(self.root, self.cfg, "kubectl"))

    def test_capture_ignores_repos_that_did_not_opt_in_and_dream_passes(self):
        from dreamer import capture
        stranger = self.tmp / "work" / "stranger"
        stranger.mkdir()
        ev = json.dumps({"conversation_id": "c", "hook_event_name": "beforeSubmitPrompt", "prompt": "hi",
                         "workspace_roots": [str(stranger)]})
        self.assertEqual(json.loads(capture.handle("cursor", ev, stranger)), {"continue": True})
        self.assertFalse((stranger / ".dreamer").exists(), "no files appear in a repo without dreamer")
        with mock.patch.dict(os.environ, {"DREAMER_DREAMING": "1"}):
            capture.handle("cursor", self.cursor_event("beforeSubmitPrompt", prompt="inside a pass"), self.root)
        self.assertFalse((self.root / ".dreamer" / "sessions").exists())

    def test_dream_passes_never_feed_later_passes(self):
        m = transcripts.MARKER
        self.transcript(self.root, "dreampass", [
            rec(self.root, "user", f"{m} Consolidate this repository's long-term memory.", sid="sess-dream"),
            rec(self.root, "assistant", [{"type": "text", "text": "DREAM-SESSION-TEXT"}], sid="sess-dream")])
        self.transcript(self.root, "normal", [
            rec(self.root, "user", "dream", sid="sess-norm"),
            rec(self.root, "assistant", [{"type": "tool_use", "id": "a", "name": "Bash",
                                          "input": {"command": "dreamer dream memory '{\"command\":\"view\"}'"}}], sid="sess-norm"),
            rec(self.root, "user", [{"type": "tool_result", "tool_use_id": "a", "content": f"{m}\nDIGEST-ECHO"}], sid="sess-norm"),
            rec(self.root, "user", "REAL USER TEXT afterwards", sid="sess-norm")])
        b = transcripts.collect(self.root, self.cfg, {}, 100000)
        self.assertIn("REAL USER TEXT", b.text)
        for leaked in ("DREAM-SESSION-TEXT", "DIGEST-ECHO", "dreamer dream memory"):
            self.assertNotIn(leaked, b.text)
        self.assertNotIn("DIGEST-ECHO", transcripts.search(self.root, self.cfg, "DIGEST"))

    def test_hook_commands_do_nothing_inside_a_pass_or_without_dreamer(self):
        bindir = self.tmp / "bin"
        bindir.mkdir()
        flag = self.tmp / "ran"
        fake = bindir / "dreamer"
        fake.write_text(f"#!/bin/sh\ntouch {flag}\n")
        fake.chmod(0o755)
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "CLAUDE_PROJECT_DIR": str(self.root)}
        for cmd in (install.SESSION_END_COMMAND, install.CODEX_STOP_COMMAND):
            r = subprocess.run(["bash", "-c", cmd], env={**env, "DREAMER_DREAMING": "1"})
            self.assertEqual(r.returncode, 0)
            r = subprocess.run(["bash", "-c", cmd], env={"PATH": "/usr/bin:/bin", "CLAUDE_PROJECT_DIR": str(self.root)})
            self.assertEqual(r.returncode, 0)
        import time as _t
        _t.sleep(0.3)
        self.assertFalse(flag.exists())
        subprocess.run(["bash", "-c", install.CODEX_STOP_COMMAND], env=env, cwd=self.root)
        for _ in range(30):
            if flag.exists():
                break
            _t.sleep(0.1)
        self.assertTrue(flag.exists(), "outside a pass, the hook does start dreamer")

    def test_state_lives_in_worktree_and_legacy_state_migrates(self):
        repo2 = self.tmp / "work" / "legacy"
        repo2.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo2, check=True)
        legacy = repo2 / ".git" / "dreamer"
        legacy.mkdir()
        (legacy / "state.json").write_text(json.dumps({"version": 1, "files": {"x": {"offset": 5}}, "failures": 0, "runs": []}))
        sdir = repo.state_dir(repo2)
        self.assertEqual(sdir, repo2 / ".dreamer" / "state")
        self.assertFalse(legacy.exists())
        self.assertEqual(repo.load_state(sdir)["files"], {"x": {"offset": 5}})
        self.assertIn(".dreamer/", (repo2 / ".git" / "info" / "exclude").read_text())


class TestEngines(Base):
    def fake_agent(self, body):
        script = self.tmp / "fake_agent.py"
        script.write_text("import os, subprocess, sys, json\n"
                          "assert os.environ.get('DREAMER_DREAMING') == '1'\n"
                          "assert '[dreamer:dream-pass]' in sys.argv[1]\n"
                          "def d(*a, stdin=None):\n"
                          "    return subprocess.run([sys.executable, '-m', 'dreamer.cli', 'dream', *a], input=stdin, text=True, capture_output=True)\n"
                          + body)
        self.cfg.dream.engine = "claude-code"
        self.cfg.dream.engine_command = f"{sys.executable} {script} {{prompt}}"

    def test_cli_engine_runs_the_agent_protocol(self):
        self.transcript(self.root, "s1", self.basic_session(self.root, "MINE"))
        self.fake_agent(
            "out = d('start', '--force').stdout\n"
            "assert 'SYSTEM PROMPT' in out and 'MINE' in out, out\n"
            "d('memory', stdin=json.dumps({'command': 'str_replace', 'path': '/memories/MEMORY.md', "
            "'old_str': '# Project memory\\n', 'new_str': '# Project memory\\n- pnpm only\\n'}))\n"
            "r = d('finish', '--summary', 'pnpm rule')\n"
            "assert r.returncode == 0, r.stdout\n")
        from dreamer import engines
        res = engines.run(self.root, self.cfg, force=True)
        self.assertEqual(res.status, "applied", res)
        self.assertIn("pnpm only", (self.root / "memory" / "MEMORY.md").read_text())
        self.assertEqual(self.state()["runs"][-1]["mode"], "claude-code")
        self.assertIn("pnpm only", (self.root / "AGENTS.md").read_text())

    def test_cli_engine_that_does_not_finish_changes_nothing(self):
        self.transcript(self.root, "s1", self.basic_session(self.root, "MINE"))
        self.fake_agent("d('start', '--force')\n"
                        "d('memory', json.dumps({'command': 'create', 'path': '/memories/x.md', 'file_text': 'x'}))\n")
        from dreamer import engines
        with self.assertRaises(DreamError):
            engines.run(self.root, self.cfg, force=True)
        self.assertFalse((self.root / "memory" / "x.md").exists())
        self.assertEqual(self.state()["files"], {})
        self.assertEqual(self.state()["failures"], 1)
        self.assertFalse(dream._external_active(repo.state_dir(self.root)))

    def test_auto_engine_choice(self):
        from dreamer import engines
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}):
            self.assertEqual(engines.resolve(self.cfg), "api")
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("shutil.which", side_effect=lambda b: "/x/codex" if b == "codex" else None):
            self.assertEqual(engines.resolve(self.cfg), "codex")
        self.assertIn("--full-auto", engines.command(self.cfg, "codex", False))
        self.assertIn("Bash(dreamer dream:*)", engines.command(self.cfg, "claude-code", False))
        with mock.patch.dict(os.environ, {"DREAMER_DREAMING": "1"}):
            self.assertEqual(engines.run(self.root, self.cfg).status, "skipped")


class TestPersonal(Base):
    """Opt-in personal memory shared by one person's repositories."""

    def setUp(self):
        super().setUp()
        self.dhome = self.tmp / "dreamer-home"
        self.env3 = mock.patch.dict(os.environ, {"DREAMER_HOME": str(self.dhome)})
        self.env3.start()
        self.repo_b = self.tmp / "work" / "repo-b"
        self.repo_c = self.tmp / "work" / "repo-c"
        for r in (self.repo_b, self.repo_c):
            r.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=r, check=True)
        install.init(self.root, self.cfg, harnesses=["claude", "codex", "cursor"])
        install.init(self.repo_b, DreamerConfig(), harnesses=["claude", "codex", "cursor"])
        install.init(self.repo_c, DreamerConfig(), harnesses=["claude", "codex", "cursor"])
        personal.enable(self.root, ["claude", "codex", "cursor"])
        personal.enable(self.repo_b, ["claude", "codex", "cursor"])

    def tearDown(self):
        self.env3.stop()
        super().tearDown()

    def personal_script(self, line="- Answer in Korean; review adversarially (2026-11-08, s:sess-aaa)"):
        return [
            resp(tool("memory", {"command": "str_replace", "path": "/memories/personal/MEMORY.md", "old_str": "# Personal memory\n",
                                 "new_str": f"# Personal memory\n{line}\n"}, 1),
                 tool("memory", {"command": "str_replace", "path": "/memories/MEMORY.md", "old_str": "# Project memory\n",
                                 "new_str": "# Project memory\n- pnpm only\n"}, 2), stop="tool_use"),
            resp(text("Recorded a personal preference and a project rule.")),
        ]

    def test_injected_only_through_local_uncommitted_files(self):
        local = {"CLAUDE.local.md", "AGENTS.override.md", ".cursor/rules/dreamer-personal.mdc"}
        exclude = (self.root / ".git" / "info" / "exclude").read_text().splitlines()
        for rel in local:
            self.assertTrue((self.root / rel).exists(), rel)
            self.assertIn(rel, exclude)
        override = (self.root / "AGENTS.override.md").read_text()
        self.assertTrue(override.startswith(personal.OVERRIDE_HEADER))
        self.assertIn("## Project memory", override, "Codex reads the override instead of AGENTS.md, so it embeds it")
        self.assertIn("## Personal memory", override)
        for shared in ("CLAUDE.md", "AGENTS.md"):
            self.assertNotIn("Personal memory", (self.root / shared).read_text())
        for rel in local:
            self.assertFalse((self.repo_c / rel).exists(), "a repository that did not opt in gets nothing")

    def test_tracked_files_are_never_written(self):
        repo_d = self.tmp / "work" / "repo-d"
        repo_d.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo_d, check=True)
        (repo_d / "CLAUDE.local.md").write_text("# committed by the team\n")
        subprocess.run(["git", "add", "CLAUDE.local.md"], cwd=repo_d, check=True)
        install.init(repo_d, DreamerConfig(), harnesses=["claude"])
        out = personal.enable(repo_d, ["claude"])
        self.assertTrue(any("tracked by git" in o for o in out))
        self.assertEqual((repo_d / "CLAUDE.local.md").read_text(), "# committed by the team\n")

    def test_learned_in_one_repo_reaches_the_others_that_opted_in(self):
        self.transcript(self.root, "s1", self.basic_session(self.root, "MINE"))
        res, client = self.run_pass(self.personal_script())
        self.assertEqual(res.status, "applied")
        self.assertIn("personal/MEMORY.md", res.changed)
        req = client.requests[0]
        self.assertIn("/memories/personal/", req["system"])
        self.assertIn("## /memories/personal/MEMORY.md", req["messages"][0]["content"])
        self.assertIn("Answer in Korean", (self.dhome / "personal" / "MEMORY.md").read_text())
        self.assertIn("pnpm only", (self.root / "memory" / "MEMORY.md").read_text())
        self.assertNotIn("Korean", (self.root / "memory" / "MEMORY.md").read_text())
        self.assertIn("Answer in Korean", (self.root / "CLAUDE.local.md").read_text())
        # Repo B picks it up on its next sync (every run / hook does one); repo C never.
        self.assertIn("CLAUDE.local.md", targets.stale(self.repo_b, DreamerConfig()))
        targets.sync(self.repo_b, DreamerConfig())
        for rel in ("CLAUDE.local.md", "AGENTS.override.md", ".cursor/rules/dreamer-personal.mdc"):
            self.assertIn("Answer in Korean", (self.repo_b / rel).read_text())
        targets.sync(self.repo_c, DreamerConfig())
        self.assertNotIn("Korean", "".join(p.read_text() for p in self.repo_c.rglob("*.md")))
        self.assertNotIn("pnpm only", (self.repo_b / "memory" / "MEMORY.md").read_text(), "project memory stays per repo")

    def test_disabled_repo_cannot_touch_personal_memory(self):
        repo_c_cfg = DreamerConfig()
        repo_c_cfg.dream.min_new_chars = 50
        self.transcript(self.repo_c, "s1", self.basic_session(self.repo_c, "C"))
        client = FakeClient([
            resp(tool("memory", {"command": "create", "path": "/memories/personal/x.md", "file_text": "x"}, 1), stop="tool_use"),
            resp(text("nothing"))])
        res = dream.run(self.repo_c, repo_c_cfg, client_factory=lambda: client)
        self.assertEqual(res.status, "unchanged")
        err = client.requests[1]["messages"][-1]["content"][0]
        self.assertTrue(err["is_error"])
        self.assertIn("not enabled", err["content"])
        self.assertFalse((self.repo_c / "memory" / "personal").exists())
        self.assertNotIn("/memories/personal", client.requests[0]["system"])

    def test_concurrent_change_from_another_repo_aborts(self):
        self.transcript(self.root, "s1", self.basic_session(self.root, "MINE"))
        pidx = self.dhome / "personal" / "MEMORY.md"

        def other_repo_writes(n):
            if n == 2:
                pidx.write_text("# Personal memory\n- written by repo B meanwhile\n")
        with self.assertRaises(DreamError):
            self.run_pass(self.personal_script(), on_call=other_repo_writes)
        self.assertIn("repo B meanwhile", pidx.read_text())
        self.assertNotIn("pnpm only", (self.root / "memory" / "MEMORY.md").read_text())
        self.assertEqual(self.state()["files"], {})

    def test_undo_restores_personal_only_if_untouched_since(self):
        self.transcript(self.root, "s1", self.basic_session(self.root, "MINE"))
        pidx = self.dhome / "personal" / "MEMORY.md"
        self.run_pass(self.personal_script())
        dream.undo(self.root, self.cfg)
        self.assertEqual(pidx.read_text(), "# Personal memory\n")
        self.assertNotIn("pnpm only", (self.root / "memory" / "MEMORY.md").read_text())

        self.run_pass(self.personal_script())
        pidx.write_text(pidx.read_text() + "- added later from repo B\n")
        dream.undo(self.root, self.cfg)
        self.assertIn("added later from repo B", pidx.read_text(), "another repo's later change is not rolled back")
        self.assertNotIn("pnpm only", (self.root / "memory" / "MEMORY.md").read_text(), "project memory still undone")

    def test_budget_and_own_override_file(self):
        t = personal.tool(self.tmp / "nostage")
        self.assertIsNone(t)
        tool_ = store.MemoryTool(personal.ensure_store(), personal.config(), personal.VROOT)
        with self.assertRaises(store.ToolError):
            tool_.execute({"command": "create", "path": "/memories/personal/MEMORY.md", "file_text": "x" * 5000})
        mine = self.repo_c / "AGENTS.override.md"
        mine.write_text("# My own Codex overrides\nUse the staging cluster.\n")
        personal.enable(self.repo_c, ["codex"])
        text = mine.read_text()
        self.assertTrue(text.startswith("# My own Codex overrides\nUse the staging cluster."))
        self.assertIn("## Personal memory", text)
        personal.disable(self.repo_c)
        self.assertEqual(mine.read_text(), "# My own Codex overrides\nUse the staging cluster.\n")

    def test_disable_and_uninstall_remove_local_copies(self):
        personal.disable(self.root)
        for rel in ("CLAUDE.local.md", "AGENTS.override.md", ".cursor/rules/dreamer-personal.mdc"):
            self.assertFalse((self.root / rel).exists(), rel)
        self.assertFalse(personal.enabled(self.root))
        self.assertTrue((self.dhome / "personal" / "MEMORY.md").exists(), "the store itself is kept")


class TestConfig(unittest.TestCase):
    def test_unknown_and_wrongly_typed_keys_warn(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "dreamer.toml").write_text('preset = "vibe"\n[model]\nprovider = "ollama"\neffort = "low"\n[memory]\nmax_files = "x"\n')
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                cfg = DreamerConfig.load(Path(d))
            self.assertEqual(cfg.model.effort, "low")
            self.assertEqual(cfg.memory.max_files, 60)
            self.assertIn("model.provider", err.getvalue())
            self.assertIn("preset", err.getvalue())


if __name__ == "__main__":
    unittest.main()
