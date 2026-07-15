# Viewer Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the viewer show the debate instead of the machinery around it, and make agent markdown readable without letting agent text execute.

**Architecture:** Six tasks, all in `debatelab/viewer/index.html`. Task 1 splits the file's one `<script>` into a pure `<script id="render">` layer (no DOM, no fetch) plus the existing app script, and makes `esc` attribute-safe — this is what lets every later task be tested by running it under node instead of grepping its source. Tasks 2–5 build the render layer bottom-up: `renderMarkdown`, then `classifyEvent`, then `groupRounds`, then `pairTelemetry`. Task 6 wires the hero panel to `result.json` and rewires `showDebate` to use all of it. No Python module changes: `orchestrator.py`, `protocol.py`, `replay.py`, `result.py`, and `store.py` are read from and never modified.

**Tech Stack:** Vanilla ES2020 in one HTML file, no dependencies, no CDN. Python ≥ 3.10 + pytest for the harness; node (any modern version) for the render tests, skipped when absent.

**Spec:** `specs/2026-07-15-viewer-rendering-design.md`

## Global Constraints

- **No new runtime dependencies.** The viewer is one self-contained HTML file: no CDN, no npm, no vendored JS. Runtime deps stay PyYAML only.
- **No Python module in `debatelab/` may be modified by any task in this plan.** The only Python changes are in `tests/`. If a task appears to need an orchestrator or store change, stop — that is a spec violation, not a plan gap.
- **The `<script id="render">` block must contain no DOM, `fetch`, or page-global access.** No `document.`, `window.`, `location.`, or `fetch(`. Task 1 Step 1 enforces this with a test that greps the block for those tokens, and every later task keeps it passing. This is what makes the block runnable under node.
- **`esc()` runs before any rule inserts markup.** Every renderer in the render layer takes untrusted agent text and escapes it first. No function may insert agent text into markup that has not been through `esc`.
- **Link `href` values carry a scheme allowlist** — `http:`, `https:`, `mailto:` only. Allowlist, never blocklist (spec §2).
- **An unrecognized event type renders as a content card.** Never dropped, never thrown on. This is the defect the spec exists to repair; a task that makes an unknown type vanish has failed (spec §1).
- **The hero renders prose only when `result.status === "approved"`.** Unapproved candidate text keeps its existing "Candidate answer (from X)" heading below the hero (spec §3).
- The four committed debates in `debates/` have no `result.json` and must keep working: no hero, everything else intact.
- Commit messages: conventional style (`feat:`, `fix:`, `test:`), **no attribution trailers of any kind**.
- All commands run from repo root `/home/bossbaby/Desktop/fix-me/ai-debate-lab`; Python is `.venv/bin/python`.
- Baseline before starting: `.venv/bin/python -m pytest -q` ⇒ **312 passed** in ~4s. The suite must stay single-digit seconds.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `debatelab/viewer/index.html` `<script id="render">` **(new block)** | Pure render layer: `esc`, `renderMarkdown`, `classifyEvent`, `groupRounds`, `pairTelemetry`, `telemetryNote`, `renderHero`, `eventCard` | 1–6 |
| `debatelab/viewer/index.html` `<script>` (existing) | App wiring: fetch, routing, polling, `showList`, `showDebate` | 1, 6 |
| `debatelab/viewer/index.html` `<style>` | Classes for hero, phase groups, telemetry notes | 2, 4, 5, 6 |
| `tests/test_viewer_render.py` **(new)** | Node harness + all render-layer behavior tests | 1–6 |
| `tests/test_serve.py` | Drops one source-grep test (Task 1); gains `result.json` serving tests (Task 6) | 1, 6 |

**Why one file and not `viewer/md.js`:** `make_server` (`cli.py:298-319`) special-cases `/` and `/index.html` and roots everything else at the *debates* directory, so `/md.js` would resolve against `debates/md.js` and 404. Splitting needs a new route plus a `pyproject.toml` `package-data` change, for ~80 lines. See spec, "Design constraint".

---

### Task 1: A pure render layer and an attribute-safe `esc`

**Files:**
- Modify: `debatelab/viewer/index.html:54-64` (open a `<script id="render">` block before the app script; move `esc` into it, rewritten)
- Create: `tests/test_viewer_render.py`
- Modify: `tests/test_serve.py:86-89` (delete `test_viewer_escapes_index_and_state_round_values`)

**Interfaces:**
- Consumes: nothing
- Produces:
  - A `<script id="render">…</script>` block in `index.html`, ahead of the app `<script>`, containing only pure functions.
  - `esc(s) -> string` — escapes `&`, `<`, `>`, `"`, `'` and strips `\u0000`. Safe in both text and attribute contexts.
  - `tests/test_viewer_render.py` helpers: `render_source() -> str`, `run_js(snippet) -> str`, `render_js(expr) -> str`, and the `needs_node` mark. Every later task's tests import or re-use these.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_viewer_render.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_viewer_render.py -q`
Expected: FAIL — `AssertionError: viewer/index.html has no <script id="render"> block`

- [ ] **Step 3: Add the render block and rewrite `esc`**

