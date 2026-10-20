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
        ("refreshChart", 5000), ("refreshIncidents", 5000), ("refreshQuality", 5000),
        ("refreshWifi", 5000), ("refreshQos", 5000), ("refreshInterfaces", 5000),
        ("refresh", 3000),
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


# ------------------------------------------------------------------ the network-quality section
def test_the_quality_section_states_what_the_figures_are_and_are_not():
    text = " ".join(PAGE.split())  # the caption wraps across source lines
    for needle in ('id="q-panel"', "Network quality", 'id="q-jitter"', 'id="q-loss"',
                   "not RFC 3550 RTP jitter", "not one-way delay", "never counted as 0 ms",
                   "application-level heuristics, not a QoS standard",
                   "does not verify or enforce QoS"):
        assert needle in text, needle


def test_the_quality_section_follows_the_existing_device_and_window_controls():
    # one set of controls: the quality requests use the same window and device as the latency chart
    assert '"/api/quality?" + q' in PAGE and "/quality?" in PAGE
    assert 'const q = "window_s=" + chartWindow' in PAGE
    assert "resetQuality();" in function_body("resetChart")


def test_quality_is_drawn_with_text_nodes_only():
    assert "innerHTML" not in PAGE  # values from the API are never parsed as markup


def test_missing_quality_figures_are_shown_as_dashes_not_zero():
    assert 'v === null || v === undefined ? "–"' in PAGE
    assert "const fmtOptMs" in PAGE and "const fmtPct" in PAGE


# ------------------------------------------------------------------ the Wi-Fi panel
def test_the_wifi_panel_is_hidden_until_the_api_says_it_is_enabled():
    assert 'id="w-panel" hidden' in PAGE
    assert "if (!data.enabled) { wPanel.hidden = true; return; }" in PAGE
    assert PAGE.index('"/api/wifi/history?window_s="') > PAGE.index("if (!data.enabled)")


def test_the_wifi_panel_states_what_it_does_and_does_not_do():
    text = " ".join(PAGE.split())
    for needle in ("Wi-Fi signal (this computer)", "Local only", "never scans for nearby networks",
                   "never records other networks' names", "never connects to anything",
                   "never calculated from the percentage", "not measured throughput",
                   "A dash means unavailable, not zero", "SIMULATED WI-FI DATA"):
        assert needle in text, needle


def test_wifi_never_derives_rssi_from_the_signal_percentage():
    block = PAGE[PAGE.index("function renderWifi"):PAGE.index("function wSummaryText")]
    assert "signal_pct" in block and "rssi_dbm" in block
    # the RSSI row reads only the API's rssi_dbm; there is no arithmetic that makes dBm from %
    assert "signal_pct / 2" not in block and "- 100" not in block and "-100" not in block
    assert "never converts the signal quality % into dBm" in block


def test_the_wifi_chart_never_draws_a_disconnection_as_a_zero_signal():
    block = PAGE[PAGE.index("function drawWifiChart"):PAGE.index("async function refreshWifi")]
    assert "p.signal_mean === null" in block  # no signal: breaks the line
    assert '"data-kind": "notconnected"' in block and "offzone" in block


def test_the_wifi_last_observation_label_is_a_live_one_second_label():
    block = PAGE[PAGE.index("function renderWifi"):PAGE.index("function wSummaryText")]
    assert 'setAge(when, cur.age_s, "seconds")' in block


def test_the_wifi_window_buttons_do_not_drive_the_latency_chart():
    assert 'class="wwin"' in PAGE and "data-wwindow" in PAGE
    assert 'querySelectorAll(".windows button")' in PAGE  # the latency chart's own group
    assert 'querySelectorAll(".wwin button")' in PAGE


