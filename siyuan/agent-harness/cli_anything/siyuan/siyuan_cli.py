"""cli-anything-siyuan — SiYuan (思源笔记) CLI harness.

Connects to a running SiYuan kernel via its HTTP API and provides
commands for notebooks, documents, blocks, search, and export.
"""

import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

import click

from cli_anything.siyuan.core.client import (
    SiYuanClient,
    SiYuanClientError,
    SiYuanConfig,
    load_config,
)
from cli_anything.siyuan.core.session import SessionManager
from cli_anything.siyuan.utils.siyuan_backend import check_siyuan_running


def _read_stdin() -> str:
    """Read stdin as raw bytes and decode as UTF-8 (with BOM handling).

    PowerShell pipes text using the console code page (e.g. GBK on Chinese
    Windows), which mangles CJK characters before Python sees them.  Reading
    raw bytes then decoding as UTF-8-sig is the most robust approach.
    """
    raw = sys.stdin.buffer.read()
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        return raw.decode('utf-8-sig', errors='replace')


# ── Global state ───────────────────────────────────────────────────────

_config: SiYuanConfig | None = None
_client: SiYuanClient | None = None
_session_mgr: SessionManager | None = None


def get_client() -> SiYuanClient:
    global _client, _config
    if _client is None:
        _config = load_config()
        _client = SiYuanClient(_config)
    return _client


def get_session() -> SessionManager:
    global _session_mgr
    if _session_mgr is None:
        _session_mgr = SessionManager()
        _session_mgr.load()
    return _session_mgr


# ── Click context ─────────────────────────────────────────────────────

class SiYuanContext:
    def __init__(self, json_output: bool = False):
        self.json_output = json_output
        self.client = get_client()
        self.session = get_session()


class _CatchErrors(click.Group):
    """Click Group that converts SiYuanClientError to clean CLI errors."""

    def invoke(self, ctx: click.Context) -> object:
        try:
            return super().invoke(ctx)
        except SiYuanClientError as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)


@click.group(cls=_CatchErrors, invoke_without_command=True)
@click.option("--json", "json_output", is_flag=True, help="Output in JSON format")
@click.option("--host", default="", help="SiYuan host (default: 127.0.0.1)")
@click.option("--port", default=0, type=int, help="SiYuan port (default: 6806)")
@click.option("--token", default="", help="SiYuan API token")
@click.option("--config", "config_path", default="", help="Config file path")
@click.pass_context
def cli(ctx: click.Context, json_output: bool, host: str, port: int,
        token: str, config_path: str):
    # Force UTF-8 output to handle CJK characters on Windows
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')

    """CLI for SiYuan (思源笔记) — interact with your knowledge base.

    Connects to a running SiYuan instance via its HTTP API.
    Default: http://127.0.0.1:6806

    Configure connection via ~/.siyuan-cli.json, env vars
    (SIYUAN_HOST, SIYUAN_PORT, SIYUAN_TOKEN), or CLI flags.
    """
    global _config, _client
    if host or port or token or config_path:
        cfg = load_config(config_path or None)
        _config = SiYuanConfig(
            host=host or cfg.host,
            port=port or cfg.port,
            token=token or cfg.token,
        )
        _client = SiYuanClient(_config)
    ctx.obj = SiYuanContext(json_output=json_output)

    if ctx.invoked_subcommand is None:
        if not get_client().ping():
            click.echo("Error: Cannot connect to SiYuan. Is it running?", err=True)
            click.echo("  Configure via: --host --port --token", err=True)
            click.echo("  Or set: SIYUAN_HOST, SIYUAN_PORT, SIYUAN_TOKEN", err=True)
            sys.exit(1)
        ctx.invoke(repl)


# ── REPL ───────────────────────────────────────────────────────────────

def _build_repl_commands() -> dict[str, str]:
    return {
        "notebook list": "List all notebooks",
        "notebook create <name>": "Create a notebook",
        "notebook rename <id> <name>": "Rename a notebook",
        "notebook remove <id>": "Remove a notebook",
        "doc create <notebook> <path>": "Create a document with optional --md content",
        "doc list <notebook> <path>": "List documents at path",
        "doc tree <notebook>": "List full document tree",
        "doc get <id>": "Get document info by ID",
        "block insert <parent> <data>": "Insert a block",
        "block update <id> <data>": "Update a block",
        "block delete <id>": "Delete a block",
        "block get <id>": "Get block kramdown source",
        "sql <stmt>": "Execute SQL query",
        "search <query>": "Full-text search",
        "export md <doc-id>": "Export doc as Markdown",
        "status": "Show connection and session status",
        "help": "Show this help",
        "quit": "Exit REPL",
    }