In `debatelab/viewer/index.html`, replace lines 54-64 — which today open `<script>` and define `esc` with a DOM round-trip — with a pure render block followed by the app script:

```html
<script id="render">
/* Pure render layer: no DOM, no fetch, no page globals.
   tests/test_viewer_render.py extracts this block and runs it under node. */
const ESCAPES = {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"};

function esc(s) {
  /* Quotes are escaped because the markdown renderer emits href="..." — the
     textContent/innerHTML trick this replaced escapes &, < and > only, which
     is safe in text and unsafe in an attribute. NUL is stripped so agent text
     cannot forge renderMarkdown's fenced-code placeholders. */
  return String(s ?? "")
    .replace(/\u0000/g, "")
    .replace(/[&<>"']/g, c => ESCAPES[c]);
}
</script>
<script>
const app = document.getElementById("app");
const back = document.getElementById("back");
let pollTimer = null;
let routeGeneration = 0;

async function fetchJSON(url) {
```

Everything from `async function fetchJSON(url)` onward (old lines 65-184) stays exactly as it is. The old `esc` definition is gone — the app script calls the render layer's one instead, which works because both are page-global scripts and the render block is parsed first.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_viewer_render.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Delete the superseded source-grep test**

Delete `test_viewer_escapes_index_and_state_round_values` from `tests/test_serve.py:86-89` entirely. It asserted that the string `round ${esc(String(e.round))}` appears in the source; `test_esc_escapes_markup` now tests the property that test was reaching for. The other three grep tests in that file cover app-layer behavior (polling, partial JSONL, route generation) that the render harness cannot reach, and stay.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 317 passed (312 baseline − 1 deleted + 6 new)

- [ ] **Step 7: Commit**

```bash
git add debatelab/viewer/index.html tests/test_viewer_render.py tests/test_serve.py
git commit -m "refactor: split a pure render layer out of the viewer

esc() set textContent and read back innerHTML, which escapes &, < and >
and never quotes. That is safe in a text node and unsafe in an attribute,
and the markdown renderer is about to emit href=\"...\". Replace it with
explicit escaping that covers quotes, and strip NUL so the renderer's
fenced-code placeholders cannot be forged by agent text.

The layer is DOM-free so node can run it, which is what lets the tests
assert behavior instead of grepping index.html for its own source."
```

---

### Task 2: `renderMarkdown` — escape-first subset renderer

**Files:**
- Modify: `debatelab/viewer/index.html` (append to the `<script id="render">` block), `<style>` (add `.md` rules)
- Test: `tests/test_viewer_render.py` (append)

**Interfaces:**
- Consumes: `esc(s)` from Task 1
- Produces:
  - `renderMarkdown(text) -> html` — the only function that turns agent text into markup. Used by Tasks 3 and 6.
  - `inline(s) -> html` — inline rules over an already-escaped string. Internal to the render layer; no other task calls it directly.
  - `MD_SCHEMES` — the `href` allowlist regex.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_viewer_render.py`:

```python
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
    assert render_js('renderMarkdown("   \n  ")') == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_viewer_render.py -q`
Expected: FAIL — `node exited 1: ReferenceError: renderMarkdown is not defined`

- [ ] **Step 3: Implement the renderer**

Append inside the `<script id="render">` block, after `esc`:

```js
const MD_SCHEMES = /^(https?:|mailto:)/i;

function inline(s) {
  /* s is already escaped. Inline code is parked first so emphasis rules
     cannot fire inside it. */
  const codes = [];
  let t = s.replace(/`([^`]+)`/g, (_, c) => {
    codes.push(c);
    return "\u0000C" + (codes.length - 1) + "\u0000";
  });
  t = t
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (whole, label, href) =>
      MD_SCHEMES.test(href)
        ? `<a href="${href}" rel="noopener noreferrer" target="_blank">${label}</a>`
        : whole)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/_([^_]+)_/g, "<em>$1</em>");
  return t.replace(/\u0000C(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
}

function renderMarkdown(text) {
  /* Escape first, then apply rules to the escaped string: the only tags in
     the output are ones this function emitted. */
  let s = esc(text);
  if (!s.trim()) return "";

  const fences = [];
  s = s.replace(/```[^\n]*\n?([\s\S]*?)```/g, (_, code) => {
    fences.push(code);
    return "\u0000F" + (fences.length - 1) + "\u0000";
  });

  const out = [];
  let list = null, para = [], quote = [];
  const flushList = () => {
    if (!list) return;
    out.push(`<${list.tag}>` +
      list.items.map(x => `<li>${inline(x)}</li>`).join("") +
      `</${list.tag}>`);
    list = null;
  };
  const flushPara = () => {
    if (para.length) { out.push(`<p>${inline(para.join(" "))}</p>`); para = []; }
  };
  const flushQuote = () => {
    if (quote.length) {
      out.push(`<blockquote>${inline(quote.join(" "))}</blockquote>`);
      quote = [];
    }
  };
  const flushAll = () => { flushList(); flushPara(); flushQuote(); };

  for (const line of s.split("\n")) {
    const fence = line.match(/^\u0000F(\d+)\u0000$/);
    if (fence) {
      flushAll();
      out.push(`<pre class="code"><code>${fences[+fence[1]]}</code></pre>`);
      continue;
    }
    if (!line.trim()) { flushAll(); continue; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      flushAll();
      /* +2: the page owns h1 and h2, so an agent's h1 becomes an h3. */
      out.push(`<h${Math.min(h[1].length + 2, 6)}>${inline(h[2])}` +
               `</h${Math.min(h[1].length + 2, 6)}>`);
      continue;
    }
    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    if (ul) {
      flushPara(); flushQuote();
      if (!list || list.tag !== "ul") { flushList(); list = {tag: "ul", items: []}; }
      list.items.push(ul[1]);
      continue;
    }
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ol) {
      flushPara(); flushQuote();
      if (!list || list.tag !== "ol") { flushList(); list = {tag: "ol", items: []}; }
      list.items.push(ol[1]);
      continue;
    }
    /* &gt;, not >: the marker was escaped before this rule ran. */
    const bq = line.match(/^&gt;\s?(.*)$/);
    if (bq) { flushPara(); flushList(); quote.push(bq[1]); continue; }

    flushList(); flushQuote();
    para.push(line.trim());
  }
  flushAll();
  return out.join("");
}
```