# ------------------------------------------------------------------ the QoS section
def test_the_qos_section_is_hidden_until_the_api_says_it_is_enabled():
    assert 'id="qos-panel" hidden' in PAGE
    assert "qosPanel.hidden = !data.enabled" in PAGE
    start = PAGE.index("async function refreshQos")
    block = PAGE[start:PAGE.index("qosDeviceSelect.addEventListener")]
    assert block.index("if (!data.enabled || !qosDevice) return;") < block.index("/api/qos/history")


def test_the_qos_section_explains_what_qos_telemetry_is_and_is_not():
    text = " ".join(PAGE.split())
    for needle in ("QoS telemetry (SNMP)", "what a <b>device itself counts</b>",
                   "documented adapter", "<b>not</b> the Network quality grades",
                   "<b>not</b> the interface discards",
                   "<b>not</b> DSCP markings observed in traffic",
                   "never inspects packets", "does not prove that QoS is enforced or effective",
                   "SIMULATED QoS DATA"):
        assert needle in text, needle


def test_the_qos_panel_shows_source_adapter_counter_type_and_polling_time():
    block = PAGE[PAGE.index("function renderQos"):PAGE.index("function qosAxisLabel")]
    for needle in ("entry.adapter_label", "entry.source_mib", "r.counter_type",
                   'setAge(updated, r.age_s, "seconds"', "Requires: "):
        assert needle in block, needle


def test_unknown_qos_values_are_dashes_with_a_reason_never_zero():
    assert 'value === null || value === undefined ? "–"' in PAGE
    assert "QOS_NOTES[row.rate_note]" in PAGE
    for note in ("first_sample", "counter_reset", "discontinuity", "gap", "no_counters", "absent"):
        assert f"  {note}:" in PAGE[PAGE.index("const QOS_NOTES"):PAGE.index("const QOS_METRICS")]


def test_the_qos_chart_keeps_missing_data_honest():
    block = PAGE[PAGE.index("function drawQosChart"):PAGE.index("function qosSummaryText")]
    assert "p[qosMetric] === null" in block  # a null rate breaks the line, it is not drawn as 0
    assert 'class: "norate"' in block and 'class: "gapzone"' in block
    assert "not the same as zero traffic or zero drops" in block


def test_the_qos_controls_do_not_drive_the_latency_chart():
    assert 'class="qwin"' in PAGE and "data-qwindow" in PAGE and "data-qmetric" in PAGE
    assert 'querySelectorAll(".windows button")' in PAGE  # the latency chart's own group
    assert 'querySelectorAll(".qwin button")' in PAGE


def test_qos_values_from_the_api_are_never_parsed_as_markup():
    assert "innerHTML" not in PAGE


# ------------------------------------------------------------------ the arithmetic, in Node.js
NODE = shutil.which("node")

# The program is sent to Node on standard input (`node -`), never as a command-line argument:
# Windows rejects a command line longer than about 32,000 characters (WinError 206).
HARNESS = r"""
(function () {
  let now = 0;
  const performance = {now: () => now};
  const labels = [];
  const document = {querySelectorAll: () => labels};
  const label = () => { const el = {dataset: {}, textContent: ""}; labels.push(el); return el; };
%(helpers)s
  const api = {setAge, tickAges};
%(body)s
})();
"""


def helper_source():
    """Just the page's own label helpers (and the `ago` function they use), not the whole page."""
    ago_start = PAGE.index("function ago(")
    ago_source = PAGE[ago_start:PAGE.index("\n}\n", ago_start) + 2]
    block = PAGE[PAGE.index(HELPERS_START):PAGE.index(HELPERS_END)]
    return ago_source + block


def run_js(body):
    script = HARNESS % {"helpers": helper_source(), "body": body}
    done = subprocess.run([NODE, "-"], input=script.encode("utf-8"), capture_output=True,
                          timeout=30)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return json.loads(done.stdout.decode("utf-8"))


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


