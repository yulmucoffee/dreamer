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

from dreamer import dream, install, repo, store, transcripts
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
        cursors = {k: {"offset": v} for k, v in b1.offsets.items()}
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
        self.assertEqual(md.count(install.BLOCK_BEGIN), 1)
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