Add to the `<style>` block, after the existing `pre` rule (`index.html:39-40`):

```css
  .md > :first-child { margin-top:0; }
  .md > :last-child { margin-bottom:0; }
  .md h3, .md h4, .md h5, .md h6 { margin:14px 0 4px; color:var(--fg); }
  .md p { margin:8px 0; }
  .md ul, .md ol { margin:8px 0; padding-left:22px; }
  .md li { margin:2px 0; }
  .md blockquote { margin:8px 0; padding-left:12px;
                   border-left:3px solid var(--line); color:var(--muted); }
  .md code { background:var(--bg); border:1px solid var(--line);
             border-radius:4px; padding:0 4px;
             font:13px/1.5 ui-monospace,monospace; }
  .md pre.code { background:var(--bg); border:1px solid var(--line);
                 border-radius:6px; padding:10px; overflow-x:auto; margin:8px 0; }
  .md pre.code code { background:none; border:0; padding:0; }
  .md a { color:var(--accent); }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_viewer_render.py -q`
Expected: PASS (23 passed)

- [ ] **Step 5: Commit**

```bash
git add debatelab/viewer/index.html tests/test_viewer_render.py
git commit -m "feat: render agent markdown in the viewer

Agents write markdown and the viewer showed the asterisks: every proposal
and critique went into a monospace <pre> as its own source.

Escape the text first, then apply block and inline rules to the escaped
string, so the only tags in the output are ones the renderer emitted.
Fenced and inline code are parked behind NUL-delimited placeholders before
inline rules run, and esc strips NUL so agent text cannot forge one.

Links carry a scheme allowlist. That is the one hole escaping cannot close,
because the renderer emits the href itself."
```

---

### Task 3: Event taxonomy and markdown content cards

**Files:**
- Modify: `debatelab/viewer/index.html` (append `classifyEvent` and move `eventCard` into the render block; delete the old `eventCard` at `:107-115`)
- Test: `tests/test_viewer_render.py` (append)

**Interfaces:**
- Consumes: `esc`, `renderMarkdown`
- Produces:
  - `STRUCTURE_EVENTS`, `TELEMETRY_EVENTS`, `HEADER_EVENTS` — `Set`s of type names.
  - `classifyEvent(type) -> "structure" | "content" | "telemetry"` — unknown types return `"content"`. Used by Tasks 4 and 5.
  - `eventCard(ev, note) -> html` — `note` is the telemetry annotation string from Task 5; pass `""` until then.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_viewer_render.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_viewer_render.py -q`
Expected: FAIL — `ReferenceError: classifyEvent is not defined`

- [ ] **Step 3: Implement the taxonomy**

Append inside the `<script id="render">` block:

```js
/* Structure events become the layout (§1): the phase delimiters group the
   transcript, the header events render as a metadata line. */
const HEADER_EVENTS = new Set(["debate_created", "run_config", "roster_changed"]);
const PHASE_DELIMITERS = new Set(["phase_started", "phase_completed"]);
const TELEMETRY_EVENTS = new Set(["agent_call"]);

function classifyEvent(type) {
  if (HEADER_EVENTS.has(type) || PHASE_DELIMITERS.has(type)) return "structure";
  if (TELEMETRY_EVENTS.has(type)) return "telemetry";
  /* Everything else is content, including types this viewer has never seen.
     A new event type must degrade to a card, never blank the reading view. */
  return "content";
}

function eventCard(ev, note) {
  const verdict = ev.verdict
    ? ` · <span class="vote-${esc(ev.verdict)}">${esc(ev.verdict)}</span>`
    : "";
  const timing = note ? ` <span class="note">${esc(note)}</span>` : "";
  return `
    <details class="card">
      <summary>${esc(ev.agent ?? "system")} · ${esc(ev.type)}${verdict}${timing}</summary>
      <div class="md">${renderMarkdown(ev.content)}</div>
    </details>`;
}
```

Delete the old `eventCard` from the app script (`index.html:107-115`) — the render layer owns it now.

Add to the `<style>` block:

```css
  .note { color:var(--muted); font-size:12px; }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_viewer_render.py -q`
Expected: PASS (30 passed)

- [ ] **Step 5: Commit**

```bash
git add debatelab/viewer/index.html tests/test_viewer_render.py
git commit -m "feat: classify transcript events instead of carding all of them

