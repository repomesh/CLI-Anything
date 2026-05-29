"""Wire-format tests for cli_anything.browser.utils.domshell_backend.

These tests patch the async ``_call_execute`` helper and assert the exact
command string sent to the DOMShell ``domshell_execute`` tool, so wire-format
regressions (quoting, command names, multi-line layout, restore ordering)
fail loudly.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

from cli_anything.browser.core.session import Session
from cli_anything.browser.utils import domshell_backend as backend


# ── Path translation: harness `/` vs DOMShell `~/` ───────────────────


def test_translate_path_root_is_absolute_empty():
    """`/` → ("", True). Absolute target with no further subdir."""
    assert backend._translate_path("/") == ("", True)


def test_translate_path_empty_is_relative_empty():
    """`""` → ("", False). Bare/relative no-op; caller decides what to do."""
    assert backend._translate_path("") == ("", False)


def test_translate_path_strips_all_leading_slashes():
    """`//main` collapses to `("main", True)` via lstrip."""
    assert backend._translate_path("/main") == ("main", True)
    assert backend._translate_path("//main") == ("main", True)
    assert backend._translate_path("///main") == ("main", True)


def test_translate_path_preserves_relative():
    """Relative paths pass through unchanged — DOMShell handles them."""
    assert backend._translate_path("main") == ("main", False)
    assert backend._translate_path("..") == ("..", False)
    assert backend._translate_path(".") == (".", False)


# ── ls / cd / cat / click: absolute paths anchor at %here% ───────────


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_ls_absolute_path_uses_three_separate_calls(mock_call):
    """Split-and-check: anchor → bare ls → restore, three distinct calls.

    Anchor success is load-bearing — if the cd fails, ls would run in
    the wrong cwd. Tested separately by
    test_ls_anchor_failure_short_circuits.
    """
    mock_call.return_value = _make_result("[lane: 1]")
    sess = _make_session(working_dir="/main")
    backend.ls("/main", session=sess)
    assert mock_call.call_count == 3
    assert mock_call.call_args_list[0].args[0] == "cd %here%/main"  # anchor
    assert mock_call.call_args_list[1].args[0] == "ls"               # op
    assert mock_call.call_args_list[2].args[0] == "cd %here%/main"  # restore


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_ls_root_from_drifted_cwd_anchors_at_here(mock_call):
    """The Codex P1 case: `ls /` from a drifted `/main` cwd."""
    mock_call.return_value = _make_result("[lane: 1]")
    sess = _make_session(working_dir="/main")
    backend.ls("/", session=sess)
    assert mock_call.call_count == 3
    assert mock_call.call_args_list[0].args[0] == "cd %here%"         # anchor at tab root
    assert mock_call.call_args_list[1].args[0] == "ls"                # bare ls
    assert mock_call.call_args_list[2].args[0] == "cd %here%/main"   # back to /main


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_ls_relative_path_no_wrap(mock_call):
    """Relative `ls main` runs against lane cwd — no anchor, no restore."""
    mock_call.return_value = _make_result("[lane: 1]")
    backend.ls("main")
    assert mock_call.call_args.args[0] == "ls main"


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_ls_absolute_path_with_spaces_quoted(mock_call):
    """The `%here%/<deeper>` token is shell-quoted as one unit.

    Upstream-smoked: `cd '%here%/<path>'` works in DOMShell 2.0.x — so
    whitespace and other shell metachars in absolute targets are safe.
    """
    mock_call.return_value = _make_result("[lane: 1]")
    backend.ls("/path with spaces", session=_make_session(working_dir="/"))
    # Anchor (first call) is the quoted form.
    assert mock_call.call_args_list[0].args[0] == "cd '%here%/path with spaces'"


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_cd_root_uses_here(mock_call):
    """`cd /` → `cd %here%`. Single line — cd's purpose IS the new state."""
    mock_call.return_value = _make_result("[lane: 1]")
    backend.cd("/")
    assert mock_call.call_args.args[0] == "cd %here%"


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_cd_absolute_is_single_line_no_restore(mock_call):
    """`cd /main` → `cd %here%/main`. One line, no restore."""
    mock_call.return_value = _make_result("[lane: 1]")
    backend.cd("/main")
    assert mock_call.call_args.args[0] == "cd %here%/main"


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_cd_subpath_with_indexing(mock_call):
    """Brackets are shell-metachars — `_here_path` quotes the full token."""
    mock_call.return_value = _make_result("[lane: 1]")
    backend.cd("/main/div[0]")
    assert mock_call.call_args.args[0] == "cd '%here%/main/div[0]'"


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_cd_relative_quoted(mock_call):
    """Relative `cd main` is quoted via _q (handles spaces / metachars)."""
    mock_call.return_value = _make_result("[lane: 1]")
    backend.cd("main")
    assert mock_call.call_args.args[0] == "cd main"


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_cat_absolute_path_uses_three_separate_calls(mock_call):
    """`cat /main/btn`: anchor at tab root → cat main/btn → restore."""
    mock_call.return_value = _make_result("[lane: 1]")
    sess = _make_session(working_dir="/")
    backend.cat("/main/btn", session=sess)
    assert mock_call.call_count == 3
    assert mock_call.call_args_list[0].args[0] == "cd %here%"
    assert mock_call.call_args_list[1].args[0] == "cat main/btn"
    assert mock_call.call_args_list[2].args[0] == "cd %here%"


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_cat_relative_no_wrap(mock_call):
    mock_call.return_value = _make_result("[lane: 1]")
    backend.cat("main/btn")
    assert mock_call.call_args.args[0] == "cat main/btn"


