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


@needs_node
def test_script_in_agent_text_renders_inert():
    """The headline regression. An agent writes <script>; the viewer must
    display it, never run it. Escape-first is what guarantees this: the tag
    is &lt;script&gt; before any rule that emits markup has run."""
    out = render_js(r'renderMarkdown("<script>alert(1)</script>")')
    assert "<script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


@needs_node
def test_img_onerror_in_agent_text_renders_inert():
    out = render_js(r"""renderMarkdown('<img src=x onerror=alert(1)>')""")
    assert "<img" not in out
    assert "&lt;img" in out


@needs_node
def test_javascript_href_renders_as_text_not_a_link():
    """The one hole escape-first does not close: renderMarkdown emits this
    href itself, so escaping the input cannot help. An allowlist can."""
    out = render_js(r'renderMarkdown("[click](javascript:alert(1))")')
    assert "<a " not in out
    assert "javascript:alert(1)" in out


@needs_node
def test_href_allowlist_is_not_a_blocklist():
    """Case games and unknown schemes fail the allowlist by default."""
    for href in ("JaVaScRiPt:alert(1)", "data:text/html,<b>x", "vbscript:x"):
        out = render_js(f'renderMarkdown("[click]({href})")')
        assert "<a " not in out, f"{href} produced a link"


@needs_node
def test_allowed_schemes_render_as_links():
    out = render_js(r'renderMarkdown("[docs](https://example.com/a)")')
    assert '<a href="https://example.com/a"' in out
    assert ">docs</a>" in out
    assert render_js(r'renderMarkdown("[m](mailto:a@b.c)")').count("<a ") == 1
    assert render_js(r'renderMarkdown("[h](http://a.b)")').count("<a ") == 1


@needs_node
def test_quote_in_href_cannot_break_out_of_the_attribute():
    out = render_js(r"""renderMarkdown('[x](https://a" onmouseover="alert(1))')""")
    assert 'onmouseover="alert(1)"' not in out
    assert "&quot;" in out or "<a " not in out


@needs_node
def test_headings_render_below_the_page_heading_level():
    """Agent h1 must not compete with the page's own h1/h2 chrome."""
    assert "<h3>Title</h3>" in render_js(r'renderMarkdown("# Title")')
    assert "<h4>Sub</h4>" in render_js(r'renderMarkdown("## Sub")')
    assert "<h6>Deep</h6>" in render_js(r'renderMarkdown("###### Deep")')


@needs_node
def test_emphasis_and_inline_code_render():
    assert "<strong>b</strong>" in render_js(r'renderMarkdown("**b**")')
    assert "<em>i</em>" in render_js(r'renderMarkdown("*i*")')
    assert "<em>u</em>" in render_js(r'renderMarkdown("_u_")')
    assert "<code>x()</code>" in render_js(r'renderMarkdown("`x()`")')


@needs_node
def test_lists_render():
    ul = render_js(r'renderMarkdown("- one\n- two")')
    assert ul == "<ul><li>one</li><li>two</li></ul>"
    ol = render_js(r'renderMarkdown("1. one\n2. two")')
    assert ol == "<ol><li>one</li><li>two</li></ol>"


@needs_node
def test_blockquote_renders_despite_the_marker_being_escaped_first():
    """> is &gt; by the time block rules run — the block rule has to match
    the escaped form. A rule written against the raw marker silently never
    fires, and the text renders as a paragraph."""
    out = render_js(r'renderMarkdown("> quoted")')
    assert out == "<blockquote>quoted</blockquote>"


@needs_node
def test_paragraphs_split_on_blank_lines():
    out = render_js(r'renderMarkdown("one\n\ntwo")')
    assert out == "<p>one</p><p>two</p>"