The viewer's rule was 'every event is a card'. That held for eight
prose-carrying event types; the transcript now has twenty, and a one-round
three-agent debate drew 43 cards of which 25 were bookkeeping.

Classify into structure, content, and telemetry. Unknown types default to
content, which is the half that matters: the reliability and replay cycles
each added event types the viewer had no rule for, and each degraded it
silently. A sixth cycle must degrade to a card, not a blank page."
```

---

### Task 4: Phase grouping

**Files:**
- Modify: `debatelab/viewer/index.html` (append `groupRounds` to the render block)
- Test: `tests/test_viewer_render.py` (append)

**Interfaces:**
- Consumes: `classifyEvent`, `HEADER_EVENTS`, `PHASE_DELIMITERS`
- Produces:
  - `groupRounds(events) -> [{round, phases: [{phase, started, completed, halted, events}]}]` — rounds in first-seen order, phases in first-seen order within a round. Content events only in `events`. Used by Task 6.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_viewer_render.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_viewer_render.py -q`
Expected: FAIL — `ReferenceError: groupRounds is not defined`

- [ ] **Step 3: Implement grouping**

Append inside the `<script id="render">` block:

```js
function groupRounds(events) {
  /* Rounds and phases in first-seen order. phase_started is appended before
     its fanout runs, so first-seen order is the order the phases ran — and
     legacy transcripts that have no phase_started fall into the same path,
     because their first content event opens the group instead. */
  const rounds = [];
  const byRound = new Map();
  for (const e of events || []) {
    /* Header events carry round 0, and a "Round 0" heading is not a round. */
    if (HEADER_EVENTS.has(e.type)) continue;
    if (e.round === undefined || e.round === null) continue;

    if (!byRound.has(e.round)) {
      const r = {round: e.round, phases: [], byPhase: new Map()};
      byRound.set(e.round, r);
      rounds.push(r);
    }
    const r = byRound.get(e.round);
    const name = e.phase ?? "";
    if (!r.byPhase.has(name)) {
      const p = {phase: name, started: false, completed: false,
                 halted: false, events: []};
      r.byPhase.set(name, p);
      r.phases.push(p);
    }
    const p = r.byPhase.get(name);
    if (e.type === "phase_started") { p.started = true; }
    else if (e.type === "phase_completed") { p.completed = true; }
    else if (classifyEvent(e.type) === "content") { p.events.push(e); }
    p.halted = p.started && !p.completed;
  }
  return rounds;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_viewer_render.py -q`
Expected: PASS (36 passed)

- [ ] **Step 5: Commit**

```bash
git add debatelab/viewer/index.html tests/test_viewer_render.py
git commit -m "feat: group the transcript by phase within each round

Rounds were flat lists, so propose, critique, revise and vote interleaved
into one wall. Promote the phase delimiters from cards to structure: they
group the events they bracket, and a phase that started without completing
renders as halted — which is what the replay cycle recorded that boundary
for.

Header events carry round 0 and were drawing a 'Round 0' heading. They are
not a round."
```

---

### Task 5: Telemetry annotation

**Files:**
- Modify: `debatelab/viewer/index.html` (append `pairTelemetry` and `telemetryNote` to the render block)
- Test: `tests/test_viewer_render.py` (append)

**Interfaces:**
- Consumes: `classifyEvent`
- Produces:
  - `pairTelemetry(events) -> {calls: Map<eventIndex, attempts[]>, orphans: attempts[][]}` — `calls` keys are indices into the `events` array passed in. Used by Task 6.
  - `telemetryNote(attempts) -> string` — `""`, `"1.4s"`, or `"3 attempts · 4.2s · rate_limit"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_viewer_render.py`:

```python
@needs_node
def test_a_clean_call_annotates_its_content_card_with_a_duration():
    events = js_events([
        {"round": 1, "phase": "propose", "agent": "a", "type": "agent_call",
         "attempt": 1, "duration_ms": 1400, "ok": True, "content": ""},
        {"round": 1, "phase": "propose", "agent": "a", "type": "proposal",
         "content": "p"},
    ])
    out = render_js(
        f"telemetryNote(pairTelemetry({events}).calls.get(1))"
    )
    assert out == "1.4s"


@needs_node
def test_retries_annotate_with_attempt_count_and_error_kind():
    """Why did this agent abstain — the question the reliability cycle
    recorded kind for, finally readable in the viewer."""
    events = js_events([
        {"round": 1, "phase": "propose", "agent": "a", "type": "agent_call",
         "attempt": 1, "duration_ms": 200, "ok": False, "kind": "rate_limit",
         "content": "429"},
        {"round": 1, "phase": "propose", "agent": "a", "type": "agent_call",
         "attempt": 2, "duration_ms": 4000, "ok": True, "content": ""},
        {"round": 1, "phase": "propose", "agent": "a", "type": "proposal",
         "content": "p"},
    ])
    out = render_js(f"telemetryNote(pairTelemetry({events}).calls.get(2))")
    assert out == "2 attempts · 4.2s · rate_limit"


@needs_node
def test_calls_pair_across_the_vote_phases_two_fanouts():
    """Pairing cannot key on (round, phase, agent): the vote phase runs a
    nominate fanout and a vote fanout under one phase name, so that key holds
    two calls per agent. The rule is ordering-based instead."""
    events = js_events([
        {"round": 1, "phase": "vote", "agent": "a", "type": "agent_call",
         "attempt": 1, "duration_ms": 1000, "ok": True, "content": ""},
        {"round": 1, "phase": "vote", "agent": "a", "type": "nomination",
         "content": "NOMINATE: b"},
        {"round": 1, "phase": "vote", "agent": "a", "type": "agent_call",
         "attempt": 1, "duration_ms": 2000, "ok": True, "content": ""},
        {"round": 1, "phase": "vote", "agent": "a", "type": "vote",
         "verdict": "accept", "content": "VOTE: accept"},
    ])
    out = render_js(
        f"JSON.stringify([...pairTelemetry({events}).calls].map("
        f"([i, a]) => [i, a.length, a[0].duration_ms]))"
    )
    assert json.loads(out) == [[1, 1, 1000], [3, 1, 2000]]


@needs_node
def test_interleaved_agents_pair_to_their_own_cards():
    """_fanout runs agents concurrently, so calls from different agents
    interleave. A given agent's own events stay ordered — that is all the
    rule needs."""
    events = js_events([
        {"round": 1, "phase": "propose", "agent": "a", "type": "agent_call",
         "attempt": 1, "duration_ms": 1000, "ok": True, "content": ""},
        {"round": 1, "phase": "propose", "agent": "b", "type": "agent_call",
         "attempt": 1, "duration_ms": 2000, "ok": True, "content": ""},
        {"round": 1, "phase": "propose", "agent": "b", "type": "proposal",
         "content": "pb"},
        {"round": 1, "phase": "propose", "agent": "a", "type": "proposal",
         "content": "pa"},
    ])
    out = render_js(
        f"JSON.stringify([...pairTelemetry({events}).calls].map("
        f"([i, a]) => [i, a[0].duration_ms]))"
    )
    assert sorted(json.loads(out)) == [[2, 2000], [3, 1000]]


@needs_node
def test_calls_with_no_content_event_are_orphans_not_dropped():
    """When a phase halts, _fanout raises before the phase function emits any
    content, so the agents that succeeded have calls with nothing to attach
    to. Dropping them erases the evidence of the only phase that matters."""
    events = js_events([
        {"round": 1, "phase": "propose", "agent": "a", "type": "agent_call",
         "attempt": 1, "duration_ms": 1000, "ok": True, "content": ""},
        {"round": 1, "phase": "propose", "agent": "b", "type": "agent_call",
         "attempt": 1, "duration_ms": 900, "ok": False, "kind": "timeout",
         "content": "timed out"},
    ])
    out = render_js(
        f"JSON.stringify(pairTelemetry({events}).orphans.map("
        f"a => [a[0].agent, a.length]))"
    )
    assert sorted(json.loads(out)) == [["a", 1], ["b", 1]]


@needs_node
def test_telemetry_note_of_nothing_is_empty():
    assert render_js("telemetryNote([])") == ""
    assert render_js("telemetryNote(undefined)") == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_viewer_render.py -q`
Expected: FAIL — `ReferenceError: pairTelemetry is not defined`

- [ ] **Step 3: Implement pairing**

Append inside the `<script id="render">` block:

```js
function pairTelemetry(events) {
  /* Accumulate agent_call events per (round, phase, agent); flush into the
     next content event with that key. Ordering, not the key alone: the vote
     phase runs two fanouts under one phase name. Safe under _fanout's
     concurrency because one agent's events are appended by one thread. */
  const key = e => `${e.round}\u0000${e.phase}\u0000${e.agent}`;
  const pending = new Map();
  const calls = new Map();
  (events || []).forEach((e, i) => {
    const cls = classifyEvent(e.type);
    if (cls === "telemetry") {
      const k = key(e);
      if (!pending.has(k)) pending.set(k, []);
      pending.get(k).push(e);
    } else if (cls === "content" && e.agent) {
      const k = key(e);
      if (pending.has(k)) { calls.set(i, pending.get(k)); pending.delete(k); }
    }
  });
  /* Unflushed calls: a phase that halted before emitting content. */
  return {calls, orphans: [...pending.values()]};
}

function telemetryNote(attempts) {
  if (!attempts || !attempts.length) return "";
  const ms = attempts.reduce((sum, a) => sum + (a.duration_ms || 0), 0);
  const secs = (ms / 1000).toFixed(1) + "s";
  if (attempts.length === 1) return secs;
  const kinds = attempts.filter(a => a.kind).map(a => a.kind);
  const kind = kinds.length ? " · " + kinds[kinds.length - 1] : "";
  return `${attempts.length} attempts · ${secs}${kind}`;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_viewer_render.py -q`