def test_cat_root_raises_value_error():
    with pytest.raises(ValueError, match="element name is required"):
        backend.cat("/")
    with pytest.raises(ValueError, match="element name is required"):
        backend.cat("")


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_click_absolute_path_uses_three_separate_calls(mock_call):
    mock_call.return_value = _make_result("[lane: 1]")
    sess = _make_session(working_dir="/main")
    backend.click("/main/button[0]", session=sess)
    assert mock_call.call_count == 3
    assert mock_call.call_args_list[0].args[0] == "cd %here%"
    assert mock_call.call_args_list[1].args[0] == "click 'main/button[0]'"
    assert mock_call.call_args_list[2].args[0] == "cd %here%/main"


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_click_relative_no_wrap(mock_call):
    mock_call.return_value = _make_result("[lane: 1]")
    backend.click("button[0]")
    assert mock_call.call_args.args[0] == "click 'button[0]'"


def test_click_root_raises_value_error():
    with pytest.raises(ValueError, match="element name is required"):
        backend.click("/")
    with pytest.raises(ValueError, match="element name is required"):
        backend.click("")


# ── grep absolute-path anchoring ─────────────────────────────────────


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_grep_rooted_absolute_uses_three_separate_calls(mock_call):
    """Rooted grep (absolute): anchor → grep → restore, three distinct calls."""
    mock_call.return_value = _make_result("[lane: 1]")
    backend.grep(
        "Login", path="/main", prev="/", session=_make_session(working_dir="/"),
    )
    assert mock_call.call_count == 3
    assert mock_call.call_args_list[0].args[0] == "cd %here%/main"
    assert mock_call.call_args_list[1].args[0] == "grep Login"
    assert mock_call.call_args_list[2].args[0] == "cd %here%"


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_grep_rooted_absolute_restores_to_session_wd(mock_call):
    """Restore (third call) follows session.working_dir, not the `prev`
    kwarg, when the path is absolute — the lane cwd needs to end up
    where the harness thinks it is."""
    mock_call.return_value = _make_result("[lane: 1]")
    backend.grep(
        "Login", path="/main", prev="/", session=_make_session(working_dir="/main"),
    )
    assert mock_call.call_args_list[2].args[0] == "cd %here%/main"


# ── Anchor-failure short-circuit (the Codex P2 case per-wrapper) ─────
#
# If the anchor `cd` fails, the operation must NOT run — it would
# resolve against the wrong cwd and produce wrong-target results. Each
# wrapper that uses split-and-check mirrors `type_text`'s
# anchor-failure pattern.


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_ls_anchor_failure_short_circuits(mock_call):
    """ls absolute-path anchor failure → only the anchor call ran.

    Uses DOMShell's actual error shape: ANSI-red-wrapped, command-
    prefixed ("cd: ...") — not the imaginary "Error: ..." prefix that
    silently bypassed ``_is_error`` for several rounds.
    """
    mock_call.return_value = _make_result(
        "\x1b[31mcd: tab 12345 is outside the session group\x1b[0m"
    )
    sess = _make_session(working_dir="/")
    result = backend.ls("/main", session=sess)
    assert mock_call.call_count == 1
    assert mock_call.call_args_list[0].args[0] == "cd %here%/main"
    assert "error" in result


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_cat_anchor_failure_short_circuits(mock_call):
    mock_call.return_value = _make_result(
        "\x1b[31mcd: chrome:// not debuggable\x1b[0m"
    )
    sess = _make_session(working_dir="/")
    result = backend.cat("/main/btn", session=sess)
    assert mock_call.call_count == 1
    assert mock_call.call_args_list[0].args[0] == "cd %here%"
    assert "error" in result


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_click_anchor_failure_short_circuits(mock_call):
    """Critical for click: a misdirected click could trigger an
    unintended action. Anchor must halt before the click runs.
    """
    mock_call.return_value = _make_result(
        "\x1b[31mcd: chrome:// not debuggable\x1b[0m"
    )
    sess = _make_session(working_dir="/")
    result = backend.click("/main/button[0]", session=sess)
    assert mock_call.call_count == 1
    assert mock_call.call_args_list[0].args[0] == "cd %here%"
    assert "error" in result


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_grep_rooted_anchor_failure_short_circuits(mock_call):
    """Rooted grep with anchor failure → grep doesn't run (wrong-scope
    matches would be misleading). Restore also skipped because we
    never moved off the original cwd.
    """
    mock_call.return_value = _make_result(
        "\x1b[31mcd: /missing: No such directory\x1b[0m"
    )
    sess = _make_session(working_dir="/")
    result = backend.grep("Login", path="/missing", prev="/", session=sess)
    assert mock_call.call_count == 1
    assert mock_call.call_args_list[0].args[0] == "cd %here%/missing"
    assert "error" in result


