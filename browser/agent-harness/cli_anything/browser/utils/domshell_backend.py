"""DOMShell MCP client wrapper — communicates with DOMShell MCP server via stdio.

DOMShell is a browser automation tool that maps Chrome's Accessibility Tree
to a virtual filesystem. This module provides a Python interface to DOMShell's
MCP server.

Installation:
1. Install DOMShell Chrome extension from Chrome Web Store
2. Ensure npx is available: npm install -g npx

DOMShell GitHub: https://github.com/apireno/DOMShell
Chrome Web Store: https://chromewebstore.google.com/detail/domshell-%E2%80%94-browser-filesy/okcliheamhmijccjknkkplploacoidnp

DOMShell 2.0.0 (May 2026) changed the default MCP tool surface from 38
per-command tools to a single `domshell_execute` tool that accepts a
shell-style command string (multi-line supported). This wrapper targets
that single tool.
"""

import asyncio
import os
import re
import shlex
import subprocess
import shutil
from typing import Any, Optional
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# DOMShell MCP server command
# The harness connects to a running DOMShell server via domshell-proxy (stdio bridge).
# Configure via environment variables:
#   DOMSHELL_TOKEN  — auth token (required, must match the running server)
#   DOMSHELL_PORT   — MCP HTTP port of the running server (default: 3001)
DEFAULT_SERVER_CMD = "npx"


def _build_server_args() -> list[str]:
    """Build server args at call time so env var changes are honored."""
    token = os.environ.get("DOMSHELL_TOKEN", "")
    if not token:
        raise RuntimeError(
            "DOMSHELL_TOKEN environment variable is required.\n"
            "Set it to the auth token of your running DOMShell server.\n"
            "Example: export DOMSHELL_TOKEN=<token from DOMShell startup>"
        )
    port = os.environ.get("DOMSHELL_PORT", "3001")
    return [
        "-p", "@apireno/domshell",
        "domshell-proxy",
        "--port", port,
        "--token", token,
    ]

# Daemon mode: persistent MCP connection
_daemon_session: Optional[ClientSession] = None
_daemon_read: Optional[Any] = None
_daemon_write: Optional[Any] = None
_daemon_client_context: Optional[Any] = None  # Store stdio_client context manager


def _check_npx() -> bool:
    """Check if npx is available."""
    return shutil.which("npx") is not None


