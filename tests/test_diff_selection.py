"""Large-PR diff selection tests (no network, deterministic)."""

from __future__ import annotations

from backend.agents.specialists import parse_unified_diff
from backend.diff_utils import select_reviewable_diff, split_unified_diff


def _chunk(path: str, added: list[str]) -> str:
    lines = "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,1 +1,{len(added)} @@\n"
        f"{lines}"
    )


def test_selection_excludes_vendored_generated_and_lockfiles() -> None:
    diff = (
        _chunk("node_modules/pkg/index.js", ["eval(userInput)"])
        + _chunk("modules/lib/dist/bundle.js", ["eval(userInput)"])
        + _chunk("package-lock.json", ['"lockfileVersion": 3'])
        + _chunk("src/app.py", ["def lookup(user_id):", "    return user_id"])
    )

    selection = select_reviewable_diff(diff)

    assert selection.included_files == ["src/app.py"]
    assert "node_modules/pkg/index.js" in selection.excluded_files
    assert "modules/lib/dist/bundle.js" in selection.excluded_files
    assert "package-lock.json" in selection.excluded_files
    assert "src/app.py" in selection.diff
    assert "package-lock.json" not in selection.diff


def test_selection_prefers_source_over_docs_when_budget_is_tight() -> None:
    source = _chunk("src/security.py", ["query = f'SELECT * FROM users WHERE id={user_id}'"])
    docs = _chunk("docs/guide.md", ["# A very long guide", "word " * 200])

    selection = select_reviewable_diff(docs + source, max_bytes=200, max_file_bytes=200)

    assert selection.included_files == ["src/security.py"]
    assert "docs/guide.md" not in selection.diff
    assert selection.truncated is True


def test_selection_caps_per_file_and_total_bytes() -> None:
    diff = _chunk("src/a.py", [f"value_{i} = {i}" for i in range(200)])
    diff += _chunk("src/b.py", [f"other_{i} = {i}" for i in range(200)])

    selection = select_reviewable_diff(diff, max_bytes=500, max_file_bytes=300)

    assert selection.selected_bytes <= 500
    assert selection.truncated is True
    assert selection.original_bytes == len(diff.encode("utf-8"))
    assert selection.included_files


def test_selection_falls_back_when_every_chunk_is_excluded() -> None:
    diff = _chunk("assets/logo.png", []) + (
        "diff --git a/assets/icon.png b/assets/icon.png\n"
        "--- a/assets/icon.png\n"
        "+++ b/assets/icon.png\n"
        "Binary files a/assets/icon.png and b/assets/icon.png differ\n"
    )

    selection = select_reviewable_diff(diff, max_bytes=100)

    assert selection.included_files == []
    assert selection.diff
    assert selection.truncated is True


def test_selection_preserves_added_line_numbers_for_grounding() -> None:
    diff = (
        _chunk("node_modules/x.js", ["ignore()"])
        + "diff --git a/src/db.py b/src/db.py\n"
        "--- a/src/db.py\n"
        "+++ b/src/db.py\n"
        "@@ -10,3 +10,4 @@ def handler():\n"
        " context = 1\n"
        "+def lookup(conn, user_id):\n"
        "+    return conn.execute(f\"SELECT * FROM users WHERE id = {user_id}\")\n"
        " return context\n"
    )

    selection = select_reviewable_diff(diff)
    added = parse_unified_diff(selection.diff)

    assert [(line.file_path, line.line_number) for line in added] == [
        ("src/db.py", 11),
        ("src/db.py", 12),
    ]
    assert "node_modules" not in selection.diff


def test_split_unified_diff_handles_multiple_files() -> None:
    diff = _chunk("a.py", ["x = 1"]) + _chunk("b.py", ["y = 2"])

    chunks = split_unified_diff(diff)

    assert [path for path, _ in chunks] == ["a.py", "b.py"]