# ── _parse_execute_result: dict shapes per command ───────────────────
#
# DOMShell 2.x returns text content; the CLI was written for the
# pre-2.0 per-command tools that returned dicts. These tests lock in
# the translator's per-command shape contract so the CLI doesn't break
# on `result.get(...)` calls.


def test_parse_execute_result_ls_extracts_entries():
    result = _make_result("button[0]\nlink[1]\nimg[2]\n[lane: 7]")
    parsed = backend._parse_execute_result(result, "ls")
    assert parsed["entries"] == [
        {"name": "button[0]", "role": "", "path": "button[0]"},
        {"name": "link[1]", "role": "", "path": "link[1]"},
        {"name": "img[2]", "role": "", "path": "img[2]"},
    ]
    # Lane marker is stripped from the raw text.
    assert "[lane:" not in parsed["raw"]


def test_parse_execute_result_ls_empty_text_returns_empty_entries():
    parsed = backend._parse_execute_result(_make_result("[lane: 7]"), "ls")
    assert parsed == {"entries": [], "raw": ""}


def test_parse_execute_result_grep_extracts_matches():
    result = _make_result(
        "/main/button[0]\n/main/link[1]\n[lane: 7]"
    )
    parsed = backend._parse_execute_result(result, "grep")
    assert parsed["matches"] == ["/main/button[0]", "/main/link[1]"]
    assert "[lane:" not in parsed["raw"]


def test_parse_execute_result_error_returns_error_dict():
    """Errors get `error` AND `output` keys — the CLI checks `"error" in
    result` for cd-style guards but also pretty-prints `output`.

    Uses the actual DOMShell error shape: ANSI-red-wrapped, command-
    prefixed (``ls: ...``).
    """
    result = _make_result(
        "\x1b[31mls: main: No such directory\x1b[0m\n[lane: 7]"
    )
    parsed = backend._parse_execute_result(result, "ls")
    assert "error" in parsed
    assert parsed["error"].startswith("\x1b[31mls:")
    assert parsed["output"] == parsed["error"]


def test_parse_execute_result_default_for_other_commands():
    """cat/click/focus/type/open/refresh/back/forward all fall through
    to the generic ``{"output": text}`` shape."""
    for cmd in ("cat", "click", "focus", "type", "open", "refresh", "back", "forward"):
        parsed = backend._parse_execute_result(
            _make_result("done\n[lane: 1]"), cmd,
        )
        assert parsed == {"output": "done"}


# ── End-to-end: wrappers return parsed dicts (not CallToolResult) ────


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_ls_returns_entries_dict(mock_call):
    """`backend.ls(...)` must return ``{"entries": [...]}`` so the CLI
    doesn't AttributeError on `result.get("entries", [])`. This is the
    Codex P1 shipped regression."""
    mock_call.return_value = _make_result("button[0]\n[lane: 1]")
    result = backend.ls("main")
    assert "entries" in result
    assert isinstance(result["entries"], list)


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_grep_returns_matches_dict(mock_call):
    mock_call.return_value = _make_result("/main/btn[0]\n[lane: 1]")
    result = backend.grep("Login")
    assert "matches" in result
    assert result["matches"] == ["/main/btn[0]"]


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_cd_returns_dict_with_output(mock_call):
    """`backend.cd(...)` returns a dict so `isinstance(result, dict)`
    in fs.change_directory works and `result.get("path", path)` falls
    back cleanly."""
    mock_call.return_value = _make_result("✓ Entered main\n[lane: 1]")
    result = backend.cd("/main")
    assert isinstance(result, dict)
    assert "error" not in result


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_cd_error_returns_error_dict(mock_call):
    """`fs.change_directory` checks `"error" not in result` — make sure
    error paths surface the error key so the harness's working-dir
    update is correctly skipped. Uses the actual DOMShell error shape.
    """
    mock_call.return_value = _make_result(
        "\x1b[31mcd: /missing: No such directory\x1b[0m"
    )
    result = backend.cd("/missing")
    assert "error" in result


# ── _is_error helper ─────────────────────────────────────────────────