Expected: PASS (42 passed)

- [ ] **Step 5: Commit**

```bash
git add debatelab/viewer/index.html tests/test_viewer_render.py
git commit -m "feat: annotate debate cards with their agent_call telemetry

agent_call was 15 of the 43 cards a one-round debate drew, each an empty
<pre>. Attach the calls to the card they produced instead: a clean call
reads 1.4s, a retried one reads its attempt count and error kind, which is
what the reliability cycle recorded kind for.

Pairing is ordering-based rather than keyed on (round, phase, agent),
because the vote phase runs two fanouts under one phase name. Calls left
unflushed by a halted phase are kept as orphans, not dropped."
```

---

### Task 6: Hero panel and `showDebate` rewiring

**Files:**
- Modify: `debatelab/viewer/index.html` (append `renderHero`/`tallyText` to the render block; rewrite `showDebate` in the app script, old `:127-174`), `<style>`
- Test: `tests/test_viewer_render.py` (append), `tests/test_serve.py` (append)

**Interfaces:**
- Consumes: `renderMarkdown`, `esc`, `groupRounds`, `pairTelemetry`, `telemetryNote`, `eventCard`
- Produces:
  - `tallyText(tally) -> string` — `"2 accept / 0 reject / 1 abstain"`.
  - `renderHero(result) -> html` — `""` when `result` is null (legacy debate).
  - `renderTranscript(events) -> html` — the grouped, annotated rounds.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_viewer_render.py`:

```python
APPROVED = {
    "status": "approved", "answer": "# Use Redis\n\nWith a **TTL**.",
    "candidate": {"agent": "claude", "round": 2},
    "tally": {"accepts": 3, "rejects": 0, "abstains": 0,
              "roster_size": 3, "required": 2},
    "decided_at": "2026-07-15T10:00:00+00:00", "note": "ship it",
    "reason": None, "round": 2, "failed_phase": None,
}
AWAITING = {
    "status": "awaiting_human", "answer": None,
    "candidate": {"agent": "claude", "round": 1},
    "tally": {"accepts": 2, "rejects": 0, "abstains": 1,
              "roster_size": 3, "required": 2},
    "decided_at": None, "note": None,
    "reason": "candidate is awaiting human review",
    "round": 1, "failed_phase": None,
}
HALTED = {
    "status": "error", "answer": None, "candidate": None, "tally": None,
    "decided_at": None, "note": None,
    "reason": "only 1 agent(s) responded in phase 'critique' — need at least 2",
    "round": 1, "failed_phase": "critique",
}


@needs_node
def test_hero_renders_the_answer_as_markdown_when_approved():
    out = render_js(f"renderHero({json.dumps(APPROVED)})")
    assert "<h3>Use Redis</h3>" in out
    assert "<strong>TTL</strong>" in out
    assert "claude" in out
    assert "3 accept / 0 reject / 0 abstain" in out


@needs_node
def test_hero_never_renders_unapproved_prose():
    """result.json keeps candidate.text out on purpose, and the result spec
    rejected showing the candidate under a status banner. A hero panel is
    that argument's strongest case: it is the largest thing on the page and
    the one that survives a screenshot without its banner."""
    out = render_js(f"renderHero({json.dumps(AWAITING)})")
    assert "No answer" in out
    assert "2 accept / 0 reject / 1 abstain" in out
    assert "awaiting human review" in out


@needs_node
def test_hero_shows_the_failing_phase_on_a_halt():
    out = render_js(f"renderHero({json.dumps(HALTED)})")
    assert "critique" in out
    assert "round 1" in out


@needs_node
def test_hero_is_empty_for_a_legacy_debate_without_a_result():
    """The four committed debates predate result.json. They lose the hero and
    keep everything else."""
    assert render_js("renderHero(null)") == ""


@needs_node
def test_hero_escapes_a_hostile_reason():
    hostile = dict(HALTED, reason="<script>alert(1)</script>")
    out = render_js(f"renderHero({json.dumps(hostile)})")
    assert "<script>" not in out


@needs_node
def test_transcript_renders_grouped_annotated_cards():
    events = js_events([
        {"round": 0, "phase": "run", "type": "run_config", "content": "..."},
        {"round": 1, "phase": "propose", "type": "phase_started"},
        {"round": 1, "phase": "propose", "agent": "a", "type": "agent_call",
         "attempt": 1, "duration_ms": 1400, "ok": True, "content": ""},
        {"round": 1, "phase": "propose", "agent": "a", "type": "proposal",
         "content": "**bold** plan"},
        {"round": 1, "phase": "propose", "type": "phase_completed"},
    ])
    out = render_js(f"renderTranscript({events})")
    assert "Round 1" in out
    assert "propose" in out
    assert "<strong>bold</strong>" in out
    assert "1.4s" in out
    assert "run_config" not in out
    assert out.count("<details") == 1


@needs_node
def test_transcript_marks_a_halted_phase():
    events = js_events([
        {"round": 1, "phase": "critique", "type": "phase_started"},
        {"round": 1, "phase": "critique", "agent": "a", "type": "abstained",
         "content": "boom"},
    ])
    out = render_js(f"renderTranscript({events})")
    assert "halted" in out