@pytest.mark.skipif(NODE is None, reason="Node.js is not installed")
def test_a_program_far_longer_than_a_windows_command_line_still_runs():
    # Regression test: the program travels on stdin, so its length is not limited like argv is.
    padding = "/* " + "x" * 300_000 + " */"
    assert run_js(padding + 'console.log(JSON.stringify(["ok"]));') == ["ok"]


# ------------------------------------------------------------------ view tabs (Stage 10.1 fix)
# Regression: the Recent alerts tabs reused the incident tabs' CSS class, and the incident tab
# handler (`.inc-tabs button`) also bound to them. Clicking an alert tab then set the incident view
# to `undefined`, so every incident refresh requested /api/incidents?view=undefined (HTTP 422).
from html.parser import HTMLParser  # noqa: E402


class _Buttons(HTMLParser):
    """Every <button> with the ids and classes of the elements it sits inside."""

    def __init__(self):
        super().__init__()
        self.stack, self.buttons, self._in_script = [], [], False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self._in_script = True
        if tag in ("br", "input", "meta", "link", "img", "hr", "col"):
            return
        self.stack.append((tag, a.get("id"), (a.get("class") or "").split()))
        if tag == "button":
            self.buttons.append((a, [f for f in self.stack[:-1]]))

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def _buttons():
    parser = _Buttons()
    parser.feed(PAGE[:PAGE.index("<script>")])
    return parser.buttons


def _matched_by(selector):
    """The buttons a `<ancestor> button` selector would match (an #id or .class ancestor)."""
    anc = selector.removesuffix(" button")
    kind, name = anc[0], anc[1:]
    assert kind in "#.", selector
    return [a for a, ancestors in _buttons()
            if any((f[1] == name) if kind == "#" else (name in f[2]) for f in ancestors)]


def _tab_selectors():
    return re.findall(r'querySelectorAll\("([#.][\w-]+ button)"\)', PAGE)


def test_incident_tab_buttons_only_carry_valid_incident_views():
    inc_tabs = _matched_by("#inc-tabs button")
    assert [b["data-incview"] for b in inc_tabs] == ["active", "history"]
    assert inc_tabs[0]["aria-selected"] == "true"  # the initial view is Active
    assert re.search(r'let incView = "active"', PAGE)


def test_alert_tab_buttons_only_carry_valid_alert_views():
    alert_tabs = _matched_by("#alert-tabs button")
    assert [b["data-alertview"] for b in alert_tabs] == ["unresolved", "archived", "all"]
    assert alert_tabs[0]["aria-selected"] == "true"
    assert 'let alertView = "unresolved"' in PAGE


def test_no_tab_handler_can_bind_to_the_other_tab_group():
    selectors = _tab_selectors()
    assert "#inc-tabs button" in selectors and "#alert-tabs button" in selectors
    for selector in selectors:
        matched = _matched_by(selector)
        groups = {"incident" if "data-incview" in b else "alert" for b in matched
                  if "data-incview" in b or "data-alertview" in b}
        assert len(groups) <= 1, f"{selector} matches buttons from both tab groups"
        if groups:  # a tab handler must match tab buttons only, never other buttons
            assert all("data-incview" in b or "data-alertview" in b for b in matched), selector
    # the shared look is a neutral class that no script selects on
    assert ".inc-tabs" not in PAGE and 'class="view-tabs"' in PAGE


def test_incident_requests_can_never_carry_an_undefined_view():
    body = function_body("refreshIncidents")
    assert 'view: INC_VIEWS.includes(incView) ? incView : "active"' in body
    assert 'const INC_VIEWS = ["active", "history"];' in PAGE
    handler = PAGE[PAGE.index('for (const tab of document.querySelectorAll("#inc-tabs button"))'):]
    assert "if (!INC_VIEWS.includes(tab.dataset.incview)) return;" in handler[:400]
    refresh_body = function_body("refresh")
    assert 'ALERT_VIEWS.includes(view) ? view : "unresolved"' in refresh_body
    assert '"&view=" + (ALERT_VIEWS' in refresh_body