def test_is_error_detects_isError_attribute():
    assert backend._is_error(SimpleNamespace(isError=True)) is True
    assert backend._is_error(SimpleNamespace(isError=False)) is False


def test_is_error_detects_dict_keys():
    assert backend._is_error({"isError": True}) is True
    assert backend._is_error({"error": "boom"}) is True
    assert backend._is_error({"path": "/main"}) is False


def test_is_error_detects_ansi_red_wrapper():
    """Common DOMShell error shape: ANSI red around a command-prefixed
    message. These do NOT start with the literal "error" — the earlier
    detection that ANSI-stripped first then ``startswith("error")``
    failed every one of these, silently regressing the safety chain
    across every wrapper. Catch on the ``\\x1b[31m`` red marker
    instead.
    """
    assert backend._is_error(
        _make_result("\x1b[31mcd: foo: No such directory\x1b[0m")
    ) is True
    assert backend._is_error(
        _make_result("\x1b[31mfocus: No such element\x1b[0m")
    ) is True
    assert backend._is_error(
        _make_result("\x1b[31mls: main: No such directory\x1b[0m")
    ) is True
    assert backend._is_error(
        _make_result("\x1b[31mcd: tab 12345 is outside the session group\x1b[0m")
    ) is True


def test_is_error_detects_explicit_error_prefix_without_ansi():
    """Fallback: rarer non-coloured errors or pre-stripped input still
    detected via ``"error:"`` prefix.
    """
    assert backend._is_error(_make_result("Error: bad command")) is True
    assert backend._is_error(_make_result("ERROR: case insensitive")) is True


def test_is_error_does_not_flag_success_in_other_colors():
    """Anti-false-positive: only ``\\x1b[31m`` (red) signals an error.
    Success commonly wraps in ``\\x1b[32m`` (green) — must not flag.
    """
    assert backend._is_error(
        _make_result("\x1b[32m✓ ok\x1b[0m")
    ) is False
    assert backend._is_error(
        _make_result("✓ command completed")
    ) is False
    assert backend._is_error(
        _make_result("✓ Focused\n[lane: 1]")
    ) is False


def test_is_error_handles_empty():
    assert backend._is_error(_make_result("")) is False


def test_is_error_handles_missing_content():
    assert backend._is_error(SimpleNamespace()) is False


# ── grep: command string and call sequencing ──────────────────────────


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_grep_unrooted_produces_single_grep_call(mock_call):
    """Unrooted grep dispatches one ``grep <pattern>`` call."""
    mock_call.return_value = {}

    backend.grep("Login")

    # session=None when no session is passed (default).
    assert mock_call.call_args_list == [call("grep Login", False, session=None)]


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_grep_rooted_emits_three_call_sequence(mock_call):
    """Rooted grep is THREE separate ``_call_execute`` calls: anchor →
    grep → restore. Anchor success is load-bearing (would search the
    wrong subtree on cwd drift), so we can't fold this into a single
    multi-line continue-on-error call.

    Lane sharing across the three calls is preserved via
    ``session.domshell_lane_id`` — see round-4 lane-persistence tests.
    """
    mock_call.return_value = _make_result("[lane: 1]")

    backend.grep("Login", path="/main", prev="/")

    assert mock_call.call_args_list == [
        call("cd %here%/main", False, session=None),
        call("grep Login", False, session=None),
        call("cd %here%", False, session=None),
    ]


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_grep_rooted_quotes_path_with_spaces(mock_call):
    """Absolute paths with whitespace are quoted as `cd '%here%/path with spaces'`.

    Upstream-smoked: DOMShell's `cd` accepts the quoted form cleanly.
    """
    mock_call.return_value = _make_result("[lane: 1]")

    backend.grep("Login", path="/path with spaces", prev="/")

    # Three-call sequence — quoting applies to the anchor (first call).
    assert mock_call.call_args_list == [
        call("cd '%here%/path with spaces'", False, session=None),
        call("grep Login", False, session=None),
        call("cd %here%", False, session=None),
    ]


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_grep_pattern_with_shell_metacharacters_quoted(mock_call):
    """Patterns with shell metacharacters get quoted (no injection via grep)."""
    mock_call.return_value = {}

    backend.grep("$(rm -rf /)")

    grep_cmd = mock_call.call_args_list[0].args[0]
    # shlex.quote will single-quote the dangerous payload.
    assert grep_cmd == "grep '$(rm -rf /)'"


def test_grep_rejects_positional_path():
    """grep(pattern, path) — positional path raises TypeError.

    Pre-migration callers writing ``grep("Login", True)`` to mean
    ``use_daemon=True`` must not silently get ``path=True``.
    """
    with pytest.raises(TypeError):
        backend.grep("Login", True)  # type: ignore[misc]


def test_grep_rejects_positional_use_daemon():
    """Even the third positional slot is blocked."""
    with pytest.raises(TypeError):
        backend.grep("Login", "/main", "/", True)  # type: ignore[misc]