```

Append to `tests/test_serve.py`:

```python
def test_debate_result_is_served_when_present(running_server, tmp_path):
    """The hero panel's source. result.json lives beside state.json under the
    debates root, so the existing static route already reaches it."""
    _, body = get(running_server + "/index.json")
    debate_id = json.loads(body)[0]["id"]
    (tmp_path / "debates" / debate_id / "result.json").write_text(
        json.dumps({"status": "approved", "answer": "Use Redis."})
    )
    status, body = get(f"{running_server}/{debate_id}/result.json")
    assert status == 200
    assert json.loads(body)["answer"] == "Use Redis."


def test_missing_result_404s_cleanly(running_server):
    """The four committed debates predate result.json; the viewer treats a
    404 as 'legacy debate, no hero' rather than as a failure to load."""
    _, body = get(running_server + "/index.json")
    debate_id = json.loads(body)[0]["id"]
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(f"{running_server}/{debate_id}/result.json")
    assert exc.value.code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_viewer_render.py tests/test_serve.py -q`
Expected: FAIL — `ReferenceError: renderHero is not defined`, and `test_debate_result_is_served_when_present` fails with HTTP 404.

- [ ] **Step 3: Implement the hero and the transcript renderer**

Append inside the `<script id="render">` block:

```js
function tallyText(t) {
  return `${esc(String(t.accepts))} accept / ${esc(String(t.rejects))} reject` +
         ` / ${esc(String(t.abstains))} abstain`;
}

function renderHero(result) {
  /* Prose only when a human approved it. result.json keeps candidate.text
     out for this reason; the unapproved candidate keeps its own honest
     heading further down the page. */
  if (!result) return "";
  if (result.answer !== null && result.answer !== undefined) {
    const c = result.candidate || {};
    let prov = `Approved ${esc(String(result.decided_at ?? ""))}` +
      ` · from <strong>${esc(c.agent)}</strong>, round ${esc(String(c.round))}`;
    if (result.tally) prov += ` · ${tallyText(result.tally)}`;
    return `<div class="card hero"><h2>Answer</h2>` +
      `<div class="md">${renderMarkdown(result.answer)}</div>` +
      `<p class="note">${prov}</p></div>`;
  }
  const bits = [];
  if (result.tally) {
    bits.push(`${tallyText(result.tally)} of ` +
      `${esc(String(result.tally.roster_size))} ` +
      `(${esc(String(result.tally.required))} required)`);
  }
  if (result.round !== null && result.round !== undefined) {
    bits.push(`round ${esc(String(result.round))}`);
  }
  if (result.failed_phase) {
    bits.push(`halted in <strong>${esc(result.failed_phase)}</strong>`);
  }
  return `<div class="card hero"><h2>No answer</h2>` +
    `<p>${esc(result.reason ?? "")}</p>` +
    (bits.length ? `<p class="note">${bits.join(" · ")}</p>` : "") +
    `</div>`;
}

function renderTranscript(events) {
  const {calls, orphans} = pairTelemetry(events);
  const index = new Map();
  (events || []).forEach((e, i) => index.set(e, i));
  let html = "";
  for (const r of groupRounds(events)) {
    html += `<h2>Round ${esc(String(r.round))}</h2>`;
    for (const p of r.phases) {
      const flag = p.halted ? ` <span class="note">halted</span>` : "";
      html += `<h3>${esc(p.phase)}${flag}</h3>`;
      for (const e of p.events) {
        html += eventCard(e, telemetryNote(calls.get(index.get(e))));
      }
      const stray = orphans.filter(a =>
        a[0].round === r.round && a[0].phase === p.phase);
      for (const attempts of stray) {
        html += `<div class="card note">${esc(attempts[0].agent)} · ` +
          `unfinished call · ${esc(telemetryNote(attempts))}</div>`;
      }
    }
  }
  return html;
}
```

Rewrite `showDebate` in the app script (old `index.html:127-174`), keeping the generation and polling logic exactly as it is:

```js
async function fetchJSONOptional(url) {
  try { return await fetchJSON(url); } catch { return null; }
}