@needs_node
def test_fenced_code_renders_and_inline_rules_do_not_fire_inside_it():
    """The reason fences are parked behind placeholders before inline rules
    run: an agent's code sample must survive verbatim."""
    out = render_js(r'renderMarkdown("```\n**not bold** `not code`\n```")')
    assert "<strong>" not in out
    assert "<code>**not bold** `not code`\n</code>" in out
    assert "<pre" in out


@needs_node
def test_inline_code_is_literal():
    out = render_js(r'renderMarkdown("`**not bold**`")')
    assert "<strong>" not in out
    assert "<code>**not bold**</code>" in out


@needs_node
def test_fenced_code_is_still_escaped():
    out = render_js(r'renderMarkdown("```\n<script>alert(1)</script>\n```")')
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


@needs_node
def test_forged_fence_placeholder_in_agent_text_is_inert():
    """Agent text containing a NUL-delimited placeholder must not have code
    substituted into it. esc strips NUL, so the forgery cannot survive."""
    out = render_js(r'renderMarkdown("a\u0000F0\u0000b")')
    assert "\u0000" not in out
    assert "aF0b" in out


@needs_node
def test_empty_and_nullish_render_empty():
    assert render_js("renderMarkdown(null)") == ""
    assert render_js('renderMarkdown("")') == ""
    assert render_js(r'renderMarkdown("   \n  ")') == ""


@needs_node
def test_bookkeeping_events_are_not_content():
    """Regression: eventCard rendered every event as a card. A one-round
    three-agent debate emits 43 events, 25 of them bookkeeping and 23 of
    them carrying content:"" — so the viewer drew 23 empty <pre> blocks."""
    for t in ("debate_created", "run_config", "roster_changed",
              "phase_started", "phase_completed"):
        assert render_js(f'classifyEvent("{t}")') == "structure"
    assert render_js('classifyEvent("agent_call")') == "telemetry"


@needs_node
def test_debate_content_is_content():
    for t in ("proposal", "critique", "revision", "nomination", "vote",
              "abstained", "candidate", "consensus", "no_consensus", "error",
              "human_decision", "fallback_candidate", "nomination_dropped",
              "nomination_retry"):
        assert render_js(f'classifyEvent("{t}")') == "content"


@needs_node
def test_unknown_event_types_default_to_content():
    """The load-bearing half of the rule. Two cycles added event types the
    viewer had no rule for and silently degraded it; a third must degrade to
    a card, never to a blank page."""
    assert render_js('classifyEvent("future_event_from_cycle_six")') == "content"
    assert render_js("classifyEvent(undefined)") == "content"


@needs_node
def test_event_card_renders_content_as_markdown():
    out = render_js(
        r'eventCard({agent:"claude", type:"proposal", content:"# Plan\n- one"}, "")'
    )
    assert "<h3>Plan</h3>" in out
    assert "<li>one</li>" in out
    assert "claude" in out
    assert "proposal" in out


@needs_node
def test_event_card_escapes_the_agent_name_and_type():
    out = render_js(
        r'eventCard({agent:"<script>x</script>", type:"proposal", content:"hi"}, "")'
    )
    assert "<script>" not in out


@needs_node
def test_event_card_shows_the_vote_verdict():
    out = render_js(
        r'eventCard({agent:"a", type:"vote", verdict:"accept", content:"ok"}, "")'
    )
    assert 'class="vote-accept"' in out
    assert "accept" in out


@needs_node
def test_event_card_labels_a_system_event_as_system():
    out = render_js(r'eventCard({agent:null, type:"consensus", content:"x"}, "")')
    assert "system" in out


import json


def js_events(events) -> str:
    """Serialize a Python event list into a JS literal for the harness."""
    return json.dumps(events)