def _check_npx_has_domshell() -> bool:
    """Check if DOMShell package is available to npx."""
    try:
        result = subprocess.run(
            ["npx", "@apireno/domshell", "--version"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def is_available() -> tuple[bool, str]:
    """Check if DOMShell MCP server is available.

    Returns:
        (available, message): Tuple of availability status and descriptive message.

    Examples:
        >>> is_available()
        (True, "DOMShell v2.0.0 is available")
        >>> is_available()
        (False, "npx not found. Install Node.js from https://nodejs.org/")
    """
    if not _check_npx():
        return (
            False,
            "npx not found. Install Node.js from https://nodejs.org/ "
            "Then run: npm install -g npx"
        )

    if not _check_npx_has_domshell():
        return (
            False,
            "DOMShell not found. Run `npx @apireno/domshell --version` once\n"
            "Note: The first run may download the package (10-50 MB)."
        )

    # Try to get version
    try:
        result = subprocess.run(
            ["npx", "@apireno/domshell", "--version"],
            capture_output=True,
            timeout=10,
            text=True,
        )
        version = result.stdout.strip() or "unknown"
        return True, f"DOMShell {version} is available"
    except Exception as e:
        return False, f"DOMShell check failed: {e}"


def _q(arg: str) -> str:
    """Quote an argument for the DOMShell command parser (shell-style).

    Rejects newlines: DOMShell's ``domshell_execute`` splits multi-line
    input *before* shell-style quote parsing, so a ``\\n`` or ``\\r``
    inside an otherwise-quoted argument still becomes a command
    separator. Enforcing the check here means every wrapper that flows
    user input through ``_q`` is protected by default, instead of
    relying on per-call ``_assert_single_line`` at each call site.

    Wrappers may still call ``_assert_single_line`` ahead of ``_q`` when a
    field-named error message (e.g. ``"text: ..."``) is more useful than
    the generic one raised here.
    """
    if "\n" in arg or "\r" in arg:
        # Bound the echoed value so an arbitrarily large untrusted payload
        # (e.g. a multi-line paste from the page) doesn't end up verbatim
        # in error messages or downstream logs / telemetry.
        preview = arg[:80] + ("…" if len(arg) > 80 else "")
        raise ValueError(
            "Newline characters are not allowed in command arguments — "
            "DOMShell's domshell_execute treats them as command separators, "
            "so a newline inside any wrapper input would inject additional "
            f"commands. Got ({len(arg)} chars): {preview!r}"
        )
    return shlex.quote(arg)


# DOMShell 2.x appends a "[lane: <id>]" marker as the last line of every
# domshell_execute reply. We parse it out and store it on the harness
# Session so subsequent calls can pass group_id=<id> and stay pinned to
# the same Chrome tab-group (i.e. same browser state).
_LANE_LINE = re.compile(r"\[lane:\s*([^\]\s]+)\s*\]\s*$")


def _extract_lane_id(result: Any) -> Optional[str]:
    """Parse the trailing ``[lane: <id>]`` marker DOMShell appends to replies.

    Returns the lane id, or ``None`` if no marker is present, the text is
    empty, or the marker reports the default "shared" lane (which is
    DOMShell's no-isolation sentinel and not something we want to pin to).
    """
    text = ""
    content = getattr(result, "content", None)
    if content:
        for c in content:
            piece = getattr(c, "text", None)
            if piece:
                text += piece
    if not text:
        return None
    m = _LANE_LINE.search(text.strip())
    if not m:
        return None
    lane = m.group(1).strip()
    if not lane or lane == "shared":
        return None
    return lane


def _capture_lane(session: Any, result: Any) -> None:
    """Update ``session.domshell_lane_id`` from a ``_call_execute`` result."""
    if session is None:
        return
    lane = _extract_lane_id(result)
    if lane:
        session.domshell_lane_id = lane


def _translate_path(harness_path: str) -> str:
    """Translate a harness DOM path to a DOMShell command path.

    The harness models ``/`` as the focused tab's AX root. DOMShell models
    ``~/`` (and bare ``/``) as the BROWSER root — windows and tabs.
    Sending ``ls /`` verbatim would list tabs, not page elements. Strip
    the leading ``/`` so harness ``/main`` becomes DOMShell ``main``
    (relative to the lane's cwd, which IS the focused tab's AX root once
    ``page open`` has put the lane inside the tab).

    - ``""`` and ``"/"`` → ``""`` (signal: operate on lane cwd; the
      wrappers turn this into a bare command, e.g. ``ls`` or ``cd %here%``).
    - ``"/main"`` → ``"main"``
    - ``"/main/article"`` → ``"main/article"``
    - ``"main"``, ``".."``, ``"."`` → passed through unchanged.
    """
    if not harness_path or harness_path == "/":
        return ""
    if harness_path.startswith("/"):
        return harness_path[1:]
    return harness_path


def _is_error(result: Any) -> bool:
    """Best-effort check that a ``domshell_execute`` result represents an error.

    Inspects ``isError`` if the MCP SDK populated it, then ``isError`` /
    ``error`` keys on dict-shaped test fixtures, and finally scans the
    concatenated text content for a leading "error". Robust to the raw
    ``CallToolResult`` and to the ``SimpleNamespace(content=[...])``
    fixtures used in tests.
    """
    if hasattr(result, "isError") and result.isError:
        return True
    if isinstance(result, dict):
        if result.get("isError"):
            return True
        if "error" in result:
            return True
    text = ""
    content = getattr(result, "content", None)
    if content:
        for c in content:
            piece = getattr(c, "text", None)
            if piece:
                text += piece
    return text.strip().lower().startswith("error")


def _assert_single_line(field: str, value: str) -> None:
    """Reject newline characters in a user-supplied string.

    DOMShell's ``domshell_execute`` splits its ``command`` argument on
    newlines *before* shell-style quote parsing, so a literal ``\\n`` or
    ``\\r`` inside an otherwise-quoted argument escapes the quoting and
    starts a fresh DOMShell command. Guard at the wrapper layer for any
    value that gets interpolated into a multi-line command string.
    """
    if "\n" in value or "\r" in value:
        raise ValueError(
            f"{field}: newline characters are not allowed (would be interpreted "
            f"as DOMShell command separators). Got: {value!r}"
        )


async def _call_execute(
    command: str,
    use_daemon: bool = False,
    *,
    session: Any = None,
) -> Any:
    """Run a DOMShell command via the single `domshell_execute` MCP tool.

    Args:
        command: DOMShell command string. May contain newlines for multi-command
            execution — each line runs in order in the same shell state.
        use_daemon: If True, use persistent daemon connection (if available)
        session: Harness ``Session`` whose ``domshell_lane_id`` should be
            forwarded as ``group_id`` (when set) and updated from the result
            (when DOMShell returns a ``[lane: <id>]`` marker). Pass ``None``
            for one-off direct calls that don't need cross-call state.

    Returns:
        Tool result as returned by MCP server

    Raises:
        RuntimeError: If MCP server is not available or tool call fails
    """
    global _daemon_session, _daemon_read, _daemon_write

    arguments: dict[str, Any] = {"command": command}
    # Reuse the previously-captured DOMShell lane so this call lands in the
    # same Chrome tab-group as the prior commands in this session. Without
    # this, every fresh stdio ClientSession would be assigned a brand-new
    # lane and `page open` / `fs ls` etc. would run in disjoint browser
    # state. The very first call leaves group_id unset so DOMShell
    # auto-assigns; _capture_lane stores that id on the session for the
    # next call.
    if session is not None and getattr(session, "domshell_lane_id", None):
        arguments["group_id"] = session.domshell_lane_id

    if use_daemon and _daemon_session is not None:
        # Use persistent daemon connection
        try:
            result = await _daemon_session.call_tool(
                "domshell_execute", arguments
            )
            _capture_lane(session, result)
            return result
        except Exception:
            # Daemon died, fall back to spawning new server
            await _stop_daemon()

    # Spawn new MCP server process
    server_params = StdioServerParameters(
        command=DEFAULT_SERVER_CMD,
        args=_build_server_args()
    )

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as mcp_session:
                await mcp_session.initialize()
                result = await mcp_session.call_tool(
                    "domshell_execute", arguments
                )
                _capture_lane(session, result)
                return result
    except Exception as e:
        raise RuntimeError(
            f"DOMShell MCP call failed: {e}\n"
            f"Ensure Chrome is running with DOMShell extension installed.\n"
            f"Chrome Web Store: https://chromewebstore.google.com/detail/domshell"
        ) from e

# NOTE: Known limitation - Daemon mode uses asyncio.run() per tool call (in sync wrappers).
# Each asyncio.run() creates a new event loop. Async IO objects created in one loop
# (like the daemon session) may have issues when accessed from subsequent calls that
# create new loops. This is a documented limitation for v1; future work should use
# a single long-lived event loop (e.g., background thread + run_coroutine_threadsafe).
async def _start_daemon() -> bool:
    """Start persistent daemon mode.

    Returns:
        True if daemon started successfully

    Raises:
        RuntimeError: If daemon fails to start
    """
    global _daemon_session, _daemon_read, _daemon_write, _daemon_client_context

    if _daemon_session is not None:
        return True  # Already running

    server_params = StdioServerParameters(
        command=DEFAULT_SERVER_CMD,
        args=_build_server_args()
    )

    try:
        # Store the context manager so we can properly clean it up later
        _daemon_client_context = stdio_client(server_params)
        _daemon_read, _daemon_write = await _daemon_client_context.__aenter__()
        _daemon_session = ClientSession(_daemon_read, _daemon_write)
        await _daemon_session.__aenter__()
        await _daemon_session.initialize()
        return True
    except Exception as e:
        _daemon_session = None
        _daemon_read = None
        _daemon_write = None
        _daemon_client_context = None
        raise RuntimeError(f"Failed to start DOMShell daemon: {e}") from e


async def _stop_daemon() -> None:
    """Stop persistent daemon mode."""
    global _daemon_session, _daemon_read, _daemon_write, _daemon_client_context

    if _daemon_session is None:
        return

    try:
        await _daemon_session.__aexit__(None, None, None)
        if _daemon_client_context:
            await _daemon_client_context.__aexit__(None, None, None)
    except Exception:
        pass  # Ignore cleanup errors
    finally:
        _daemon_session = None
        _daemon_read = None
        _daemon_write = None
        _daemon_client_context = None


def daemon_started() -> bool:
    """Check if daemon mode is active."""
    return _daemon_session is not None


# ── Sync wrappers for each DOMShell command ──────────────────────────
#
# Each wrapper builds a shell-style command string and dispatches to
# `domshell_execute`. The public Python API is unchanged from the
# pre-2.0.0 per-tool wrappers.

def ls(path: str = "/", use_daemon: bool = False, *, session: Any = None) -> dict:
    """List directory contents in the accessibility tree.

    Args:
        path: Path in accessibility tree (e.g., "/", "/main", "/main/div[0]")
        use_daemon: Use persistent daemon connection if available
        session: Harness session whose DOMShell lane id is reused / updated

    Returns:
        Dict with 'entries' key containing list of accessible elements

    Example:
        >>> ls("/")
        {"path": "/", "entries": [{"name": "main", "role": "landmark", ...}]}
    """
    translated = _translate_path(path)
    command = f"ls {_q(translated)}" if translated else "ls"
    return asyncio.run(_call_execute(command, use_daemon, session=session))


def cd(path: str, use_daemon: bool = False, *, session: Any = None) -> dict:
    """Change directory in the accessibility tree.

    Args:
        path: Target path
        use_daemon: Use persistent daemon connection if available
        session: Harness session whose DOMShell lane id is reused / updated

    Returns:
        Dict with 'path' key confirming current location

    Example:
        >>> cd("/main/div[0]")
        {"path": "/main/div[0]", "element": {...}}
    """
    translated = _translate_path(path)
    # Harness `cd /` means "back to focused-tab AX root" — DOMShell's
    # equivalent is `cd %here%`, which jumps to the tab root regardless
    # of where the lane wandered to.
    command = "cd %here%" if not translated else f"cd {_q(translated)}"
    return asyncio.run(_call_execute(command, use_daemon, session=session))


def cat(path: str, use_daemon: bool = False, *, session: Any = None) -> dict:
    """Read element content from the accessibility tree.

    Args:
        path: Path to element
        use_daemon: Use persistent daemon connection if available
        session: Harness session whose DOMShell lane id is reused / updated

    Returns:
        Dict with element details including text, role, attributes

    Example:
        >>> cat("/main/button[0]")
        {"name": "Submit", "role": "button", "text": "Submit", ...}
    """
    translated = _translate_path(path)
    if not translated:
        raise ValueError(
            "cat: an element name is required — cannot cat the tab root. "
            "Use `ls` to list the root's children, or pass a specific name."
        )
    return asyncio.run(_call_execute(f"cat {_q(translated)}", use_daemon, session=session))


def grep(
    pattern: str,
    *,
    path: str = "",
    prev: str = "/",
    use_daemon: bool = False,
    session: Any = None,
) -> dict:
    """Search for pattern in the accessibility tree.

    When ``path`` is provided and is not ``/``, the search is rooted at that
    path: ``cd`` into it, ``grep``, then ``cd`` back to ``prev`` — sent as one
    multi-line ``domshell_execute`` call so all three lines share an MCP
    session (and therefore a DOMShell lane / cwd). Each ``_call_execute`` in
    non-daemon mode opens a fresh stdio session that lands in its own
    DOMShell 2.x lane, so splitting cd/grep/restore across separate calls
    would lose the cwd between them. The trailing ``cd prev`` is delivered as
    the final line of the same command and runs even if ``grep`` errors —
    DOMShell's multi-line splitter continues past errors (see
    `apireno/DOMShell#46 <https://github.com/apireno/DOMShell/issues/46>`_).

    ``path``, ``prev``, and ``use_daemon`` are keyword-only to prevent silent
    breakage of callers written against the pre-migration positional
    signature ``grep(pattern, use_daemon)``.

    Args:
        pattern: Text pattern to search for
        path: Optional path to root the search at. If empty or "/", searches
            from the server-side current working directory.
        prev: Path to restore as cwd after the search. Used only when
            ``path`` is provided. Defaults to "/".
        use_daemon: Use persistent daemon connection if available

    Returns:
        Dict with 'matches' key containing list of matching elements

    Example:
        >>> grep("Login")
        {"matches": ["/main/button[0]", "/main/link[1]"]}
        >>> grep("Login", path="/main")
        {"matches": ["/main/button[0]"]}
    """
    _assert_single_line("pattern", pattern)
    translated_path = _translate_path(path)
    translated_prev = _translate_path(prev)
    if translated_path:
        _assert_single_line("path", path)
        _assert_single_line("prev", prev)
        # Harness `/` (the default for `prev`) means "back to focused-tab
        # AX root", which is DOMShell's `cd %here%`. Any other restore
        # path becomes `cd <translated>`.
        restore = (
            f"cd {_q(translated_prev)}" if translated_prev else "cd %here%"
        )
        command = f"cd {_q(translated_path)}\ngrep {_q(pattern)}\n{restore}"
        return asyncio.run(_call_execute(command, use_daemon, session=session))
    return asyncio.run(
        _call_execute(f"grep {_q(pattern)}", use_daemon, session=session)
    )


def click(path: str, use_daemon: bool = False, *, session: Any = None) -> dict:
    """Click an element in the accessibility tree.

    Args:
        path: Path to element to click
        use_daemon: Use persistent daemon connection if available
        session: Harness session whose DOMShell lane id is reused / updated

    Returns:
        Dict with action result

    Example:
        >>> click("/main/button[0]")
        {"action": "click", "path": "/main/button[0]", "status": "success"}
    """
    translated = _translate_path(path)
    if not translated:
        raise ValueError(
            "click: an element name is required — cannot click the tab root."
        )
    return asyncio.run(
        _call_execute(f"click {_q(translated)}", use_daemon, session=session)
    )


def open_url(url: str, use_daemon: bool = False, *, session: Any = None) -> dict:
    """Navigate to a URL in Chrome.

    Args:
        url: URL to navigate to
        use_daemon: Use persistent daemon connection if available
        session: Harness session whose DOMShell lane id is reused / updated

    Returns:
        Dict with navigation result

    Example:
        >>> open_url("https://example.com")
        {"url": "https://example.com", "status": "loaded"}
    """
    return asyncio.run(
        _call_execute(f"open {_q(url)}", use_daemon, session=session)
    )


def reload(use_daemon: bool = False, *, session: Any = None) -> dict:
    """Reload the current page.

    Args:
        use_daemon: Use persistent daemon connection if available
        session: Harness session whose DOMShell lane id is reused / updated

    Returns:
        Dict with reload result
    """
    return asyncio.run(_call_execute("refresh", use_daemon, session=session))


def back(use_daemon: bool = False, *, session: Any = None) -> dict:
    """Navigate back in history.

    Args:
        use_daemon: Use persistent daemon connection if available
        session: Harness session whose DOMShell lane id is reused / updated

    Returns:
        Dict with navigation result
    """
    return asyncio.run(_call_execute("back", use_daemon, session=session))


def forward(use_daemon: bool = False, *, session: Any = None) -> dict:
    """Navigate forward in history.

    Args:
        use_daemon: Use persistent daemon connection if available
        session: Harness session whose DOMShell lane id is reused / updated

    Returns:
        Dict with navigation result
    """
    return asyncio.run(_call_execute("forward", use_daemon, session=session))


def type_text(
    path: str,
    text: str,
    use_daemon: bool = False,
    *,
    session: Any = None,
) -> dict:
    """Type text into an input element.

    Issued as two separate ``domshell_execute`` calls — ``focus``, check
    for error, then ``type`` only if ``focus`` succeeded. Both share the
    persisted lane id (via ``session``), so the focus state from the
    first call carries into the second.

    Why split: DOMShell's multi-line splitter continues past per-line
    errors (apireno/DOMShell#46). That's the right semantic for cleanup
    chains like ``cd / grep / cd back`` (the restore must always run)
    but the WRONG semantic for safety chains like ``focus / type`` — a
    failed ``focus`` followed by a successful ``type`` would dispatch
    keys into whatever was previously focused (potentially a password
    field). Halting between focus and type prevents that.

    Args:
        path: Path to input element
        text: Text to type
        use_daemon: Use persistent daemon connection if available
        session: Harness session whose DOMShell lane id is reused / updated

    Returns:
        Dict with action result. If ``focus`` errors, returns the focus
        result without calling ``type``.

    Raises:
        ValueError: If ``path`` or ``text`` contains a newline. DOMShell's
            ``domshell_execute`` treats newlines as command separators, so
            an embedded newline would inject additional commands. Split
            into multiple ``type_text`` calls for multi-line input.
    """
    _assert_single_line("path", path)
    _assert_single_line("text", text)
    translated_path = _translate_path(path)
    if not translated_path:
        raise ValueError(
            "type_text: an element name is required — cannot focus the tab root."
        )
    # Relies on DOMShell serializing commands within a lane: the `type`
    # call below cannot be preempted by another agent's `focus` on the
    # same lane between these two _call_execute boundaries. If that
    # contract ever changes upstream, this needs to revert to a single
    # multi-line call with an alternative safety story.
    focus_result = asyncio.run(_call_execute(
        f"focus {_q(translated_path)}", use_daemon, session=session,
    ))
    if _is_error(focus_result):
        return focus_result  # don't type — focus didn't land
    return asyncio.run(_call_execute(
        f"type {_q(text)}", use_daemon, session=session,
    ))


# ── Daemon control functions ───────────────────────────────────────────

def start_daemon() -> bool:
    """Start persistent daemon mode (sync wrapper).

    Returns:
        True if daemon started successfully

    Raises:
        RuntimeError: If daemon fails to start
    """
    return asyncio.run(_start_daemon())


def stop_daemon() -> None:
    """Stop persistent daemon mode (sync wrapper)."""
    asyncio.run(_stop_daemon())