@cli.command()
@click.pass_context
def repl(ctx: click.Context):
    """Start interactive REPL mode."""
    from cli_anything.siyuan.utils.repl_skin import ReplSkin

    try:
        kernel_version = get_client().get_version()
    except SiYuanClientError:
        kernel_version = "?"
    skin = ReplSkin("siyuan", version=kernel_version)
    skin.print_banner()

    pt_session = skin.create_prompt_session()
    commands = _build_repl_commands()

    session = get_session()
    state = session.state

    while True:
        ctx_str = ""
        if state.current_notebook_name:
            ctx_str = state.current_notebook_name
        user_input = skin.get_input(
            pt_session,
            project_name=state.current_doc_path or "",
            modified=False,
            context=ctx_str,
        )

        if not user_input:
            continue

        if user_input in ("quit", "exit", "q"):
            ctx.obj.session.flush()
            break

        if user_input == "help":
            skin.help(commands)
            continue

        if user_input == "status":
            skin.status_block({
                "Connected": str(state.connected),
                "Notebook": state.current_notebook_name or "(none)",
                "Document": state.current_doc_path or "(none)",
            }, title="Status")
            continue

        try:
            _handle_repl_command(skin, user_input)
        except SiYuanClientError as e:
            skin.error(str(e))
        except Exception as e:
            skin.error(f"Error: {e}")

    skin.print_goodbye()