def test_grep_keyword_use_daemon_still_works():
    """Keyword call against the new signature still type-checks at call time."""
    with patch.object(backend, "_call_execute", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {}
        backend.grep("Login", use_daemon=True)
        assert mock_call.call_args_list == [call("grep Login", True, session=None)]


# ── type_text: focus+type pairing and newline injection guard ─────────


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_type_text_emits_focus_then_type_with_session(mock_call):
    """type_text issues focus + type as two separate execute calls.

    The split is for safety: DOMShell's multi-line splitter continues
    past per-line errors, so a single ``focus path\\ntype text`` call
    would dispatch keys into stale focus if ``focus`` failed. Two calls
    with an error check between them prevents that.
    """
    sess = _make_session(working_dir="/")
    mock_call.return_value = _make_result("✓ Focused\n[lane: 1]")

    backend.type_text("search_input", "machine learning", session=sess)

    assert mock_call.call_count == 2
    # Relative path → no anchor wrap.
    assert mock_call.call_args_list[0].args[0] == "focus search_input"
    assert mock_call.call_args_list[1].args[0] == "type 'machine learning'"


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_type_text_absolute_path_uses_four_separate_calls(mock_call):
    """Absolute focus path: anchor → focus → type → restore, all distinct.

    type_text is a safety chain, NOT a cleanup-line idiom: it must HALT
    on focus failure rather than continue past it. Wrapping focus in a
    multi-line ``cd %here%\\nfocus\\ncd <restore>`` would re-introduce
    the continue-on-error trap (the anchor's leading "✓" masks the
    focus error in the combined response). Split into four separate
    _call_execute calls, all sharing the persisted lane via session.
    """
    sess = _make_session(working_dir="/main")
    mock_call.return_value = _make_result("✓\n[lane: 1]")

    backend.type_text("/main/input", "hello", session=sess)

    assert mock_call.call_count == 4
    assert mock_call.call_args_list[0].args[0] == "cd %here%"
    assert mock_call.call_args_list[1].args[0] == "focus main/input"
    assert mock_call.call_args_list[2].args[0] == "type hello"
    assert mock_call.call_args_list[3].args[0] == "cd %here%/main"


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_type_text_absolute_path_focus_failure_skips_type_and_restores(mock_call):
    """The Codex P2 safety case: a stale absolute focus path must halt
    before type can dispatch keys into whatever was previously focused.
    The restore still runs so the lane cwd ends up where the harness
    expects.
    """
    sess = _make_session(working_dir="/main")
    mock_call.side_effect = [
        _make_result("✓ Entered tab 123\n[lane: 1]"),                  # cd anchor ok
        _make_result("\x1b[31mfocus: No such element\x1b[0m"),          # focus fails
        _make_result("✓ Typed\n[lane: 1]"),                             # MUST NOT be reached
        _make_result("✓\n[lane: 1]"),                                    # restore
    ]

    backend.type_text("/main/missing", "secret_password", session=sess)

    # Three calls: anchor, focus, restore. Type is skipped because the
    # focus error halts the safety chain before keys are dispatched.
    assert mock_call.call_count == 3
    assert mock_call.call_args_list[0].args[0] == "cd %here%"
    assert mock_call.call_args_list[1].args[0] == "focus main/missing"
    assert mock_call.call_args_list[2].args[0] == "cd %here%/main"
    # Critically — the "type secret_password" command was never sent.
    for call_args in mock_call.call_args_list:
        assert "type" not in call_args.args[0].split("\n")[0]


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_type_text_anchor_failure_does_not_attempt_focus(mock_call):
    """If even the anchor cd fails (e.g. chrome:// not debuggable),
    neither focus nor type runs. No restore needed because we never
    moved off the original cwd.
    """
    sess = _make_session(working_dir="/")
    mock_call.return_value = _make_result(
        "\x1b[31mcd: chrome:// not debuggable\x1b[0m"
    )

    backend.type_text("/some/path", "text", session=sess)

    assert mock_call.call_count == 1
    assert mock_call.call_args_list[0].args[0] == "cd %here%"


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_type_text_relative_path_focus_failure_skips_type(mock_call):
    """Relative path focus failure: no anchor (no drift to restore), so
    just `focus`, then halt. Type MUST NOT run.

    The absolute-path equivalent (with anchor + restore) is covered by
    test_type_text_absolute_path_focus_failure_skips_type_and_restores.
    """
    sess = _make_session(working_dir="/")
    mock_call.side_effect = [
        _make_result("\x1b[31mfocus: No such element\x1b[0m"),  # focus fails
        _make_result("✓ Typed"),  # should NOT be reached
    ]

    result = backend.type_text("stale_input", "secret_password", session=sess)

    # Only the focus call was made; the focus result was returned.
    assert mock_call.call_count == 1
    assert mock_call.call_args_list[0].args[0] == "focus stale_input"
    # The focus result is parsed (CLI consumes dicts, not CallToolResult).
    assert result == {
        "error": "\x1b[31mfocus: No such element\x1b[0m",
        "output": "\x1b[31mfocus: No such element\x1b[0m",
    }


def test_type_text_raises_without_session_in_non_daemon_mode():
    """Non-daemon mode: the two halves must share a lane via group_id,
    which requires a session. Without one, raise.
    """
    with pytest.raises(ValueError, match="session"):
        backend.type_text(
            "search_input", "text", use_daemon=False, session=None,
        )


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_type_text_allows_no_session_in_daemon_mode(mock_call):
    """Daemon mode shares lane via the persistent connection — session
    isn't required. Locks in the contract Copilot flagged.
    """
    mock_call.return_value = _make_result("✓\n[lane: 1]")

    # No exception. Relative path → two calls (focus + type).
    backend.type_text("search_input", "hello", use_daemon=True, session=None)

    assert mock_call.call_count == 2
    assert mock_call.call_args_list[0].args[0] == "focus search_input"
    assert mock_call.call_args_list[1].args[0] == "type hello"


def test_type_text_raises_with_empty_path():
    with pytest.raises(ValueError, match="input path is required"):
        backend.type_text("", "text", session=_make_session())
    with pytest.raises(ValueError, match="input path is required"):
        backend.type_text("/", "text", session=_make_session())


def test_type_text_rejects_newline_in_text():
    """``\\n`` in text would inject a new DOMShell command — must raise."""
    with pytest.raises(ValueError, match="newline"):
        backend.type_text("search_input", "line1\nline2")


def test_type_text_rejects_carriage_return_in_text():
    """``\\r`` is just as dangerous as ``\\n`` for DOMShell's line splitter."""
    with pytest.raises(ValueError, match="newline"):
        backend.type_text("search_input", "line1\rline2")


def test_type_text_rejects_newline_in_path():
    """A newline in the path argument also injects — guard both fields."""
    with pytest.raises(ValueError, match="newline"):
        backend.type_text("input\nclick /admin", "anything")


# ── grep: newline guard on rooted multi-step path ─────────────────────


def test_grep_rejects_newline_in_path():
    """Rooted grep interpolates path into a multi-line cd/grep/cd — reject newlines."""
    with pytest.raises(ValueError, match="newline"):
        backend.grep("Login", path="/main\nclick /admin", prev="/")


def test_grep_rejects_newline_in_pattern():
    with pytest.raises(ValueError, match="newline"):
        backend.grep("Login\nclick /admin", path="/main", prev="/")


def test_grep_rejects_newline_in_prev():
    with pytest.raises(ValueError, match="newline"):
        backend.grep("Login", path="/main", prev="/\nclick /admin")


# ── Centralized newline guard in _q ──────────────────────────────────
#
# The per-wrapper _assert_single_line calls above cover type_text and
# rooted grep with field-named error messages. The newline check inside
# _q itself catches the same class of injection for every OTHER wrapper
# that flows user input through the quoting layer (open_url, click, cd,
# cat, unrooted grep, etc.) — without needing a per-call guard at each
# site.


def test_q_rejects_line_feeds():
    with pytest.raises(ValueError, match="[Nn]ewline"):
        backend._q("foo\nbar")


def test_q_rejects_carriage_returns():
    with pytest.raises(ValueError, match="[Nn]ewline"):
        backend._q("foo\rbar")


def test_q_accepts_normal_strings():
    """Plain strings pass through to shlex.quote unchanged."""
    assert backend._q("simple") == "simple"
    assert backend._q("hello world") == "'hello world'"


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_unrooted_grep_pattern_rejects_newlines(mock_call):
    """The unrooted grep path was not field-guarded — _q catches it."""
    mock_call.return_value = {}
    with pytest.raises(ValueError, match="[Nn]ewline"):
        backend.grep("evil\nclick /admin")


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_open_url_rejects_newlines(mock_call):
    """open_url has no per-call _assert_single_line — _q must catch it."""
    mock_call.return_value = {}
    with pytest.raises(ValueError, match="[Nn]ewline"):
        backend.open_url("https://example.com\nclick /admin")


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_click_rejects_newlines(mock_call):
    """click is covered structurally by _q without a per-call guard."""
    mock_call.return_value = {}
    with pytest.raises(ValueError, match="[Nn]ewline"):
        backend.click("/main/button[0]\nclick /admin")


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_cd_rejects_newlines(mock_call):
    mock_call.return_value = {}
    with pytest.raises(ValueError, match="[Nn]ewline"):
        backend.cd("/main\nclick /admin")


# ── DOMShell lane persistence across _call_execute calls ─────────────
#
# DOMShell 2.x assigns a fresh lane (Chrome tab-group) to every new MCP
# session. In non-daemon mode each _call_execute opens its own stdio
# ClientSession, so without explicit group_id every command would land
# in a brand-new lane and lose browser state from the previous call.
# The fix: parse the trailing "[lane: <id>]" marker DOMShell appends to
# each reply, store it on the harness Session, and pass group_id=<id>
# on every subsequent call.


def _make_session(
    working_dir: str = "/",
    daemon_mode: bool = False,
    lane_id=None,
):
    """Build a minimal session fixture compatible with the backend's
    ``getattr``-style access (working_dir, daemon_mode, domshell_lane_id)."""
    return SimpleNamespace(
        working_dir=working_dir,
        daemon_mode=daemon_mode,
        domshell_lane_id=lane_id,
    )


def _make_result(text: str):
    """Build a fake CallToolResult whose ``content[0].text`` is ``text``."""
    return SimpleNamespace(content=[SimpleNamespace(text=text)])


# ── _extract_lane_id ────────────────────────────────────────────────


def test_extract_lane_id_parses_trailing_marker():
    assert backend._extract_lane_id(_make_result("✓ ls done\n[lane: 12345]")) == "12345"


def test_extract_lane_id_handles_trailing_whitespace():
    assert backend._extract_lane_id(_make_result("✓ ls done\n[lane: lane-abc]\n")) == "lane-abc"


def test_extract_lane_id_ignores_shared_marker():
    """`[lane: shared]` is DOMShell's no-isolation sentinel — must not pin to it."""
    assert backend._extract_lane_id(_make_result("✓ ls done\n[lane: shared]")) is None


def test_extract_lane_id_returns_none_when_marker_absent():
    assert backend._extract_lane_id(_make_result("✓ ls done")) is None


def test_extract_lane_id_returns_none_for_empty_text():
    assert backend._extract_lane_id(_make_result("")) is None


def test_extract_lane_id_returns_none_when_content_missing():
    assert backend._extract_lane_id(SimpleNamespace()) is None


# ── lane capture + propagation ──────────────────────────────────────


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_first_call_omits_group_id_and_captures_lane(mock_call):
    """A session with no stored lane: first call has no group_id, captures the returned lane."""
    sess = Session()  # domshell_lane_id starts as None
    mock_call.return_value = _make_result("✓\n[lane: 12345]")

    backend.click("submit_btn", session=sess)

    # The session= kwarg the wrapper passes is the Session object itself;
    # _call_execute is responsible for translating session.domshell_lane_id
    # into the wire-format group_id. We only assert the call signature here.
    assert mock_call.call_args == call("click submit_btn", False, session=sess)
    # _call_execute is mocked, so we exercise _capture_lane directly to
    # verify the parser hookup that the real _call_execute would do.
    backend._capture_lane(sess, mock_call.return_value)
    assert sess.domshell_lane_id == "12345"


def test_capture_lane_updates_session():
    sess = Session()
    backend._capture_lane(sess, _make_result("✓\n[lane: lane-XYZ]"))
    assert sess.domshell_lane_id == "lane-XYZ"


def test_capture_lane_no_op_when_session_is_none():
    """Direct backend callers without a session must not crash."""
    backend._capture_lane(None, _make_result("✓\n[lane: 12345]"))  # no exception


def test_capture_lane_no_op_when_marker_missing():
    sess = Session()
    sess.domshell_lane_id = "preexisting"
    backend._capture_lane(sess, _make_result("✓ done"))
    # No marker → don't clobber the previously-captured lane.
    assert sess.domshell_lane_id == "preexisting"


def test_capture_lane_ignores_shared_marker():
    sess = Session()
    sess.domshell_lane_id = "preexisting"
    backend._capture_lane(sess, _make_result("✓\n[lane: shared]"))
    assert sess.domshell_lane_id == "preexisting"


# ── End-to-end: lane reuse on subsequent calls ───────────────────────


def test_call_execute_includes_group_id_when_lane_is_set():
    """When session.domshell_lane_id is set, _call_execute sends it as group_id.

    We bypass the stdio_client / ClientSession plumbing entirely by patching
    them — what we want to assert is the arguments dict that gets passed to
    ``call_tool``.
    """
    sess = Session()
    sess.domshell_lane_id = "lane-7"

    fake_tool = AsyncMock(return_value=_make_result("✓\n[lane: lane-7]"))

    fake_mcp_session = AsyncMock()
    fake_mcp_session.__aenter__.return_value = fake_mcp_session
    fake_mcp_session.__aexit__.return_value = None
    fake_mcp_session.initialize = AsyncMock()
    fake_mcp_session.call_tool = fake_tool

    fake_stdio = AsyncMock()
    fake_stdio.__aenter__.return_value = (object(), object())
    fake_stdio.__aexit__.return_value = None

    with patch.object(backend, "stdio_client", return_value=fake_stdio), \
         patch.object(backend, "ClientSession", return_value=fake_mcp_session), \
         patch.object(backend, "_build_server_args", return_value=[]):
        import asyncio as _aio
        _aio.run(backend._call_execute("ls /", session=sess))

    name, arguments = fake_tool.call_args.args
    assert name == "domshell_execute"
    assert arguments == {"command": "ls /", "group_id": "lane-7"}


def test_call_execute_omits_group_id_when_lane_is_none():
    """First-call shape: no stored lane → no group_id; DOMShell auto-assigns."""
    sess = Session()  # lane is None
    fake_tool = AsyncMock(return_value=_make_result("✓\n[lane: brand-new]"))

    fake_mcp_session = AsyncMock()
    fake_mcp_session.__aenter__.return_value = fake_mcp_session
    fake_mcp_session.__aexit__.return_value = None
    fake_mcp_session.initialize = AsyncMock()
    fake_mcp_session.call_tool = fake_tool

    fake_stdio = AsyncMock()
    fake_stdio.__aenter__.return_value = (object(), object())
    fake_stdio.__aexit__.return_value = None

    with patch.object(backend, "stdio_client", return_value=fake_stdio), \
         patch.object(backend, "ClientSession", return_value=fake_mcp_session), \
         patch.object(backend, "_build_server_args", return_value=[]):
        import asyncio as _aio
        _aio.run(backend._call_execute("ls /", session=sess))

    arguments = fake_tool.call_args.args[1]
    assert "group_id" not in arguments
    # The auto-assigned lane gets captured for next time.
    assert sess.domshell_lane_id == "brand-new"


def test_distinct_sessions_have_isolated_lanes():
    """Two sessions track lanes independently — no cross-contamination."""
    s1 = Session()
    s2 = Session()
    backend._capture_lane(s1, _make_result("✓\n[lane: lane-A]"))
    backend._capture_lane(s2, _make_result("✓\n[lane: lane-B]"))
    assert s1.domshell_lane_id == "lane-A"
    assert s2.domshell_lane_id == "lane-B"


# ── Keyword-only enforcement on session= ─────────────────────────────
#
# Only ``session`` is keyword-only; ``use_daemon`` stays positional so
# pre-2.0.0 callers like ``ls("/", True)`` keep working. ``grep`` is a
# deliberate exception (fully keyword-only after the round-1 review).


def test_session_is_keyword_only_on_click():
    """Trailing positional `None` is interpreted as a 3rd positional arg
    that the wrapper doesn't accept — must raise TypeError."""
    with pytest.raises(TypeError):
        backend.click("submit_btn", False, None)  # type: ignore[misc]


def test_session_is_keyword_only_on_ls():
    with pytest.raises(TypeError):
        backend.ls("/", False, None)  # type: ignore[misc]


def test_session_is_keyword_only_on_type_text():
    with pytest.raises(TypeError):
        backend.type_text("input", "hello", False, None)  # type: ignore[misc]


def test_session_is_keyword_only_on_open_url():
    with pytest.raises(TypeError):
        backend.open_url("https://example.com", False, None)  # type: ignore[misc]


# ── Positional `use_daemon` stays valid ──────────────────────────────
#
# These guard against accidentally pulling ``use_daemon`` behind the ``*``
# in the future. Pre-2.0.0 calls written as ``ls("/", True)`` must not
# regress to ``TypeError``.


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_use_daemon_positional_on_ls(mock_call):
    mock_call.return_value = _make_result("[lane: 1]")
    backend.ls("/", True)  # use_daemon=True positionally
    # Absolute `/` with no session → 3-call sequence, use_daemon=True
    # flows through all three calls.
    assert mock_call.call_args_list == [
        call("cd %here%", True, session=None),
        call("ls", True, session=None),
        call("cd %here%", True, session=None),
    ]


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_use_daemon_positional_on_click(mock_call):
    mock_call.return_value = {}
    backend.click("submit_btn", True)
    assert mock_call.call_args == call("click submit_btn", True, session=None)


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_use_daemon_positional_on_reload(mock_call):
    mock_call.return_value = {}
    backend.reload(True)
    assert mock_call.call_args == call("refresh", True, session=None)


@patch.object(backend, "_call_execute", new_callable=AsyncMock)
def test_use_daemon_positional_on_type_text(mock_call):
    """Positional ``use_daemon`` flows through both halves of the split.

    type_text now requires a session, so pass one explicitly.
    """
    sess = _make_session(working_dir="/")
    mock_call.return_value = _make_result("✓\n[lane: 1]")
    backend.type_text("input", "hello", True, session=sess)
    # Two calls (focus then type), both with use_daemon=True positionally.
    assert mock_call.call_args_list == [
        call("focus input", True, session=sess),
        call("type hello", True, session=sess),
    ]