@needs_node
def test_rounds_group_by_phase_in_the_order_the_phases_ran():
    events = js_events([
        {"round": 1, "phase": "propose", "type": "phase_started"},
        {"round": 1, "phase": "propose", "type": "proposal",
         "agent": "a", "content": "p"},
        {"round": 1, "phase": "propose", "type": "phase_completed"},
        {"round": 1, "phase": "critique", "type": "phase_started"},
        {"round": 1, "phase": "critique", "type": "critique",
         "agent": "a", "content": "c"},
        {"round": 1, "phase": "critique", "type": "phase_completed"},
    ])
    out = render_js(
        f"JSON.stringify(groupRounds({events}).map("
        f"r => [r.round, r.phases.map(p => p.phase)]))"
    )
    assert json.loads(out) == [[1, ["propose", "critique"]]]


@needs_node
def test_header_events_do_not_create_a_round_zero():
    """Regression: debate_created and run_config carry round 0, so the viewer
    drew a 'Round 0' heading holding two bookkeeping cards."""
    events = js_events([
        {"round": 0, "phase": "create", "type": "debate_created",
         "content": "T"},
        {"round": 0, "phase": "run", "type": "run_config", "content": "..."},
        {"round": 1, "phase": "propose", "type": "phase_started"},
        {"round": 1, "phase": "propose", "type": "proposal",
         "agent": "a", "content": "p"},
    ])
    out = render_js(f"JSON.stringify(groupRounds({events}).map(r => r.round))")
    assert json.loads(out) == [1]


@needs_node
def test_only_content_events_land_in_a_phase():
    events = js_events([
        {"round": 1, "phase": "propose", "type": "phase_started"},
        {"round": 1, "phase": "propose", "type": "agent_call",
         "agent": "a", "duration_ms": 10, "ok": True, "content": ""},
        {"round": 1, "phase": "propose", "type": "proposal",
         "agent": "a", "content": "p"},
        {"round": 1, "phase": "propose", "type": "phase_completed"},
    ])
    out = render_js(
        f"JSON.stringify(groupRounds({events})[0].phases[0].events"
        f".map(e => e.type))"
    )
    assert json.loads(out) == ["proposal"]


@needs_node
def test_a_phase_that_started_and_never_completed_is_halted():
    """The boundary the replay cycle paid for: a completed phase used to be
    indistinguishable from a halted one. 20260714-...-furt-2 is this shape —
    propose raised DebateHalted and never completed."""
    events = js_events([
        {"round": 1, "phase": "propose", "type": "phase_started"},
        {"round": 1, "phase": "propose", "type": "abstained",
         "agent": "a", "content": "boom"},
    ])
    out = render_js(f"JSON.stringify(groupRounds({events})[0].phases[0])")
    phase = json.loads(out)
    assert phase["started"] is True
    assert phase["completed"] is False
    assert phase["halted"] is True


@needs_node
def test_a_completed_phase_is_not_halted():
    events = js_events([
        {"round": 1, "phase": "propose", "type": "phase_started"},
        {"round": 1, "phase": "propose", "type": "proposal",
         "agent": "a", "content": "p"},
        {"round": 1, "phase": "propose", "type": "phase_completed"},
    ])
    out = render_js(f"JSON.stringify(groupRounds({events})[0].phases[0].halted)")
    assert json.loads(out) is False


@needs_node
def test_legacy_transcripts_without_phase_delimiters_still_group():
    """The four committed debates predate phase_started. First-seen order of
    the phase field is the same answer for a debate that never resumed, so
    the fallback is the same code path, not a second one."""
    events = js_events([
        {"round": 1, "phase": "propose", "type": "proposal",
         "agent": "a", "content": "p"},
        {"round": 1, "phase": "critique", "type": "critique",
         "agent": "a", "content": "c"},
        {"round": 2, "phase": "propose", "type": "proposal",
         "agent": "a", "content": "p2"},
    ])
    out = render_js(
        f"JSON.stringify(groupRounds({events}).map("
        f"r => [r.round, r.phases.map(p => p.phase), r.phases[0].halted]))"
    )
    assert json.loads(out) == [[1, ["propose", "critique"], False],
                              [2, ["propose"], False]]