def _handle_repl_command(skin: Any, cmd: str) -> None:
    """Parse and execute a REPL command."""
    parts = shlex.split(cmd.strip())
    if not parts:
        return

    client = get_client()
    json_mode = "--json" in parts
    parts = [p for p in parts if p != "--json"]

    # notebook commands
    if parts[0] == "notebook":
        if len(parts) < 2:
            skin.error("Usage: notebook <list|create|rename|remove>")
            return
        sub = parts[1]
        if sub == "list":
            notebooks = client.list_notebooks()
            if json_mode:
                click.echo(json.dumps(notebooks, ensure_ascii=False))
            else:
                skin.table(["ID", "Name", "Icon", "Closed"],
                           [[n["id"], n["name"], n.get("icon", ""), str(n.get("closed", ""))]
                            for n in notebooks])
        elif sub == "create" and len(parts) >= 3:
            name = " ".join(parts[2:])
            nb = client.create_notebook(name)
            get_session().update(current_notebook_id=nb["id"], current_notebook_name=nb["name"])
            client.open_notebook(nb["id"])
            skin.success(f'Created notebook: {nb["name"]} ({nb["id"]})')
        elif sub == "rename" and len(parts) >= 4:
            client.rename_notebook(parts[2], " ".join(parts[3:]))
            skin.success("Renamed")
        elif sub == "remove" and len(parts) >= 3:
            client.remove_notebook(parts[2])
            skin.success("Removed")
        else:
            skin.error("Invalid notebook command")

    # doc commands
    elif parts[0] == "doc":
        if len(parts) < 2:
            skin.error("Usage: doc <create|list|tree|get|rename|remove|export>")
            return
        sub = parts[1]
        if sub == "create" and len(parts) >= 4:
            md = ""
            if "--md" in parts:
                idx = parts.index("--md") + 1
                if idx < len(parts):
                    md = parts[idx]
                parts = parts[:parts.index("--md")]
            nb_id = parts[2]
            doc_path = parts[3]
            doc_id = client.create_doc_with_md(nb_id, doc_path, md)
            get_session().update(current_doc_id=doc_id, current_doc_path=doc_path)
            if json_mode:
                click.echo(json.dumps({"id": doc_id}, ensure_ascii=False))
            else:
                skin.success(f"Created doc: {doc_id}")
        elif sub == "list" and len(parts) >= 4:
            docs = client.list_docs_by_path(parts[2], parts[3])
            items = docs.get("files", []) if isinstance(docs, dict) else docs
            if json_mode:
                click.echo(json.dumps(items, ensure_ascii=False))
            else:
                skin.table(["ID", "Name", "Type"],
                           [[d.get("id", ""), d.get("name", ""), d.get("type", "")]
                            for d in items])
        elif sub == "tree" and len(parts) >= 3:
            tree = client.list_doc_tree(parts[2])
            if isinstance(tree, dict):
                items = tree.get("files") or tree.get("tree") or []
            else:
                items = tree
            if json_mode:
                click.echo(json.dumps(items, ensure_ascii=False))
            else:
                skin.table(["ID", "Name", "Path"],
                           [[t.get("id", ""), t.get("name", ""), t.get("path", "")]
                            for t in items])
        elif sub == "get" and len(parts) >= 3:
            hpath = client.get_hpath_by_id(parts[2])
            if json_mode:
                click.echo(json.dumps({"hpath": hpath}, ensure_ascii=False))
            else:
                skin.success(f"Path: {hpath}")
        elif sub == "rename" and len(parts) >= 4:
            client.rename_doc_by_id(parts[2], " ".join(parts[3:]))
            skin.success("Renamed")
        elif sub == "remove" and len(parts) >= 3:
            client.remove_doc_by_id(parts[2])
            skin.success("Removed")
        elif sub == "export" and len(parts) >= 3:
            md = client.export_md_content(parts[2])
            if json_mode:
                click.echo(json.dumps(md, ensure_ascii=False))
            else:
                skin.section(md.get("hPath", ""))
                click.echo(md.get("content", ""))
        else:
            skin.error("Invalid doc command")

    # block commands
    elif parts[0] == "block":
        if len(parts) < 2:
            skin.error("Usage: block <insert|prepend|append|update|delete|get|child>")
            return
        sub = parts[1]
        if sub == "insert":
            if len(parts) < 4:
                skin.error("Usage: block insert <parent_id> <data>")
                return
            result = client.insert_block("markdown", parts[3], parent_id=parts[2])
            if json_mode:
                click.echo(json.dumps(result, ensure_ascii=False))
            else:
                skin.success("Block inserted")
        elif sub == "prepend" and len(parts) >= 4:
            client.prepend_block("markdown", parts[3], parts[2])
            skin.success("Block prepended")
        elif sub == "append" and len(parts) >= 4:
            client.append_block("markdown", parts[3], parts[2])
            skin.success("Block appended")
        elif sub == "update" and len(parts) >= 4:
            client.update_block("markdown", parts[3], parts[2])
            skin.success("Block updated")
        elif sub == "delete" and len(parts) >= 3:
            client.delete_block(parts[2])
            skin.success("Block deleted")
        elif sub == "get" and len(parts) >= 3:
            kramdown = client.get_block_kramdown(parts[2])
            if json_mode:
                click.echo(json.dumps({"kramdown": kramdown}, ensure_ascii=False))
            else:
                click.echo(kramdown)
        elif sub == "child" and len(parts) >= 3:
            children = client.get_child_blocks(parts[2])
            if json_mode:
                click.echo(json.dumps(children, ensure_ascii=False))
            else:
                skin.table(["ID", "Type", "SubType"],
                           [[c.get("id", ""), c.get("type", ""), c.get("subType", "")]
                            for c in children])
        else:
            skin.error("Invalid block command")

    # sql command
    elif parts[0] == "sql" and len(parts) >= 2:
        stmt = " ".join(parts[1:])
        results = client.query_sql(stmt)
        if json_mode:
            click.echo(json.dumps(results, ensure_ascii=False))
        elif not results:
            skin.info("No results")
        else:
            headers = list(results[0].keys())
            rows = [[str(r.get(h, "")) for h in headers] for r in results]
            skin.table(headers, rows)

    # search command
    elif parts[0] == "search" and len(parts) >= 2:
        query = " ".join(parts[1:])
        data = client.search_blocks(query)
        blocks = data.get("blocks", []) if isinstance(data, dict) else data
        if json_mode:
            click.echo(json.dumps(blocks, ensure_ascii=False))
        elif not blocks:
            skin.info("No results")
        else:
            skin.table(["ID", "Content"],
                       [[r.get("id", ""), r.get("content", "")[:80]] for r in blocks])

    # export command
    elif parts[0] == "export" and len(parts) >= 3:
        if parts[1] == "md" and len(parts) >= 3:
            md = client.export_md_content(parts[2])
            if json_mode:
                click.echo(json.dumps(md, ensure_ascii=False))
            else:
                skin.section(md.get("hPath", ""))
                click.echo(md.get("content", ""))
        else:
            skin.error("Usage: export md <doc-id>")

    else:
        skin.error(f"Unknown command: {parts[0]}")


