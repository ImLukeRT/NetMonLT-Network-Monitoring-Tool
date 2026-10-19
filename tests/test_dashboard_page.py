"""The dashboard's live "N s ago" labels.

The page is plain HTML with inline JavaScript, so there is no browser test here. These tests pin
the properties that matter: one timer repaints the labels every second from data already
received (no extra requests), the data-refresh intervals are unchanged, and the elapsed-time
arithmetic is right (run with Node.js when it is installed).
"""

import json
import re
import shutil
import subprocess

import pytest

from helpers import REPO_ROOT

PAGE = (REPO_ROOT / "src" / "netmonlt" / "templates" / "dashboard.html").read_text(encoding="utf-8")
HELPERS_START = "// ---- live "  # the block of helpers that make labels tick
HELPERS_END = "function render(data)"


def intervals():
    return re.findall(r"^\s*setInterval\((\w+),\s*(\d+)\)", PAGE, flags=re.M)


def function_body(name):
    start = PAGE.index(f"function {name}(")
    return PAGE[start:PAGE.index("\n}\n", start)]


def test_labels_tick_every_second_from_a_single_timer():
    assert [name for name, ms in intervals() if ms == "1000"] == ["tickAges"]
    assert PAGE.count("setInterval(tickAges") == 1  # created once at load, never inside a callback


def test_the_data_refresh_intervals_are_unchanged():
    assert [(n, int(ms)) for n, ms in intervals() if n != "tickAges"] == [
        ("refreshChart", 5000), ("refreshInterfaces", 5000), ("refresh", 3000),
    ]


def test_ticking_makes_no_requests_and_only_repaints_existing_labels():
    for name in ("tickAges", "paintAge"):
        body = function_body(name)
        assert "fetch" not in body and "getJson" not in body, name
    assert 'querySelectorAll("[data-age]")' in function_body("tickAges")
    # it must not rebuild tables or create elements, which would leak nodes every second
    assert "createElement" not in function_body("tickAges")
    assert "performance.now()" in function_body("paintAge")  # monotonic: ignores clock changes


def test_every_age_shown_is_a_live_label():
    # the old static rendering of an API age must be gone from all three tables
    assert "Math.round(d.age_s)" not in PAGE
    assert "Math.round(i.age_s)" not in PAGE
    assert "Math.round(snmp.last_ok_age_s)" not in PAGE
    assert "ago(a.age_s)" not in PAGE
    for call in ("setAge(ageCell, d.age_s", "setAge(timeCell, a.age_s", "setAge(updated, i.age_s",
                 "setAge(why, snmp.last_ok_age_s"):
        assert call in PAGE, call


# ------------------------------------------------------------------ the arithmetic, in Node.js
NODE = shutil.which("node")

HARNESS = r"""
const page = %(page)s;
const start = page.indexOf(%(start)s), end = page.indexOf(%(end)s);
const agoStart = page.indexOf("function ago(");
const agoSource = page.slice(agoStart, page.indexOf("\n}\n", agoStart) + 2);
let now = 0;
const performance = {now: () => now};
const labels = [];
const document = {querySelectorAll: () => labels};
const label = () => { const el = {dataset: {}, textContent: ""}; labels.push(el); return el; };
eval(agoSource + page.slice(start, end) + "; globalThis.api = {setAge, tickAges};");
%(body)s
"""


def run_js(body):
    script = HARNESS % {
        "page": json.dumps(PAGE), "start": json.dumps(HELPERS_START),
        "end": json.dumps(HELPERS_END), "body": body,
    }
    done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_elapsed_time_is_added_to_the_age_the_api_reported():
    out = run_js("""
      const el = label(), seen = [];
      api.setAge(el, 5, "seconds");
      seen.push(el.textContent);
      for (let i = 0; i < 3; i++) { now += 1000; api.tickAges(); seen.push(el.textContent); }
      console.log(JSON.stringify(seen));
    """)
    assert out == ["5s ago", "6s ago", "7s ago", "8s ago"]


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_a_new_response_replaces_the_age_instead_of_adding_to_it():
    out = run_js("""
      const el = label(), seen = [];
      api.setAge(el, 4.2, "seconds"); now += 2000; api.tickAges(); seen.push(el.textContent);
      api.setAge(el, 1.0, "seconds"); seen.push(el.textContent);   // a fresh check arrived
      now += 1000; api.tickAges(); seen.push(el.textContent);
      console.log(JSON.stringify(seen));
    """)
    assert out == ["6s ago", "1s ago", "2s ago"]


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_prefix_and_the_coarser_alert_format_are_kept():
    out = run_js("""
      const a = label(), b = label(), seen = [];
      api.setAge(a, 58, "ago", "10:01:02 · ");
      api.setAge(b, 3, "seconds", "last good data ");
      now += 2000; api.tickAges(); seen.push(a.textContent, b.textContent);
      now += 60000; api.tickAges(); seen.push(a.textContent);   // seconds become minutes
      console.log(JSON.stringify(seen));
    """)
    assert out == ["10:01:02 · 1m ago", "last good data 5s ago", "10:01:02 · 2m ago"]


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_the_age_never_runs_backwards():
    out = run_js("""
      now = 5000; const el = label();
      api.setAge(el, 10, "seconds");
      now = 1000; api.tickAges();    // the monotonic clock cannot do this, but be safe
      console.log(JSON.stringify([el.textContent]));
    """)
    assert out == ["10s ago"]
