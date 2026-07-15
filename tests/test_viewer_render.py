"""Behavioral tests for the viewer's pure render layer, executed under node.

The layer is extracted from <script id="render"> in viewer/index.html and run
by node. These are deliberately not source-grep assertions: a renderer that
turns untrusted agent text into markup has to be tested by running it. See
specs/2026-07-15-viewer-rendering-design.md, "Motivating defects".
"""
import re
import shutil
import subprocess
from importlib import resources

import pytest

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def render_source() -> str:
    """The pure render layer, as source text."""
    html = resources.files("debatelab").joinpath("viewer/index.html").read_text()
    m = re.search(r'<script id="render">(.*?)</script>', html, re.S)
    assert m, 'viewer/index.html has no <script id="render"> block'
    return m.group(1)


def run_js(snippet: str) -> str:
    """Run snippet with the render layer in scope; return its stdout."""
    proc = subprocess.run(
        [NODE, "-e", render_source() + "\n" + snippet],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise AssertionError(f"node exited {proc.returncode}:\n{proc.stderr}")
    return proc.stdout


def render_js(expr: str) -> str:
    """Evaluate one JS expression against the render layer; return it as text."""
    return run_js(f"process.stdout.write(String({expr}))")


def test_render_layer_is_pure():
    """The layer must stay free of the page: node has no document or window,
    so a DOM reference here is a crash the first time a test calls it. This
    is the constraint that makes every other test in this file possible."""
    source = render_source()
    for token in ("document.", "window.", "location.", "fetch("):
        assert token not in source, f"render layer touches {token}"


@needs_node
def test_esc_escapes_quotes_for_attribute_contexts():
    """Regression: esc() set textContent and read back innerHTML. HTML text
    node serialization escapes &, < and > and never quotes — safe in text,
    unsafe in an attribute. renderMarkdown (Task 2) emits href="...", so an
    unescaped " in agent text would close the attribute and open a new one."""
    out = render_js("""esc('a" onmouseover="alert(1)')""")
    assert '"' not in out
    assert "&quot;" in out


@needs_node
def test_esc_escapes_markup():
    assert render_js('esc("<b>a & b</b>")') == "&lt;b&gt;a &amp; b&lt;/b&gt;"


@needs_node
def test_esc_renders_nullish_as_empty():
    assert render_js("esc(null)") == ""
    assert render_js("esc(undefined)") == ""
    assert render_js("esc(0)") == "0"


@needs_node
def test_esc_strips_nul_so_fence_placeholders_cannot_be_forged():
    """renderMarkdown (Task 2) parks fenced code behind \\u0000-delimited
    placeholders. Stripping NUL here is what stops agent text from writing a
    placeholder of its own and having code substituted into it."""
    out = render_js(r'esc("a\u0000F0\u0000b")')
    assert "\u0000" not in out
    assert out == "aF0b"