# ── Notebook commands ──────────────────────────────────────────────────

@cli.group()
def notebook():
    """Manage notebooks (笔记本)."""


@notebook.command("list")
@click.pass_obj
def notebook_list(ctx: SiYuanContext):
    """List all notebooks."""
    notebooks = ctx.client.list_notebooks()
    if ctx.json_output:
        click.echo(json.dumps(notebooks, ensure_ascii=False))
    else:
        click.echo(f"{'ID':<30} {'Name':<30} {'Closed':<8}")
        click.echo("-" * 70)
        for nb in notebooks:
            click.echo(f"{nb['id']:<30} {nb['name']:<30} {str(nb.get('closed', '')):<8}")


@notebook.command("create")
@click.argument("name")
@click.pass_obj
def notebook_create(ctx: SiYuanContext, name: str):
    """Create a new notebook."""
    nb = ctx.client.create_notebook(name)
    if ctx.json_output:
        click.echo(json.dumps(nb, ensure_ascii=False))
    else:
        click.echo(f"Created notebook: {nb['name']} ({nb['id']})")


@notebook.command("remove")
@click.argument("notebook_id")
@click.pass_obj
def notebook_remove(ctx: SiYuanContext, notebook_id: str):
    """Remove a notebook by ID."""
    ctx.client.remove_notebook(notebook_id)
    click.echo(f"Removed notebook: {notebook_id}")


@notebook.command("rename")
@click.argument("notebook_id")
@click.argument("name")
@click.pass_obj
def notebook_rename(ctx: SiYuanContext, notebook_id: str, name: str):
    """Rename a notebook."""
    ctx.client.rename_notebook(notebook_id, name)
    click.echo(f"Renamed notebook {notebook_id} to: {name}")


@notebook.command("open")
@click.argument("notebook_id")
@click.pass_obj
def notebook_open(ctx: SiYuanContext, notebook_id: str):
    """Open a notebook."""
    ctx.client.open_notebook(notebook_id)
    name = notebook_id
    for nb in ctx.client.list_notebooks():
        if nb.get("id") == notebook_id:
            name = nb.get("name", notebook_id)
            break
    ctx.session.update(current_notebook_id=notebook_id, current_notebook_name=name)
    ctx.session.flush()
    click.echo(f"Opened notebook: {name} ({notebook_id})")


# ── Document commands ──────────────────────────────────────────────────

@cli.group()
def doc():
    """Manage documents (文档)."""


@doc.command("create")
@click.argument("notebook_id")
@click.argument("path")
@click.option("--md", default="", help="Markdown content. Use '-' or omit to read from stdin.")
@click.pass_obj
def doc_create(ctx: SiYuanContext, notebook_id: str, path: str, md: str):
    """Create a document with optional Markdown content.

    To avoid shell escaping issues with special characters
    (backticks, quotes, parentheses), pipe Markdown via stdin:
      echo "## Title" | sy doc create nb1 /test --md -
    """
    if not md or md == "-":
        md = _read_stdin()
    doc_id = ctx.client.create_doc_with_md(notebook_id, path, md)
    if ctx.json_output:
        click.echo(json.dumps({"id": doc_id}, ensure_ascii=False))
    else:
        click.echo(f"Created doc: {doc_id}")


@doc.command("list")
@click.argument("notebook_id")
@click.argument("path", default="/")
@click.pass_obj
def doc_list(ctx: SiYuanContext, notebook_id: str, path: str):
    """List documents at a path."""
    docs = ctx.client.list_docs_by_path(notebook_id, path)
    items = docs.get("files", []) if isinstance(docs, dict) else docs
    if ctx.json_output:
        click.echo(json.dumps(items, ensure_ascii=False))
    else:
        click.echo(f"{'ID':<30} {'Name':<30} {'Type':<10}")
        click.echo("-" * 70)
        for d in items:
            click.echo(f"{d.get('id', ''):<30} {d.get('name', ''):<30} {d.get('type', ''):<10}")


