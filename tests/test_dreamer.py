import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from dreamer import dream, install, notes, repo, store, targets, transcripts
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
        install.init(self.root, self.cfg)

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
        install.init(self.root, self.cfg)
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
        install.init(self.root, self.cfg)
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