async function showDebate(id, generation) {
  back.hidden = false;
  let state, events, result;
  try {
    state = await fetchJSON(`/${id}/state.json`);
    events = await fetchJSONL(`/${id}/transcript.jsonl`);
    result = await fetchJSONOptional(`/${id}/result.json`);
  } catch (e) {
    if (!isCurrent(id, generation)) return;
    app.innerHTML = `<p class="muted">failed to load ${esc(id)}: ${esc(String(e))}</p>`;
    schedulePoll(id, generation);
    return;
  }
  if (!isCurrent(id, generation)) return;
  let html = `
    <div class="card banner row">
      <div><strong>${esc(state.title)}</strong><br>
        <span class="muted">${esc(id)} · round ${esc(String(state.round))}/${esc(String(state.max_rounds))}${
          state.roster ? " · roster " + esc(state.roster.join(", ")) : ""}${
          state.quorum ? " · quorum " + esc(state.quorum) : ""}</span>
      </div>
      ${badge(state.status)}
    </div>`;
  html += renderHero(result);
  if (state.human_decision) {
    html += `<div class="card banner"><h3>Human decision: ${esc(state.human_decision.decision).toUpperCase()}</h3>
      <div class="md">${renderMarkdown(state.human_decision.note || "(no note)")}</div></div>`;
  }
  if (state.candidate) {
    html += `<h2>Candidate answer (from ${esc(state.candidate.agent)})</h2>
      <div class="card md">${renderMarkdown(state.candidate.text)}</div>`;
  }
  const votes = Object.entries(state.votes || {});
  if (votes.length || (state.abstained || []).length) {
    html += "<h2>Latest votes</h2><div class='card'>" +
      votes.map(([a, v]) =>
        `<div>${esc(a)}: <span class="vote-${esc(v.vote)}">${esc(v.vote)}</span></div>`
      ).join("") +
      (state.abstained || []).map(a =>
        `<div>${esc(a)}: <span class="vote-abstained">abstained</span></div>`
      ).join("") + "</div>";
  }
  /* Collapsed only once there is an answer to read instead. A reviewer at
     awaiting_human is deciding because of the process, and a halt is only
     legible from it. */
  const approved = result && result.status === "approved";
  html += `<details class="transcript" ${approved ? "" : "open"}>` +
    `<summary>Debate transcript</summary>${renderTranscript(events)}</details>`;
  app.innerHTML = html;
  if (state.status === "running") {
    schedulePoll(id, generation);
  }
}
```

Add to the `<style>` block:

```css
  .hero { border-left:4px solid var(--ok); }
  .hero h2 { margin:0 0 8px; font-size:16px; }
  details.transcript > summary { font-size:14px; margin:20px 0 4px; }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_viewer_render.py tests/test_serve.py -q`
Expected: PASS (49 render + 5 serve)

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 325 passed, in under 10s

- [ ] **Step 6: Verify against a real debate end to end**

The tests use fixtures; this drives the actual viewer over a real transcript. Run:

```bash
.venv/bin/python -m debatelab.cli serve --port 8199 &
sleep 1
curl -s "http://127.0.0.1:8199/" | grep -c 'script id="render"'
curl -s "http://127.0.0.1:8199/index.json" | head -c 200
kill %1
```

Expected: `1`, then the index JSON. Then open `http://127.0.0.1:8199/` and confirm on `20260714-how-can-this-repository-be-improved-furt`: no "Round 0" heading, phases grouped under each round, markdown rendered rather than escaped, and no hero panel (it predates `result.json`).

- [ ] **Step 7: Commit**

```bash
git add debatelab/viewer/index.html tests/test_viewer_render.py tests/test_serve.py
git commit -m "feat: lead the viewer with the answer, not the audit trail

result.json and final.md shipped last cycle so a reader could get the
answer without scrolling the process, and nothing read them. The hero
panel does, and it renders prose only when a human approved it: otherwise
it shows the outcome facts result.json records — tally, round, failing
phase — and the candidate keeps its own honest heading below.

The transcript collapses beneath the hero only once there is an answer to
read instead. A reviewer at awaiting_human is deciding because of the
process, and a halted debate is only legible from it.

Debates without a result.json get no hero and lose nothing else."
```

---

## Self-Review

**Spec coverage:**

| Spec section | Task |
|---|---|
| §1 Event taxonomy, unknown-type default | 3 |
| §2 `renderMarkdown`, escape-first, scheme allowlist | 2 (attribute-safe `esc` in 1) |
| §3 Hero panel, `result.json` fetch, legacy 404, collapse rule | 6 |
| §4 Phase grouping, `phase_started` ordering, legacy fallback, halted phases | 4 |
| §5 Telemetry annotation, ordering-based pairing, orphans | 5 |
| Testing: node harness, `skipif`, sanitization, taxonomy, hero | 1–6 |
| Testing: `result.json` served, grep tests replaced where they overlap | 1 (delete), 6 (serve) |

**Two additions the spec did not anticipate, both discovered while planning:**

1. **`esc` was not attribute-safe** (Task 1). The old implementation set `textContent` and read back `innerHTML`; HTML text-node serialization escapes `&`, `<`, `>` and never quotes. Harmless in every context the viewer had, and a hole the moment `renderMarkdown` emits `href="..."` — escape-first cannot close it, because the renderer emits the quote itself. Task 1 lands before any `href` exists.
2. **Header events carry `round: 0`** (Task 4), so the viewer draws a "Round 0" heading. Skipping their cards is not enough; grouping has to exclude them.

**Type consistency:** `classifyEvent` returns the three strings used by `groupRounds` (Task 4) and `pairTelemetry` (Task 5). `pairTelemetry().calls` is keyed by array index, and Task 6's `renderTranscript` rebuilds that index via an event→index `Map` before calling `telemetryNote`. `eventCard(ev, note)` takes its `note` parameter from Task 3 onward and is passed `""` by nothing — Task 3's tests pass it explicitly, Task 6 passes `telemetryNote(...)`, which returns `""` for no attempts.