@doc.command("tree")
@click.argument("notebook_id")
@click.option("--path", default="/", help="Root path")
@click.option("--depth", default=-1, type=int, help="Max depth")
@click.pass_obj
def doc_tree(ctx: SiYuanContext, notebook_id: str, path: str, depth: int):
    """List document tree."""
    tree = ctx.client.list_doc_tree(notebook_id, path=path, max_depth=depth)
    if isinstance(tree, dict):
        items = tree.get("files") or tree.get("tree") or []
    else:
        items = tree
    if ctx.json_output:
        click.echo(json.dumps(items, ensure_ascii=False))
    else:
        for t in items:
            indent = "  " * t.get("depth", 0)
            click.echo(f"{indent}{t.get('name', '')}  ({t.get('id', '')})")


@doc.command("get")
@click.argument("doc_id")
@click.pass_obj
def doc_get(ctx: SiYuanContext, doc_id: str):
    """Get document info by ID."""
    hpath = ctx.client.get_hpath_by_id(doc_id)
    if ctx.json_output:
        click.echo(json.dumps({"hpath": hpath}, ensure_ascii=False))
    else:
        click.echo(f"Path: {hpath}")


@doc.command("rename")
@click.argument("doc_id")
@click.argument("title")
@click.pass_obj
def doc_rename(ctx: SiYuanContext, doc_id: str, title: str):
    """Rename a document."""
    ctx.client.rename_doc_by_id(doc_id, title)
    click.echo(f"Renamed {doc_id} to: {title}")


@doc.command("remove")
@click.argument("doc_id")
@click.pass_obj
def doc_remove(ctx: SiYuanContext, doc_id: str):
    """Remove a document."""
    ctx.client.remove_doc_by_id(doc_id)
    click.echo(f"Removed: {doc_id}")


# ── Block commands ─────────────────────────────────────────────────────

@cli.group()
def block():
    """Manage blocks (内容块)."""


@block.command("insert")
@click.argument("data", required=False)
@click.option("--previous", default="", help="Previous block ID")
@click.option("--parent", default="", help="Parent block ID")
@click.option("--next", "next_", default="", help="Next block ID")
@click.option("--data-type", default="markdown", help="Data type (markdown/dom)")
@click.pass_obj
def block_insert(ctx: SiYuanContext, data: str | None, previous: str, parent: str, next_: str, data_type: str):
    """Insert a block. Data reads from stdin when '-' or omitted."""
    if not parent and not previous and not next_:
        raise click.UsageError("An anchor is required: --parent, --previous, or --next")
    if not data or data == "-":
        data = _read_stdin()
    result = ctx.client.insert_block(data_type, data, parent_id=parent, previous_id=previous, next_id=next_)
    if ctx.json_output:
        click.echo(json.dumps(result, ensure_ascii=False))
    else:
        click.echo("Block inserted")


@block.command("update")
@click.argument("block_id")
@click.argument("data", required=False)
@click.option("--data-type", default="markdown", help="Data type")
@click.pass_obj
def block_update(ctx: SiYuanContext, block_id: str, data: str | None, data_type: str):
    """Update a block's content. Data reads from stdin when '-' or omitted."""
    if not data or data == "-":
        data = _read_stdin()
    ctx.client.update_block(data_type, data, block_id)
    click.echo(f"Updated block: {block_id}")


@block.command("delete")
@click.argument("block_id")
@click.pass_obj
def block_delete(ctx: SiYuanContext, block_id: str):
    """Delete a block."""
    ctx.client.delete_block(block_id)
    click.echo(f"Deleted block: {block_id}")


@block.command("get")
@click.argument("block_id")
@click.pass_obj
def block_get(ctx: SiYuanContext, block_id: str):
    """Get block kramdown source."""
    kramdown = ctx.client.get_block_kramdown(block_id)
    if ctx.json_output:
        click.echo(json.dumps({"kramdown": kramdown}, ensure_ascii=False))
    else:
        click.echo(kramdown)


@block.command("children")
@click.argument("block_id")
@click.pass_obj
def block_children(ctx: SiYuanContext, block_id: str):
    """Get child blocks."""
    children = ctx.client.get_child_blocks(block_id)
    if ctx.json_output:
        click.echo(json.dumps(children, ensure_ascii=False))
    else:
        for c in children:
            click.echo(f"{c.get('id', ''):<30} {c.get('type', ''):<8} {c.get('subType', '')}")


# ── SQL command ────────────────────────────────────────────────────────

@cli.command()
@click.argument("stmt")
@click.pass_obj
def sql(ctx: SiYuanContext, stmt: str):
    """Execute a SQL query on the block database.

    Example: sql "SELECT * FROM blocks WHERE content LIKE '%keyword%' LIMIT 10"
    """
    results = ctx.client.query_sql(stmt)
    if ctx.json_output:
        click.echo(json.dumps(results, ensure_ascii=False))
    elif not results:
        click.echo("No results")
    else:
        for r in results:
            click.echo(json.dumps(r, ensure_ascii=False))


# ── Search commands ────────────────────────────────────────────────────

@cli.command()
@click.argument("query")
@click.pass_obj
def search(ctx: SiYuanContext, query: str):
    """Full-text search across all blocks."""
    data = ctx.client.search_blocks(query)
    blocks = data.get("blocks", []) if isinstance(data, dict) else data
    if ctx.json_output:
        click.echo(json.dumps(blocks, ensure_ascii=False))
    elif not blocks:
        click.echo("No results")
    else:
        for r in blocks[:20]:
            click.echo(f"- {r.get('id', '')}: {r.get('content', '')[:120]}")


# ── Export commands ────────────────────────────────────────────────────

@cli.group()
def export():
    """Export content from SiYuan."""


@export.command("md")
@click.argument("doc_id")
@click.pass_obj
def export_md(ctx: SiYuanContext, doc_id: str):
    """Export a document as Markdown."""
    md = ctx.client.export_md_content(doc_id)
    if ctx.json_output:
        click.echo(json.dumps(md, ensure_ascii=False))
    else:
        click.echo(f"# {md.get('hPath', '')}")
        click.echo("")
        click.echo(md.get("content", ""))


# ── System commands ────────────────────────────────────────────────────

@cli.command()
@click.pass_obj
def version(ctx: SiYuanContext):
    """Show SiYuan kernel version."""
    ver = ctx.client.get_version()
    if ctx.json_output:
        click.echo(json.dumps({"version": ver}, ensure_ascii=False))
    else:
        click.echo(f"SiYuan version: {ver}")


@cli.command()
@click.pass_obj
def status(ctx: SiYuanContext):
    """Show connection and session status."""
    connected = ctx.client.ping()
    siyuan_ver = ctx.client.get_version() if connected else ""
    session = ctx.session
    state = session.state

    info = {
        "connected": connected,
        "siyuan_version": siyuan_ver,
        "host": ctx.client.config.host,
        "port": ctx.client.config.port,
        "current_notebook": state.current_notebook_name or "",
        "current_doc": state.current_doc_path or "",
    }

    if ctx.json_output:
        click.echo(json.dumps(info, ensure_ascii=False))
    else:
        click.echo(f"  Connected:     {info['connected']}")
        click.echo(f"  SiYuan:        v{info['siyuan_version']}")
        click.echo(f"  Host:          {info['host']}:{info['port']}")
        click.echo(f"  Notebook:      {info['current_notebook']}")
        click.echo(f"  Document:      {info['current_doc']}")


# ── Tag commands ───────────────────────────────────────────────────────

@cli.group()
def tag():
    """Manage tags (标签)."""


def _print_tags(tags: list[dict], indent: int = 0) -> None:
    """Recursively print tags with hierarchical indentation."""
    pad = "  " * indent
    for t in tags:
        if indent == 0:
            click.echo(f"{t.get('name', ''):<30} ({t.get('count', 0)})")
        else:
            click.echo(f"{pad}{t.get('name', ''):<{30 - 2 * indent}} ({t.get('count', 0)})")
        children = t.get("children")
        if children:
            _print_tags(children, indent + 1)


@tag.command("list")
@click.pass_obj
def tag_list(ctx: SiYuanContext):
    """List all tags (including nested tags)."""
    tags = ctx.client.get_tags()
    if ctx.json_output:
        click.echo(json.dumps(tags, ensure_ascii=False))
    else:
        _print_tags(tags)
