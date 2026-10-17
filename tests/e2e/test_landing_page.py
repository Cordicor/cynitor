#!/usr/bin/env python3
"""E2E tests for the Cynitor landing page and the Graph tab.

Runs against the static frontend only; no backend is required. The landing
page's assertions describe the disconnected state; the Graph tab is fed
through the same paths live data takes (see "Graph tab" below).

Prerequisites:
    pip install -r requirements.txt
    playwright install chromium

Usage:
    python3 test_landing_page.py

The script serves website/ itself on port 5500 unless something already
answers there (your own dev server, for instance), and shuts its server down
afterwards. Set CYNITOR_E2E_URL to test a frontend hosted elsewhere; the
script then never starts a server.
"""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

RESULT_FILE = Path(__file__).parent / "result.md"
WEBSITE_DIR = Path(__file__).resolve().parents[2] / "website"
BASE_URL = os.environ.get("CYNITOR_E2E_URL", "http://localhost:5500").rstrip("/")
WAIT_MS = 2000


# ── Frontend server ──

def _port_answers(url: str) -> bool:
    parsed = urlparse(url)
    try:
        with socket.create_connection((parsed.hostname, parsed.port or 80), timeout=0.5):
            return True
    except OSError:
        return False


@contextmanager
def frontend_server():
    """Serve website/ on BASE_URL's port unless something already answers there."""
    if "CYNITOR_E2E_URL" in os.environ or _port_answers(BASE_URL):
        yield
        return

    port = urlparse(BASE_URL).port
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--directory", str(WEBSITE_DIR)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while not _port_answers(BASE_URL):
            if proc.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"frontend server did not start on {BASE_URL}")
            time.sleep(0.1)
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ── Test registry ──
#
# Each test is an async function taking the shared page. They run in the
# order registered, and the first one navigates, so keep it first.

TESTS = []


def test(name):
    def register(fn):
        TESTS.append((name, fn))
        return fn
    return register


@test("Page loads with correct title")
async def _(page):
    await page.goto(BASE_URL, wait_until="networkidle")
    title = await page.title()
    assert "Cynitor" in title, f"Expected 'Cynitor' in title, got '{title}'"


@test("Sidebar is visible on load")
async def _(page):
    assert await page.locator("nav.sidebar").is_visible(), "Sidebar not visible"


def semaphore_starts_off(selector, label):
    @test(f"{label} semaphore starts disconnected")
    async def _(page):
        sem = page.locator(selector)
        assert await sem.is_visible(), f"{label} semaphore not visible"
        classes = await sem.get_attribute("class") or ""
        assert "on" not in classes, f"{label} semaphore should not be 'on', got classes: {classes}"


semaphore_starts_off("#serverSemaphore", "Server")
semaphore_starts_off("#canSemaphore", "CAN")


@test("Nodes table has correct headers")
async def _(page):
    await page.wait_for_selector(".tabulator-col-title", timeout=5000)
    raw_headers = await page.locator(".tabulator-col-title").all_text_contents()
    headers = [h.strip() for h in raw_headers if h.strip()]
    expected = ["ID", "Name", "State", "Health", "Mode", "VSSC", "SW", "Rate", "Uptime",
                "Publishers", "Subscribers", "Servers", "Clients"]
    assert headers == expected, f"Expected headers {expected}, got {headers}"


@test("Empty table shows 'Not connected'")
async def _(page):
    text = await page.locator(".tabulator-placeholder").text_content()
    assert "not connected" in text.lower(), f"Expected 'Not connected', got '{text}'"


@test("Filter row has 13 inputs")
async def _(page):
    count = await page.locator(".tabulator-header-filter input").count()
    assert count == 13, f"Expected 13 filter inputs, got {count}"


@test("Uptime reads at a glance; narrow tables drop the least telling columns")
async def _(page):
    shown = await page.evaluate("[59, 700, 7300, 266400].map(formatUptime)")
    assert shown == ["59s", "11m 40s", "2h 1m", "3d 2h"], f"Uptime shown as {shown}"
    size = page.viewport_size
    await page.set_viewport_size({"width": 900, "height": size["height"]})
    try:
        await page.wait_for_timeout(300)
        visible = await page.evaluate("nodesTabulator.getColumns().filter(c => c.isVisible()).map(c => c.getField())")
        for field in ("_sortId", "name", "state"):
            assert field in visible, f"{field} hidden at 900 px: {visible}"
        assert "clients" not in visible, f"Clients should hide first: {visible}"
    finally:
        await page.set_viewport_size(size)


@test("API URL input has default value")
async def _(page):
    value = await page.locator("#apiBase").input_value()
    assert "localhost:8080" in value, f"Expected localhost:8080, got '{value}'"


@test("Connect button shows 'Connect'")
async def _(page):
    text = await page.locator("#connectDashboardBtn").text_content()
    assert "connect" in text.lower(), f"Expected 'Connect', got '{text}'"


def has_aria_label(selector, label):
    @test(f"{label} has aria-label")
    async def _(page):
        assert await page.locator(selector).is_visible(), f"{label} not visible"
        assert await page.locator(selector).get_attribute("aria-label"), f"{label} missing aria-label"


has_aria_label("#themeToggle", "Theme toggle")
has_aria_label("#sidebarCollapseBtn", "Sidebar collapse button")


@test("Theme toggle switches theme")
async def _(page):
    # Light is the attribute being absent, matching the app's own default.
    read_theme = "document.documentElement.getAttribute('data-theme') || 'light'"
    before = await page.evaluate(read_theme)
    await page.locator("#themeToggle").click()
    await page.wait_for_function(f"({read_theme}) !== {before!r}", timeout=WAIT_MS)


@test("Sidebar collapse toggles both ways")
async def _(page):
    # The click handler toggles 'collapsed' on the sidebar element itself.
    sidebar = page.locator("nav.sidebar")
    assert "collapsed" not in (await sidebar.get_attribute("class") or ""), "Sidebar should start expanded"
    await page.locator("#sidebarCollapseBtn").click()
    await page.locator("nav.sidebar.collapsed").wait_for(state="attached", timeout=WAIT_MS)
    await page.locator("#sidebarCollapseBtn").click()
    await page.locator("nav.sidebar:not(.collapsed)").wait_for(state="attached", timeout=WAIT_MS)


@test("Sortable columns exist")
async def _(page):
    count = await page.locator(".tabulator-col.tabulator-sortable").count()
    assert count >= 4, f"Expected at least 4 sortable columns, got {count}"


async def open_log_panel(page):
    if "collapsed" in (await page.locator("#logPanel").get_attribute("class") or ""):
        await page.locator("#logPanelCollapseBtn").click()
    await page.locator("#logPanel:not(.collapsed)").wait_for(state="attached", timeout=WAIT_MS)


# The same path a node's uavcan.diagnostic.Record takes from the WebSocket.
INGEST_DIAGNOSTIC = """([ago, severity, text]) => ingestLogEvent({
    subject_id: 8184, message_type: 'Record_1_1', publisher_node_id: 50,
    timestamp_unix: Date.now() / 1000 - ago,
    attributes: [{attribute: 'severity', value: severity}, {attribute: 'text', value: text}]})"""


@test("Cards: a diagnostic's severity by name, as the log panel names it, and a zero timestamp as unknown")
async def _(page):
    card = await page.evaluate("""() => {
        const div = document.createElement('div');
        div.innerHTML = renderSubjectTable('Publishers', [{subjectId: 8184, messageType: 'uavcan.diagnostic.Record.1.1',
            rate: 0.3, attributes: [{attribute: 'timestamp', value: 0, unit: 'microsecond'},
                                    {attribute: 'severity', value: 3}, {attribute: 'text', value: 'hi'}]}]);
        const other = document.createElement('div');
        other.innerHTML = renderSubjectTable('Publishers', [{subjectId: 1300, messageType: 'ns.Status.1.0', rate: 1,
            attributes: [{attribute: 'severity', value: 3}, {attribute: 'timestamp', value: 5, unit: 'microsecond'}]}]);
        const text = (d) => [...d.querySelectorAll('.metric')].map((m) =>
            [m.querySelector('.metric-key').textContent, m.querySelector('.metric-val-text').textContent,
             m.querySelector('.metric-val').title]);
        return [text(div), text(other)];
    }""")
    assert card == [[["timestamp", "unknown", "0"], ["severity", "NOTICE", "3"], ["text", "hi", "hi"]],
                    [["severity", "3", "3"], ["timestamp", "5", "5"]]], f"Cards: {card}"


@test("History: Cynitor's register writes and firmware updates are rows of the node's history")
async def _(page):
    rows = await page.evaluate("""() => {
        const div = document.createElement('div');
        div.innerHTML = renderHistoryTimeline([
            {timestamp_unix: Date.now() / 1000, event_type: 'register_write',
             detail: {name: 'cordicor.test.bits', value: '1 0 1', read_back: 'true, false, true', status: 'ok'}},
            {timestamp_unix: Date.now() / 1000, event_type: 'register_write',
             detail: {name: 'limit', value: '<b>5</b>', status: 'timeout'}},
            {timestamp_unix: Date.now() / 1000, event_type: 'register_write',
             detail: {name: 'gain', value: '5', status: 'not_applied'}},
            {timestamp_unix: Date.now() / 1000, event_type: 'firmware_update', detail: {file: 'motor-1.2.app.bin'}}]);
        return [...div.querySelectorAll('.hist-row')].map((r) =>
            [r.querySelector('.hist-badge').textContent, r.querySelector('.hist-detail').textContent]);
    }""")
    assert rows == [["Register write", "cordicor.test.bits = 1 0 1 · read back true, false, true"],
                    ["Register write", "limit = <b>5</b> · no answer"],
                    ["Register write", "gain = 5 · not taken by the node"],
                    ["Firmware update", "from motor-1.2.app.bin"]], f"Rows: {rows}"


@test("Log panel shows diagnostics' severity, in time order")
async def _(page):
    await open_log_panel(page)
    await page.evaluate(INGEST_DIAGNOSTIC, [1, 5, "e2e newer error"])
    await page.evaluate(INGEST_DIAGNOSTIC, [5, 4, "e2e older warning"])  # arrives late
    rows = page.locator("#logList .log-row", has_text="e2e ")
    texts = [" ".join(t.split()) for t in await rows.all_inner_texts()]
    assert len(texts) == 2, f"Expected 2 rows, got {texts}"
    assert "WARN" in texts[0] and "older warning" in texts[0], f"Older row should come first: {texts}"
    assert "ERROR" in texts[1] and "newer error" in texts[1], f"Newer row should come last: {texts}"


@test("Log filter hides other rows and says when none match")
async def _(page):
    await open_log_panel(page)
    await page.locator("#logFilterInput").fill("older warning")
    visible = await page.locator("#logList .log-row:not(.hidden)").all_inner_texts()
    assert len(visible) == 1 and "older warning" in visible[0], f"Filter left: {visible}"
    await page.locator("#logFilterInput").fill("no such message")
    hint = page.locator("#logEmpty")
    assert await hint.is_visible(), "No hint when the filter matches nothing"
    assert "match" in (await hint.text_content()).lower(), f"Unexpected hint: {await hint.text_content()}"
    await page.locator("#logFilterInput").fill("")


@test("Cyphal v1.1 traffic is noticed under the CAN status")
async def _(page):
    notice = page.locator("#canV11Notice")
    assert await notice.is_hidden(), "Notice shown without v1.1 traffic"
    await page.evaluate("""() => {
        state.canConnected = true;
        state.cyphalV11 = {transfers: 3, nodes: [110], subject_count: 1, subject_ids: [54580],
                           last_seen_unix: Date.now() / 1000};
        renderV11Notice(); }""")
    try:
        text = await notice.inner_text()
        assert "v1.1" in text and "110" in text and "not decoded" in text, f"Notice reads: {text}"
    finally:
        await page.evaluate("state.canConnected = false; state.cyphalV11 = null; renderV11Notice()")


# ── Graph tab ──
#
# Fed through the paths live data takes: /api/nodes polling stores its reply
# in state.latestNodesPayload, and every WebSocket message goes through
# cacheEvent(). A timer in the page plays the publishers. The first test opens
# the tab and the last one closes it, so keep them in this order.

def _graph_node(nid, name, pubs, subs, gone=False, uid_byte=0, servers=(), clients=()):
    return {"node_id": nid, "unique_id": [uid_byte] * 16, "unique_id_hex": bytes([uid_byte] * 16).hex(),
            "uptime": 100, "has_disappeared": gone, "has_responded_to_getinfo": True, "name": name,
            "publishers": pubs, "subscribers": subs, "clients": list(clients), "servers": list(servers),
            "last_seen": []}


def _graph_ghost(last_nid, name, pubs, uid_byte):
    # A device whose node-ID another device took: keyed by its unique-ID.
    return {**_graph_node(None, name, pubs, [], gone=True, uid_byte=uid_byte), "last_node_id": last_nid, "_ghost": True}


GRAPH_NODES = {"node_count": 4, "nodes": {
    "10": _graph_node(10, "org.example.imu", [1100, 7509], [], uid_byte=1, servers=[100]),
    "20": _graph_node(20, "org.example.flight_controller", [1200, 7509], [1100, 7509], uid_byte=2, clients=[100]),
    "30": _graph_node(30, "org.example.esc", [7509], [1200], uid_byte=3),
    "40": _graph_node(40, "org.example.gps", [7509], [], gone=True, uid_byte=4),
    "uid:" + "aa" * 16: _graph_ghost(37, "org.example.old_sensor_a", [1500], 0xAA),
    "uid:" + "bb" * 16: _graph_ghost(38, "org.example.old_sensor_b", [1600], 0xBB),
}}

# Node 10 publishes 1100 until window.e2eImuPublishing is cleared; 20 publishes
# 1200; 10, 20 and 30 send Heartbeat at 1 Hz each; 40 is offline.
GRAPH_START = """(payload) => {
    state.dashboardConnected = true;
    state.canConnected = true;
    state.latestNodesPayload = payload;
    const ev = (subject_id, publisher_node_id, message_type, rate, subject_rate, payload_bytes, attributes = []) =>
        ({subject_id, publisher_node_id, message_type, rate, subject_rate, payload_bytes, attributes,
          timestamp_unix: Date.now() / 1000});
    let tick = 0;
    window.e2eImuPublishing = true;
    window.e2eGraphFeed = setInterval(() => {
        cacheEvent(ev(1200, 20, 'Vector4_0_1', 10, 10, 16));
        if (window.e2eImuPublishing) cacheEvent(ev(1100, 10, 'Vector3_1_0', 10, 10, 12));
        if (tick++ % 10) return;
        for (const [nid, health] of [[10, 'NOMINAL'], [20, 'NOMINAL'], [30, 'CAUTION']]) {
            cacheEvent(ev(7509, nid, 'Heartbeat_1_0', 1, 3, 7, [{attribute: 'health', value: health}]));
        }
    }, 100);
}"""

GRAPH_STOP = """() => {
    clearInterval(window.e2eGraphFeed);
    state.dashboardConnected = false;
    state.canConnected = false;
    state.latestNodesPayload = null;
    state.latestBySubject.clear();
    state.latestByNode.clear();
    switchView('nodes');
}"""

# Edges and nodes as drawn, read back from D3's data on the SVG elements.
GRAPH_LINKS = """() => Object.fromEntries([...document.querySelectorAll('#graphContainer .graph-link')].map(el => {
    const d = d3.select(el).datum();
    const end = (n) => typeof n === 'object' ? n.id : n;
    return [`${end(d.source)}->${end(d.target)}`, {live: el.classList.contains('graph-link--live'),
        silent: el.classList.contains('graph-link--silent'),
        arrow: el.getAttribute('marker-end'), subjects: d.subjects || (d.subjectId != null ? [d.subjectId] : [])}];
}))"""

GRAPH_NODES_DRAWN = """() => Object.fromEntries([...document.querySelectorAll('#graphContainer .graph-node')].map(el => {
    const d = d3.select(el).datum();
    const label = [...document.querySelectorAll('#graphContainer .graph-label')].find(l => d3.select(l).datum().id === d.id);
    return [d.id, {pinned: el.classList.contains('graph-node--pinned'), fx: d.fx ?? null,
        shownId: el.querySelector('.graph-node-id')?.textContent ?? null, name: d.fullName ?? null,
        classes: el.getAttribute('class'), badge: el.querySelector('.graph-health-badge')?.textContent ?? null,
        label: label?.querySelector('.graph-label-name')?.textContent ?? null,
        status: label?.querySelector('.graph-label-status')?.textContent ?? null}];
}))"""


async def open_graph_display(page):
    if not await page.evaluate("document.getElementById('graphDisplay').open"):
        await page.locator("#graphDisplay > summary").click()


async def close_graph_display(page):
    if await page.evaluate("document.getElementById('graphDisplay').open"):
        await page.locator("#graphDisplay > summary").click()

GRAPH_LINK_STATS = """() => Object.fromEntries([...document.querySelectorAll('#graphContainer .graph-link-label')].map(el => {
    const d = d3.select(el).datum();
    const end = (n) => typeof n === 'object' ? n.id : n;
    return [`${end(d.source)}->${end(d.target)}`, el.textContent];
}))"""


@test("Graph: a subject that stops publishing goes silent")
async def _(page):
    await page.evaluate(GRAPH_START, GRAPH_NODES)
    await page.locator("#viewTabGraph").click()
    # A new user starts without the system subjects; the tests below use Heartbeat.
    hide_system = page.locator("#graphHideSystem")
    assert await hide_system.is_checked(), "Hide system should start on"
    await hide_system.uncheck()
    imu = f"({GRAPH_LINKS})()['dev:10->sub:1100']"
    await page.wait_for_function(f"{imu}?.live === true", timeout=5000)
    await page.evaluate("window.e2eImuPublishing = false")
    # Silent after three message periods, two seconds at least.
    await page.wait_for_function(f"{imu}?.silent === true", timeout=5000)
    links = await page.evaluate(GRAPH_LINKS)
    assert not links["dev:10->sub:1100"]["live"], "A silent edge is still drawn as traffic"
    assert links["dev:20->sub:1200"]["live"], "Subject 1200, still publishing, went silent too"


@test("Graph: an edge carries its own publisher's rate, an offline one none")
async def _(page):
    await open_graph_display(page)
    await page.locator("#graphShowLinkStats").check()
    try:
        # Heartbeat's 3 Hz is three nodes at 1 Hz each.
        await page.wait_for_function(f"({GRAPH_LINK_STATS})()['dev:10->sub:7509'] === '1 Hz · 7 B'", timeout=3000)
        stats = await page.evaluate(GRAPH_LINK_STATS)
        assert stats["dev:30->sub:7509"] == "1 Hz · 7 B", f"Node 30's Heartbeat edge reads {stats['dev:30->sub:7509']!r}"
        assert stats["dev:40->sub:7509"] == "", f"Offline node 40's edge reads {stats['dev:40->sub:7509']!r}"
        links = await page.evaluate(GRAPH_LINKS)
        assert not links["dev:40->sub:7509"]["live"], "Offline node 40's edge is drawn live"
    finally:
        await page.locator("#graphShowLinkStats").uncheck()
        await close_graph_display(page)


@test("Graph: devices that lost their node-ID keep their own identity")
async def _(page):
    nodes = await page.evaluate(GRAPH_NODES_DRAWN)
    ghosts = sorted((n["shownId"], n["name"]) for key, n in nodes.items() if key.startswith("dev:uid:"))
    expected = [("37", "org.example.old_sensor_a"), ("38", "org.example.old_sensor_b")]
    assert ghosts == expected, f"Devices that lost their node-ID drawn as {ghosts}, of {list(nodes)}"


@test("Graph: only what is unusual is coloured, and nothing pulses")
async def _(page):
    nodes = await page.evaluate(GRAPH_NODES_DRAWN)
    marked = ("graph-node--warn", "graph-node--err", "graph-node--offline")
    usual = {key: nodes[key]["classes"] for key in ("dev:10", "dev:20")
             if any(m in nodes[key]["classes"] for m in marked)}
    assert not usual, f"NOMINAL nodes are marked: {usual}"
    caution = nodes["dev:30"]
    assert "graph-node--warn" in caution["classes"] and caution["badge"] == "!", f"CAUTION node drawn as {caution}"
    offline = nodes["dev:40"]
    assert "graph-node--offline" in offline["classes"] and (offline["status"] or "").startswith("offline"), \
        f"Offline node drawn as {offline}"
    pulsing = await page.evaluate("""() => [...document.querySelectorAll('#graphContainer .graph-node *')]
        .filter(el => getComputedStyle(el).animationName !== 'none').length""")
    assert pulsing == 0, f"{pulsing} node parts animate"


@test("Graph: subjects are labelled by their ID first")
async def _(page):
    nodes = await page.evaluate(GRAPH_NODES_DRAWN)
    labels = {key: n["label"] for key, n in nodes.items() if key.startswith("sub:")}
    wrong = {key: label for key, label in labels.items() if not label.startswith(key[4:])}
    assert labels and not wrong, f"Subject labels not led by their ID: {wrong}"


@test("Graph: the layered layout puts devices and subjects in bands of their own")
async def _(page):
    await page.wait_for_function("""() => {
        const ys = (type) => [...document.querySelectorAll(`#graphContainer .graph-node--${type}`)]
            .map(el => d3.select(el).datum().y);
        return Math.max(...ys('device')) < Math.min(...ys('subject'));
    }""", timeout=5000)


@test("Graph: hovering an edge says what it carries")
async def _(page):
    mid = await page.evaluate("""() => {
        const el = [...document.querySelectorAll('#graphContainer .graph-link-hit')].find(e => {
            const d = d3.select(e).datum(); return d.source.id === 'dev:20' && d.target.id === 'sub:1200'; });
        const r = el.getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    }""")
    await page.mouse.move(mid["x"], mid["y"])
    tooltip = page.locator("#graphTooltip")
    await tooltip.wait_for(state="visible", timeout=WAIT_MS)
    text = await tooltip.inner_text()
    await page.mouse.move(5, 5)
    assert "subject 1200" in text and "published by 20" in text and "Hz" in text, f"Tooltip reads: {text}"
    await tooltip.wait_for(state="hidden", timeout=WAIT_MS)


@test("Graph: a click selects a node without moving or pinning it")
async def _(page):
    box = await page.evaluate("""() => {
        const el = [...document.querySelectorAll('#graphContainer .graph-node')]
            .find(e => d3.select(e).datum().id === 'dev:20');
        const r = el.querySelector('circle').getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2};
    }""")
    await page.mouse.click(box["x"], box["y"])
    await page.locator("#graphInfo:not(.hidden)").wait_for(timeout=WAIT_MS)
    node = (await page.evaluate(GRAPH_NODES_DRAWN))["dev:20"]
    assert not node["pinned"] and node["fx"] is None, f"The click pinned the node: {node}"


@test("Graph: the open inspector keeps its buttons while it refreshes, and draws rate sparklines")
async def _(page):
    # Rebuilt each second, its buttons were swapped out under the pointer and
    # clicks were lost. Sparklines move each second; the buttons must stay.
    kept = await page.evaluate("""() => new Promise(done => {
        const panel = document.getElementById('graphInfo');
        const before = [...panel.querySelectorAll('button')];
        setTimeout(() => {
            const after = [...panel.querySelectorAll('button')];
            done(before.length > 0 && before.length === after.length && before.every((b, i) => b === after[i]));
        }, 2500);
    })""")
    sparks = await page.locator("#graphInfo .graph-spark polyline").count()
    await page.locator("#graphInfoClose").click()
    assert kept, "The inspector replaced its buttons while refreshing"
    assert sparks > 0, "No rate sparklines in the inspector"


@test("Graph: the inspector docks beside the canvas, follows its rows, and Esc closes it")
async def _(page):
    await page.evaluate("""() => [...document.querySelectorAll('#graphContainer .graph-node')]
        .find(e => d3.select(e).datum().id === 'dev:20').dispatchEvent(new MouseEvent('click', {bubbles: true}))""")
    inspector = page.locator("#graphInfo")
    await inspector.wait_for(state="visible", timeout=WAIT_MS)
    canvas = await page.locator("#graphContainer .graph-svg-wrap").bounding_box()
    panel = await inspector.bounding_box()
    assert canvas["x"] + canvas["width"] <= panel["x"] + 1, f"Inspector overlaps the canvas: {canvas} / {panel}"
    await inspector.locator('[data-select="sub:1200"]').click()
    text = await inspector.inner_text()
    assert "Subject-ID" in text and "1200" in text, f"A port row did not select its subject: {text}"
    await page.keyboard.press("Escape")
    await inspector.wait_for(state="hidden", timeout=WAIT_MS)


@test("Graph: Enter in the filter goes to the match; a focused node selects on Enter")
async def _(page):
    await page.fill("#graphFilter", "30")
    await page.press("#graphFilter", "Enter")
    inspector = page.locator("#graphInfo")
    await inspector.wait_for(state="visible", timeout=WAIT_MS)
    assert "org.example.esc" in await inspector.inner_text(), "Enter did not select node 30"
    await page.fill("#graphFilter", "")
    await page.keyboard.press("Escape")
    await page.evaluate("document.querySelector('#graphContainer .graph-node--device').focus()")
    label = await page.evaluate("document.activeElement.getAttribute('aria-label')")
    await page.keyboard.press("Enter")
    await inspector.wait_for(state="visible", timeout=WAIT_MS)
    await page.keyboard.press("Escape")
    assert label and label.startswith("Device "), f"Focused node is named {label!r}"


@test("Graph: the drawing exports as an SVG file")
async def _(page):
    await open_graph_display(page)
    try:
        async with page.expect_download() as download:
            await page.locator("#graphExportSvg").click()
        path = await (await download.value).path()
        text = Path(path).read_text()
        assert text.startswith("<svg") and "org.example.esc" in text and "graph-link-hit" not in text, \
            f"Export is not the drawing: {text[:120]}"
    finally:
        await close_graph_display(page)


@test("Graph: a node heard only in Cyphal v1.1 is drawn, marked not decoded")
async def _(page):
    await page.evaluate("""() => { state.cyphalV11 = {transfers: 9, nodes: [10, 110], subject_count: 2,
        subject_ids: [54580, 54581], last_seen_unix: Date.now() / 1000}; }""")
    try:
        await page.wait_for_function(f"({GRAPH_NODES_DRAWN})()['dev:v11:110'] !== undefined", timeout=5000)
        nodes = await page.evaluate(GRAPH_NODES_DRAWN)
        node = nodes["dev:v11:110"]
        assert "graph-node--v11" in node["classes"] and node["status"] == "v1.1 · not decoded", f"v1.1 node: {node}"
        assert "dev:v11:10" not in nodes, "Node 10, known from v1.0, drawn twice"
        chips = " ".join(await page.locator("#graphStatus .graph-chip").all_inner_texts())
        assert "1 Cyphal v1.1, not decoded" in " ".join(chips.split()), f"Strip: {chips}"
    finally:
        await page.evaluate("state.cyphalV11 = null")


@test("Graph: the legend opens from its button")
async def _(page):
    legend = page.locator("#graphLegend .graph-legend-menu")
    assert await legend.is_hidden(), "Legend shown without being asked for"
    await page.locator("#graphLegend > summary").click()
    assert await legend.is_visible() and "silent" in await legend.inner_text(), "Legend did not open"
    await page.keyboard.press("Escape")
    assert await legend.is_hidden(), "Escape did not close the legend"


@test("Graph: the status strip counts what needs a look, and picks it out")
async def _(page):
    chips = [" ".join(t.split()) for t in await page.locator("#graphStatus .graph-chip").all_inner_texts()]
    # Offline is every device not on the bus: node 40, and the two remembered ones that hold no node-ID.
    for expected in ("3 offline", "1 unusual health", "1 silent"):
        assert expected in chips, f"Missing {expected!r} in the strip: {chips}"
    assert not any("displaced" in chip for chip in chips), f"The strip still counts devices as displaced: {chips}"
    offline = page.locator('#graphStatus [data-focus="offline"]')
    await offline.click()
    dimmed = await page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('#graphContainer .graph-node')]
        .map(el => [d3.select(el).datum().id, el.classList.contains('graph-dim')]))""")
    await offline.click()
    lit = sorted(key for key, dim in dimmed.items() if not dim)
    assert lit == ["dev:40", "dev:uid:" + "aa" * 16, "dev:uid:" + "bb" * 16], f"Picking 'offline' left these lit: {lit}"


@test("Graph: 'Open in Nodes' selects the device in the Nodes tab")
async def _(page):
    await page.evaluate("""() => [...document.querySelectorAll('#graphContainer .graph-node')]
        .find(e => d3.select(e).datum().id === 'dev:20').dispatchEvent(new MouseEvent('click', {bubbles: true}))""")
    await page.locator("#graphInfoOpen").click()
    try:
        shown = await page.evaluate("({view: state.activeView, node: state.selectedNodeId})")
        assert shown == {"view": "nodes", "node": 20}, f"After 'Open in Nodes': {shown}"
    finally:
        await page.evaluate("clearSelectedNode()")
        await page.locator("#viewTabGraph").click()


@test("Graph: 'Nodes only' honours Hide system and shows direction")
async def _(page):
    await page.select_option("#graphView", "nodes")
    await page.locator("#graphHideSystem").check()
    try:
        links = await page.evaluate(GRAPH_LINKS)
        system_only = [key for key, link in links.items()
                       if link["subjects"] and all(s >= 6144 for s in link["subjects"])]
        assert links and not system_only, f"Edges made only of system subjects: {system_only}"
        unmarked = [key for key, link in links.items() if not link["arrow"]]
        assert not unmarked, f"Edges without an arrowhead: {unmarked}"
    finally:
        await page.locator("#graphHideSystem").uncheck()
        await page.select_option("#graphView", "node-centric")


@test("Graph: traffic animates only on request, and never under reduced motion")
async def _(page):
    live_animations = """() => [...document.querySelectorAll('#graphContainer .graph-link--live')]
        .map(el => getComputedStyle(el).animationName)"""
    names = await page.evaluate(live_animations)
    assert names and set(names) == {"none"}, f"Traffic animates by default: {names}"
    await open_graph_display(page)
    await page.locator("#graphAnimate").check()
    try:
        names = await page.evaluate(live_animations)
        assert set(names) == {"graph-link-flow"}, f"'Animate traffic' did not animate: {names}"
        await page.emulate_media(reduced_motion="reduce")
        names = await page.evaluate(live_animations)
        assert set(names) == {"none"}, f"Animations still running under reduced motion: {names}"
    finally:
        await page.emulate_media(reduced_motion="no-preference")
        await page.locator("#graphAnimate").uncheck()
        await close_graph_display(page)


@test("Graph: Fit brings every node into view")
async def _(page):
    svg = page.locator("#graphContainer .graph-svg-wrap > svg")
    box = await svg.bounding_box()
    # Drag the view far off, then fit it back.
    await page.mouse.move(box["x"] + 40, box["y"] + 40)
    await page.mouse.down()
    await page.mouse.move(box["x"] + box["width"] - 10, box["y"] + box["height"] - 10, steps=5)
    await page.mouse.up()
    await page.locator("#graphFit").click()
    await page.wait_for_function("""() => {
        const s = document.querySelector('#graphContainer .graph-svg-wrap > svg').getBoundingClientRect();
        return [...document.querySelectorAll('#graphContainer .graph-node')].every(el => {
            const r = el.getBoundingClientRect();
            return r.left >= s.left && r.right <= s.right && r.top >= s.top && r.bottom <= s.bottom;
        });
    }""", timeout=WAIT_MS)


@test("Graph: service calls are drawn from client to server")
async def _(page):
    links = await page.evaluate("""() => [...document.querySelectorAll('#graphContainer .graph-link--svc')]
        .map(el => { const d = d3.select(el).datum(); return [d.source.id, d.target.id, d.services]; })""")
    assert links == [["dev:20", "dev:10", [100]]], f"Service edges: {links}"


@test("Graph: recent restarts and node-ID conflicts are said on the node")
async def _(page):
    # The backend's event log, answered here: node 10 restarted two minutes ago.
    async def events(route):
        now = time.time()
        body = {"now_unix": now, "events": [{"id": 1, "node_id": 10, "unique_id": None, "detail": None,
                                             "timestamp_unix": now - 125, "event_type": "restart_suspected"}]}
        await route.fulfill(json=body, headers={"Access-Control-Allow-Origin": "*"})

    await page.route("**/api/nodes/events*", events)
    try:
        # Polled every 5 s while the tab is open.
        await page.wait_for_function(f"({GRAPH_NODES_DRAWN})()['dev:10'].status === 'restarted 2m ago'", timeout=8000)
        chips = await page.locator("#graphStatus .graph-chip").all_inner_texts()
        assert any("1 restarted" in " ".join(c.split()) for c in chips), f"Strip: {chips}"
    finally:
        await page.unroute("**/api/nodes/events*", events)


@test("Graph: resizing the window keeps the drawing at screen scale")
async def _(page):
    size = page.viewport_size
    await page.set_viewport_size({"width": 1000, "height": 700})
    try:
        await page.wait_for_function("""() => {
            const svg = document.querySelector('#graphContainer .graph-svg-wrap > svg');
            return svg.getAttribute('viewBox') === `0 0 ${svg.parentNode.clientWidth} ${svg.parentNode.clientHeight}`;
        }""", timeout=WAIT_MS)
    finally:
        await page.set_viewport_size(size)
        await page.evaluate(GRAPH_STOP)


# ── Nodes and Subjects tables ──
#
# /api/nodes is answered here and polled as in a live session; messages go
# through cacheEvent(), from a timer in the page. Nothing else refreshes the
# tables, as with a bus gone quiet. The first test starts it all and the last
# one stops it, so keep them in this order.

TABLES_NODES = {"node_count": 6, "nodes": {
    "10": _graph_node(10, "org.example.imu", [1100, 7509], [], uid_byte=1),
    "20": _graph_node(20, "org.example.flight_controller", [1200, 1300, 7509], [1100], uid_byte=2),
    "21": _graph_node(21, "org.example.fc_backup", [1300, 7509], [], uid_byte=5),
    "30": _graph_node(30, "org.example.esc", [7509], [1200, 1300], uid_byte=3),
    "40": _graph_node(40, "org.example.gps", [7509], [], gone=True, uid_byte=4),
    "uid:" + "aa" * 16: _graph_ghost(37, "org.example.old_sensor", [1500], 0xAA),
}}

# 10 publishes 1100 until e2eFeeds[1100] is cleared; 20 publishes 1200, a
# vector, at the rate in e2eFeeds.rate1200; 1300 has two publishers, 20 at
# 10 Hz (value 3) and 21 at e2eFeeds.rate21, 5 Hz (value 300); 10, 20, 21 and
# 30 send Heartbeat at 1 Hz.
TABLES_START = """() => {
    state.dashboardConnected = true;
    state.canConnected = true;
    const ev = (subject_id, publisher_node_id, rate, subject_rate, attributes = []) =>
        ({subject_id, publisher_node_id, message_type: 'Real32_1_0', rate, subject_rate, payload_bytes: 4,
          attributes, timestamp_unix: Date.now() / 1000});
    const value = (v) => [{attribute: 'value', value: v}];
    let tick = 0;
    window.e2eFeeds = {1100: true, rate1200: 10, rate21: 5};
    window.e2eTablesFeed = setInterval(() => {
        if (e2eFeeds[1100]) cacheEvent(ev(1100, 10, 10, 10, value(1)));
        cacheEvent(ev(1200, 20, e2eFeeds.rate1200, e2eFeeds.rate1200, [{attribute: 'velocity', value: [2, 3, 4]}]));
        cacheEvent(ev(1300, 20, 10, 15, value(3)));
        if (tick % 2 === 0) cacheEvent(ev(1300, 21, e2eFeeds.rate21, 15, value(300)));
        if (tick++ % 10) return;
        for (const [nid, health, vssc] of [[10, 'NOMINAL', 0], [20, 'NOMINAL', 0], [21, 'WARNING', 0], [30, 'CAUTION', 42]]) {
            cacheEvent({...ev(7509, nid, 1, 4, [{attribute: 'health', value: health}, {attribute: 'vssc', value: vssc}]),
                        message_type: 'Heartbeat_1_0'});
        }
    }, 100);
    getAllNodes();
    startNodesPolling();
}"""

TABLES_STOP = """() => {
    clearInterval(window.e2eTablesFeed);
    stopNodesPolling();
    switchView('nodes');
    clearSelectedNode();
    state.dashboardConnected = false;
    state.canConnected = false;
    state.latestNodesPayload = null;
    state.latestBySubject.clear();
    state.latestByNode.clear();
    renderNodesTable();
}"""

TABLES_PAYLOAD = {"body": TABLES_NODES}


async def _answer_nodes(route):
    await route.fulfill(json=TABLES_PAYLOAD["body"], headers={"Access-Control-Allow-Origin": "*"})


def _cell(table, row, field):
    # null until the row is there (getRow gives false)
    return f"({table}.getRow({row!r}) || null)?.getCell('{field}').getElement().innerText.trim()"


def _card_rate(subject_id):
    return f"document.querySelector('#selectedNodeContent .subject-card[data-subject=\"{subject_id}\"] .card-rate')?.innerText.trim()"


@test("Tables: a subject that stops publishing loses its rate, in the table and on its card")
async def _(page):
    await page.route("**/api/nodes", _answer_nodes)
    await page.evaluate(TABLES_START)
    await page.evaluate("setSelectedNode(10)")
    # 10 Hz on 1100 and Heartbeat's 1 Hz.
    await page.wait_for_function(f"{_cell('nodesTabulator', 10, 'rate')} === '11.0 Hz'", timeout=5000)
    await page.wait_for_function(f"{_card_rate(1100)} === '10.0 Hz'", timeout=3000)
    before = await page.evaluate("getTotalMessageRate()")
    await page.evaluate("e2eFeeds[1100] = false")
    # Silent after three message periods, two seconds at least; no message
    # refreshes the table, the node poll does.
    await page.wait_for_function(f"{_cell('nodesTabulator', 10, 'rate')} === '1.0 Hz'", timeout=5000)
    await page.wait_for_function(f"{_card_rate(1100)} === 'silent'", timeout=3000)
    dot = await page.evaluate("!!document.querySelector('.subject-card[data-subject=\"1100\"] .live-dot')")
    assert not dot, "A silent subject's card still shows the live dot"
    after = await page.evaluate("getTotalMessageRate()")
    assert abs(before - after - 10) < 0.5, f"Total rate went from {before} to {after}, not 10 less"


@test("Tables: a card's rate follows the live rate; a subscriber's card shows the subject's")
async def _(page):
    await page.evaluate("setSelectedNode(20)")
    await page.wait_for_function(f"{_card_rate(1200)} === '10.0 Hz'", timeout=3000)
    await page.evaluate("e2eFeeds.rate1200 = 50")
    await page.wait_for_function(f"{_card_rate(1200)} === '50.0 Hz'", timeout=3000)
    # Node 30 subscribes 1300, which 20 sends at 10 Hz and 21 at 5 Hz.
    await page.evaluate("setSelectedNode(30)")
    await page.locator('.detail-tab[data-tab="subscribers"]').click()
    try:
        await page.wait_for_function(f"{_card_rate(1300)} === '15.0 Hz'", timeout=3000)
    finally:
        await page.locator('.detail-tab[data-tab="publishers"]').click()
        await page.evaluate("e2eFeeds.rate1200 = 10")


@test("Registers: a node that does not answer says so, not that the backend is unreachable")
async def _(page):
    async def silent_node(route):
        await route.fulfill(status=504, json={"error": "Node 20 does not answer register requests"},
                            headers={"Access-Control-Allow-Origin": "*"})
    await page.route("**/api/registers/20", silent_node)
    await page.evaluate("setSelectedNode(20)")
    await page.locator('.detail-tab[data-tab="registers"]').click()
    try:
        await page.locator("#selectedNodeContent .reg-refresh-btn").wait_for(timeout=3000)
        helper = page.locator("#selectedNodeContent .svc-state-helper")
        text = await helper.inner_text()
        assert text.startswith("Node 20 does not answer register requests."), f"Registers tab says: {text!r}"
        assert "backend" not in text, f"Registers tab blames the backend: {text!r}"
    finally:
        await page.unroute("**/api/registers/20", silent_node)
        await page.evaluate("REG_CACHE.clear()")
        await page.locator('.detail-tab[data-tab="publishers"]').click()


@test("Cards: a node's card shows its own message, not another publisher's of the subject")
async def _(page):
    # On a subject several nodes publish, the card showed whichever's came last
    # until the node's own arrived (audit F-043).
    shown = await page.evaluate("""() => {
        const saved = [new Map(state.latestByNode), new Map(state.latestBySubject)];
        const other = {subject_id: 7510, publisher_node_id: 100, timestamp_unix: Date.now() / 1000, rate: 1,
                       message_type: 'List_1_0', attributes: [{attribute: 'servers', value: [3333]}]};
        state.latestBySubject.set(7510, other);
        state.latestByNode.set(100, new Map([[7510, other]]));
        state.latestByNode.set(21, new Map());
        const before = buildSubjectDetailData([7510], 21)[0];
        const own = {...other, publisher_node_id: 21, attributes: [{attribute: 'servers', value: [430]}]};
        state.latestByNode.get(21).set(7510, own);
        const after = buildSubjectDetailData([7510], 21)[0];
        const subscriber = buildSubjectDetailData([7510], null)[0];
        state.latestByNode = saved[0]; state.latestBySubject = saved[1];
        return [before.attributes, before.messageType, after.attributes[0].value, subscriber.attributes[0].value];
    }""")
    assert shown == [[], "uavcan.node.port.List.1.0", [430], [3333]], f"Cards: {shown}"


@test("Tables: offline nodes that lost their node-ID stay last; health sorts by severity")
async def _(page):
    ids = "nodesTabulator.getRows('active').map(r => r.getData().id)"
    try:
        await page.evaluate("nodesTabulator.setSort('_sortId', 'desc')")
        order = await page.evaluate(ids)
        assert order[0] == 40 and order[-1] == "uid:" + "aa" * 16, f"ID descending: {order}"
        await page.evaluate("nodesTabulator.setSort('health', 'desc')")
        health = await page.evaluate("nodesTabulator.getRows('active').map(r => r.getData().health)")
        assert health[:4] == ["WARNING", "CAUTION", "NOMINAL", "NOMINAL"], f"Health descending: {health}"
        assert (await page.evaluate(ids))[-1] == "uid:" + "aa" * 16, "Health descending: the ghost is not last"
    finally:
        await page.evaluate("nodesTabulator.setSort('_sortId', 'asc')")


@test("Tables: a sorted table sorts again as values change, not under the pointer")
async def _(page):
    first = "nodesTabulator.getRows('active')[0].getData().id"
    await page.mouse.move(5, 5)  # off the table
    await page.evaluate("nodesTabulator.setSort('rate', 'desc')")
    try:
        assert await page.evaluate(first) == 20, "Node 20 sends the most"
        await page.locator("#nodesTable .tabulator-row").first.hover()
        await page.evaluate("e2eFeeds.rate21 = 50")
        await page.wait_for_timeout(4500)
        assert await page.evaluate(first) == 20, "Rows moved under the pointer"
        await page.mouse.move(5, 5)
        await page.wait_for_function(f"{first} === 21", timeout=5000)
    finally:
        await page.evaluate("e2eFeeds.rate21 = 5; nodesTabulator.setSort('_sortId', 'asc')")


@test("Tables: ID filters match whole IDs; the selected row shows no grey through")
async def _(page):
    ids = "nodesTabulator.getRows('active').map(r => r.getData().id)"
    try:
        await page.evaluate("nodesTabulator.setHeaderFilterValue('_sortId', '2')")
        assert await page.evaluate(ids) == [], "ID '2' matched nodes 20 and 21"
        await page.evaluate("nodesTabulator.setHeaderFilterValue('_sortId', '10, 21')")
        assert await page.evaluate(ids) == [10, 21], "ID '10, 21'"
        await page.evaluate("nodesTabulator.setHeaderFilterValue('_sortId', ''); nodesTabulator.setHeaderFilterValue('publishers', '130')")
        assert await page.evaluate(ids) == [], "Publishers '130' matched 1300"
        await page.evaluate("nodesTabulator.setHeaderFilterValue('publishers', '1300')")
        assert await page.evaluate(ids) == [20, 21], "Publishers '1300'"
    finally:
        await page.evaluate("nodesTabulator.setHeaderFilterValue('_sortId', ''); nodesTabulator.setHeaderFilterValue('publishers', '')")
    grey = await page.evaluate("getComputedStyle(document.querySelector('#nodesTable .tabulator-table')).backgroundColor")
    assert grey != "rgb(102, 102, 102)", "The table under the rows is the theme's grey"


@test("Tables: VSSC shows each heartbeat's vendor status code, 0 muted")
async def _(page):
    vssc = lambda nid: _cell('nodesTabulator', nid, 'vssc')  # noqa: E731
    assert await page.evaluate(vssc(30)) == "42", "Node 30 sends VSSC 42"
    assert await page.evaluate(vssc(40)) == "-", "An offline node has none"
    muted = await page.evaluate("nodesTabulator.getRow(10).getCell('vssc').getElement().querySelector('.text-muted') !== null")
    assert muted, "VSSC 0, the usual, is not muted"


@test("Tables: rows are reached and selected by keyboard")
async def _(page):
    # ↓ goes into the rows at the selected one, else at the first: none selected here.
    await page.evaluate("clearSelectedNode()")
    await page.focus("#nodesTable .tabulator-tableholder")
    try:
        for key in ("ArrowDown", "ArrowDown", "Enter"):
            await page.keyboard.press(key)
            await page.wait_for_timeout(150)
        assert await page.evaluate("state.selectedNodeId") == 20, "↓ ↓ Enter did not select node 20"
        await page.keyboard.press("Escape")
        on_holder = await page.evaluate("document.activeElement.classList.contains('tabulator-tableholder')")
        assert on_holder, "Escape did not leave the rows"
    finally:
        await page.evaluate("clearSelectedNode()")


@test("Tables: a port list shows its first IDs and how many more; the tooltip has it whole")
async def _(page):
    cell = "nodesTabulator.getRow(20).getCell('publishers').getElement()"
    shown = await page.evaluate(f"{cell}.innerText.trim()")
    assert shown == "1200, 1300 +1", f"Node 20's publishers read {shown!r}"
    title = await page.evaluate(f"{cell}.querySelector('.port-ids').title")
    assert title == "1200, 1300, 7509", f"Its tooltip: {title!r}"


@test("Tables: the Nodes strip counts what needs a look, and a count picks those rows out")
async def _(page):
    strip = page.locator("#nodesStatus")
    await page.wait_for_function("document.getElementById('nodesStatus').innerText.includes('unusual health')", timeout=3000)
    text = " ".join((await strip.inner_text()).split())
    assert text == "6 nodes 2 offline 2 unusual health Hide offline Compact", f"Strip: {text!r}"
    ids = "nodesTabulator.getRows('active').map(r => r.getData().id)"
    await strip.locator('[data-focus="health"]').click()
    try:
        assert await page.evaluate(ids) == [21, 30], "'unusual health' picks out nodes 21 and 30"
    finally:
        await strip.locator('[data-focus="health"]').click()
    assert len(await page.evaluate(ids)) == 6, "Clicked again, the count lets the rows go"


@test("Tables: Hide offline takes every offline node out of the Nodes table at once, says how many, and is remembered")
async def _(page):
    strip = page.locator("#nodesStatus")
    toggle = strip.locator("[data-hide-offline]")
    ids = "nodesTabulator.getRows('active').map(r => r.getData().id)"
    said = "document.getElementById('nodesStatus').innerText.replace(/\\s+/g, ' ').trim()"
    listed = await page.evaluate(ids)
    assert await toggle.count() == 1, "The Nodes strip has no Hide offline"
    await toggle.click()
    try:
        await page.wait_for_function(f"({ids}).length === 4", timeout=3000)
        hidden = {"ids": await page.evaluate(ids), "strip": await page.evaluate(said),
                  "pressed": await toggle.get_attribute("aria-pressed")}
        await page.wait_for_function(
            "JSON.parse(localStorage.getItem('cynitor.dashboard.settings.v1')).hideOfflineNodes === true", timeout=2000)
        # With every other node hidden by its own eye button, only offline nodes are
        # left to list: the empty table says where they went.
        await page.evaluate("""() => { for (const id of [10, 20, 21, 30]) {
            state.hiddenNodeIds.add(nodeStableKey(state.latestNodesPayload.nodes[id])); } renderNodesTable(); }""")
        await page.wait_for_function(f"({ids}).length === 0", timeout=3000)
        empty = await page.locator("#nodesTable .tabulator-placeholder").inner_text()
    finally:
        await page.evaluate("state.hiddenNodeIds.clear(); renderNodesTable()")
        await toggle.click()
    await page.wait_for_function(f"({ids}).length === 6", timeout=3000)
    assert len(listed) == 6 and hidden["ids"] == [10, 20, 21, 30], f"Listed {listed}; with Hide offline on: {hidden['ids']}"
    assert hidden["strip"] == "4 nodes · 2 offline hidden 2 unusual health Hide offline Compact" \
        and hidden["pressed"] == "true", f"The strip with Hide offline on: {hidden}"
    assert "2 offline nodes are hidden" in " ".join(empty.split()), f"The table with only offline nodes, hidden: {empty!r}"


@test("Tables: Compact makes the rows of both tables shorter, and is remembered")
async def _(page):
    height = "Math.round(document.querySelector('#nodesTable .tabulator-row').getBoundingClientRect().height)"
    normal = await page.evaluate(height)
    await page.locator("#nodesStatus [data-density]").click()
    try:
        await page.wait_for_function(f"{height} < {normal}", timeout=3000)
        # Settings are written a moment after a change.
        await page.wait_for_function(
            "JSON.parse(localStorage.getItem('cynitor.dashboard.settings.v1')).compactRows === true", timeout=2000)
    finally:
        await page.locator("#nodesStatus [data-density]").click()
    await page.wait_for_function(f"{height} === {normal}", timeout=3000)


@test("Tables: a node that lost its node-ID keeps its alias, and is forgotten only when confirmed")
async def _(page):
    ghost = "uid:" + "aa" * 16
    forgotten = []

    async def identity(route):
        forgotten.append(route.request.method)
        await route.fulfill(json={"status": "ok"}, headers={"Access-Control-Allow-Origin": "*"})

    await page.route("**/api/identity/*", identity)
    try:
        await page.locator("#nodesTable .tabulator-row", has_text="old_sensor").locator(
            '.tabulator-cell[tabulator-field="name"]').dblclick()
        await page.locator(".name-input").fill("bench sensor")
        await page.keyboard.press("Enter")
        await page.wait_for_function(f"""nodesTabulator.getRow('{ghost}').getCell('name').getElement()
            .querySelector('.name-display')?.textContent === 'bench sensor'""", timeout=3000)
        delete = page.locator("#nodesTable .tabulator-row", has_text="bench sensor").locator(".ghost-delete-btn")
        page.once("dialog", lambda dialog: asyncio.ensure_future(dialog.dismiss()))
        await delete.click()
        await page.wait_for_timeout(300)
        assert forgotten == [], f"Forgotten without asking: {forgotten}"
        page.once("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))
        await delete.click()
        await page.wait_for_timeout(300)
        assert forgotten == ["DELETE"], f"Not forgotten once confirmed: {forgotten}"
    finally:
        await page.unroute("**/api/identity/*", identity)
        await page.evaluate(f"setNodeAlias('{'aa' * 16}', ''); clearSelectedNode()")


@test("Tables: the Clients tab says the bus is not connected")
async def _(page):
    await page.evaluate("setSelectedNode(20); state.canConnected = false")
    await page.locator('.detail-tab[data-tab="clients"]').click()
    try:
        text = await page.locator("#selectedNodeContent").inner_text()
        assert "CAN bus not connected" in text, f"Clients tab, CAN down: {text!r}"
    finally:
        await page.evaluate("state.canConnected = true")
        await page.locator('.detail-tab[data-tab="publishers"]').click()
        await page.evaluate("clearSelectedNode()")


PLOT_PANELS = "[...document.querySelectorAll('.detail-plot-area .panel-label')].map(e => e.textContent)"


@test("Plots: a node's card plots its own messages, a vector one line per element")
async def _(page):
    await page.evaluate("setSelectedNode(20)")
    await page.locator('#selectedNodeContent .subject-card[data-subject="1300"]').click()
    try:
        await page.wait_for_function(f"{PLOT_PANELS}.join() === 'value'", timeout=3000)
        top = await page.evaluate("""Math.max(...[...document.querySelectorAll('.detail-plot-area .panel-y-axis .tick text')]
            .map(t => Number(t.textContent)))""")
        assert top < 10, f"Node 20's plot of 1300 reaches {top}: node 21's values (300) are in it"
        await page.locator('#selectedNodeContent .subject-card[data-subject="1200"]').click()
        await page.wait_for_function(f"{PLOT_PANELS}.join() === 'velocity[0],velocity[1],velocity[2]'", timeout=3000)
        card = await page.evaluate("document.querySelector('.subject-card[data-subject=\"1200\"] .metric-val-text').textContent")
        assert card == "[2, 3, 4]", f"The vector's card reads {card!r}"
    finally:
        await page.evaluate("clearSelectedNode()")


@test("Plots: the Nodes tab's plot has the Subjects tab's controls, shared")
async def _(page):
    await page.evaluate("setSelectedNode(20)")
    await page.locator('#selectedNodeContent .subject-card[data-subject="1300"]').click()
    try:
        await page.locator('.detail-plot-area .plot-window-btn[data-secs="300"]').click(timeout=3000)
        assert await page.evaluate("state.plotTimeWindow") == 300, "5m did not set the plot window"
        assert await page.locator(".detail-plot-area .plot-pause-btn").count() == 1, "No pause button"
    finally:
        await page.evaluate("state.plotTimeWindow = 60; saveSettings(); clearSelectedNode()")


@test("Plots: Subjects plots each publisher of a subject apart")
async def _(page):
    await page.locator("#viewTabSubjects").click()
    # A cell, not the row's middle, where a column's resize handle may be.
    row = page.locator("#subjectsTable .tabulator-row", has_text="1300").first.locator(
        '.tabulator-cell[tabulator-field="messageType"]')
    await row.click()
    try:
        await page.wait_for_function(f"{PLOT_PANELS}.join() === 'value · n20,value · n21'", timeout=3000)
    finally:
        await row.click()  # closes the plot
    # Types read one way, though the backend gives a class name (Heartbeat_1_0).
    shown = await page.evaluate(_cell('subjectsTabulator', 'sub:7509', 'messageType'))
    assert shown == "uavcan.node.Heartbeat.1.0", f"Heartbeat's type reads {shown!r}"


@test("Plots: Fill Rate keeps up once a field's history is full")
async def _(page):
    key = "1200:velocity[0]"
    # A full history, 3600 points: six minutes of a 10 Hz subject.
    await page.evaluate(f"""(() => {{ const now = Date.now() / 1000;
        state.subjectHistory.set('{key}', Array.from({{length: 3600}}, (_, i) => ({{t: now - 360 + i / 10, v: 2, n: 20}})));
    }})()""")
    await page.evaluate("openSubjectPlot(subjectsTabulator.getRow('sub:1200').getData())")
    fill = page.locator('.plot-controls input[aria-label="Fill rate interpolation"]')
    try:
        await fill.check()
        await page.wait_for_timeout(3000)
        behind = await page.evaluate(f"Date.now() / 1000 - _detailPlotCfg._smoothBufs.get('{key}').at(-1).t")
        assert behind < 1.5, f"With Fill Rate on, the plot is {behind:.1f} s behind"
    finally:
        await fill.uncheck()
        await page.evaluate("closeSubjectPlot()")


@test("Compare: a subject several nodes publish is compared one publisher at a time")
async def _(page):
    await page.locator("#viewTabCompare").click()
    await page.locator(".compare-add-btn", has_text="+ Add Graph").click()
    card = page.locator(".compare-graph-card").last
    try:
        await card.locator(".plot-series-filter").fill("1300 value")
        offered = await card.evaluate("(c) => [...c.querySelectorAll('.plot-series-list input')].map(i => i.dataset.key)")
        assert offered == ["1300:value@20", "1300:value@21"], f"For a subject two nodes publish, the list offers {offered}"
        await card.locator('.plot-series-list input[data-key="1300:value@21"]').check()
        await page.wait_for_function(
            "[...document.querySelectorAll('.compare-graph-card .plot-legend-item')].some(e => e.dataset.series === 'S1300 · value · n21')",
            timeout=3000)
        values = await page.evaluate("[...new Set(plotData('1300:value@21').map(p => p.v))]")
        assert values == [300], f"Node 21's series holds {values}"
    finally:
        await card.locator('[aria-label="Remove graph"]').click()


@test("Plots: Compare opens what a node's plot shows in a new Compare graph")
async def _(page):
    await page.locator("#viewTabNodes").click()
    await page.evaluate("setSelectedNode(20)")
    await page.locator('#selectedNodeContent .subject-card[data-subject="1200"]').click()
    try:
        await page.wait_for_function(f"{PLOT_PANELS}.join() === 'velocity[0],velocity[1],velocity[2]'", timeout=3000)
        button = page.locator("#selectedNodeContent .plot-to-compare")
        assert await button.count() == 1, "The plot has no Compare button"
        await button.click()
        assert await page.evaluate("state.activeView") == "compare", "Compare did not open the Compare tab"
        keys = await page.evaluate("state.compareGraphs.at(-1).series.map(compareSeriesKey)")
        assert keys == ["1200:velocity[0]@20", "1200:velocity[1]@20", "1200:velocity[2]@20"], f"The new graph holds {keys}"
        card = page.locator(".compare-graph-card").last
        await page.wait_for_function(
            "document.querySelectorAll('.compare-graph-card:last-child .plot-legend-item').length === 3", timeout=3000)
        assert await card.locator(".compare-graph-name").input_value() == "S1200", "The new graph is not named for its subject"
    finally:
        await page.evaluate("""() => { const g = state.compareGraphs.at(-1);
            if (g) document.querySelector(`[data-graph-id="${g.id}"] .compare-graph-delete`)?.click();
            switchView('nodes'); clearSelectedNode(); }""")


@test("Plots: a graph made from a plot before Compare has opened gets an id of its own")
async def _(page):
    # As after a reload: a graph kept from before, and the Compare tab not
    # opened yet, so nothing has counted the graphs there are.
    await page.evaluate("""() => { state.compareGraphs.push(newCompareGraph({name: 'Kept', series: []}, 'cg_1'));
        _compareGraphIdCounter = 0; }""")
    await page.locator("#viewTabNodes").click()
    await page.evaluate("setSelectedNode(20)")
    await page.locator('#selectedNodeContent .subject-card[data-subject="1200"]').click()
    try:
        await page.wait_for_function(f"{PLOT_PANELS}.join() === 'velocity[0],velocity[1],velocity[2]'", timeout=3000)
        await page.locator("#selectedNodeContent .plot-to-compare").click()
        ids = await page.evaluate("state.compareGraphs.map(g => g.id)")
        assert len(set(ids)) == len(ids), f"Graphs share an id, and so a card: {ids}"
    finally:
        await page.evaluate("""() => { for (const b of [...document.querySelectorAll('.compare-graph-delete')]) b.click();
            state.compareGraphs.length = 0; saveSettings(); switchView('nodes'); clearSelectedNode(); }""")


@test("Tables: Subjects say how long ago they were heard, and how many bytes a second")
async def _(page):
    await page.locator("#viewTabSubjects").click()
    seen = lambda row: _cell('subjectsTabulator', row, 'age')  # noqa: E731
    await page.wait_for_function(f"/^\\d+s ago$/.test({seen('sub:1100')})", timeout=3000)
    warn = await page.evaluate("""subjectsTabulator.getRow('sub:1100').getCell('age').getElement()
        .querySelector('span').classList.contains('status-warn')""")
    assert warn, "A silent subject's age is not amber"
    assert await page.evaluate(seen("sub:1200")) == "now", "A live subject is not heard 'now'"
    # 1300: 4-byte messages at 15 Hz over its two publishers.
    shown = await page.evaluate(_cell('subjectsTabulator', 'sub:1300', 'bytesPerSec'))
    assert shown == "60 B/s", f"Subject 1300 shows {shown!r}"


@test("Tables: the Subjects strip lists one kind, and picks out the silent")
async def _(page):
    strip = page.locator("#subjectsStatus")
    rows = "subjectsTabulator.getRows('active').map(r => r.getData()._rowId)"
    await page.wait_for_function("document.getElementById('subjectsStatus').innerText.includes('1 silent')", timeout=3000)
    try:
        await strip.locator('[data-focus="silent"]').click()
        assert await page.evaluate(rows) == ["sub:1100"], "'silent' picks out subject 1100"
        await strip.locator('[data-focus="silent"]').click()
        await strip.locator('[data-kind="Service"]').click()
        assert await page.evaluate(rows) == [], "No node here serves anything"
    finally:
        await strip.locator('[data-kind="all"]').click()
    assert len(await page.evaluate(rows)) == 4, "All lists every subject again"


@test("Tables: Subjects says silent, and refreshes with nothing arriving")
async def _(page):
    await page.locator("#viewTabSubjects").click()
    await page.wait_for_function(f"{_cell('subjectsTabulator', 'sub:1100', 'rate')} === 'silent'", timeout=5000)
    shown = await page.evaluate(_cell('subjectsTabulator', 'sub:1300', 'rate'))
    assert shown == "15.0 Hz", f"Subject 1300, published by two nodes, shows {shown}"
    # The bus goes quiet: no message arrives to refresh anything.
    await page.evaluate("clearInterval(window.e2eTablesFeed)")
    silent = " && ".join(f"{_cell('subjectsTabulator', row, 'rate')} === 'silent'"
                         for row in ("sub:1200", "sub:1300", "sub:7509"))
    await page.wait_for_function(silent, timeout=6000)


@test("Tables: a table scrolled down is still there after a tab switch")
async def _(page):
    # Enough nodes, each with a subject of its own, to scroll both tables.
    many = {str(n): _graph_node(n, f"org.example.node_{n}", [2000 + n], [], uid_byte=n) for n in range(50, 170)}
    TABLES_PAYLOAD["body"] = {"nodes": {**TABLES_NODES["nodes"], **many}}
    in_view = """(id) => { const h = document.querySelector('#' + id + ' .tabulator-tableholder');
        const r = h.getBoundingClientRect();
        return {top: Math.round(h.scrollTop), rows: [...h.querySelectorAll('.tabulator-row')].filter(x => {
            const b = x.getBoundingClientRect(); return b.bottom > r.top && b.top < r.bottom; }).length}; }"""
    try:
        for here, there in (("nodes", "subjects"), ("subjects", "nodes")):
            table = f"{here}Table"
            await page.locator(f"#viewTab{here.capitalize()}").click()
            await page.wait_for_function(f"{here}Tabulator.getDataCount() > 100", timeout=5000)
            # Until the table has laid out its new rows, it cannot scroll that far.
            await page.wait_for_function(f"""(() => {{ const h = document.querySelector('#{table} .tabulator-tableholder');
                h.scrollTop = 1500; return h.scrollTop === 1500; }})()""", timeout=5000)
            await page.wait_for_timeout(500)
            await page.locator(f"#viewTab{there.capitalize()}").click()
            await page.wait_for_timeout(500)
            await page.locator(f"#viewTab{here.capitalize()}").click()
            await page.wait_for_timeout(1000)
            seen = await page.evaluate(in_view, table)
            assert seen["rows"] > 0 and abs(seen["top"] - 1500) < 2, f"{here.capitalize()} after a tab switch: {seen}"
    finally:
        TABLES_PAYLOAD["body"] = TABLES_NODES
        await page.evaluate(TABLES_STOP)
        await page.unroute("**/api/nodes", _answer_nodes)


# ── Compare tab ──
#
# Fed as live data is (cacheEvent), by a timer in the page: node 10 publishes
# 1100 (value 80 ± 5), 1700 (a current of about 1 mA) and 1400 (value 10,
# until e2eCompare.mute1400 is set) at 10 Hz; node 20 joins it on 1400 (value
# 50) once e2eCompare.join1400 is set. Each test starts the feed, and in its
# finally stops it and removes its graphs.

COMPARE_START = """() => {
    state.dashboardConnected = true;
    state.canConnected = true;
    const ev = (subject_id, publisher_node_id, attribute, value) => ({subject_id, publisher_node_id,
        message_type: 'Real32_1_0', rate: 10, subject_rate: 10, payload_bytes: 4,
        attributes: [{attribute, value}], timestamp_unix: Date.now() / 1000});
    window.e2eCompare = {join1400: false, mute1400: false};
    window.e2eCompareFeed = setInterval(() => {
        const t = Date.now() / 1000;
        cacheEvent(ev(1100, 10, 'value', 80 + 5 * Math.sin(t)));
        cacheEvent(ev(1700, 10, 'current', 0.00095 + 0.0001 * Math.sin(t)));
        if (!e2eCompare.mute1400) cacheEvent(ev(1400, 10, 'value', 10));
        if (e2eCompare.join1400) cacheEvent(ev(1400, 20, 'value', 50));
    }, 100);
    switchView('compare');
}"""

COMPARE_STOP = """() => {
    clearInterval(window.e2eCompareFeed);
    const ask = window.confirm;
    window.confirm = () => true;  // a graph with markers asks before it goes
    for (const button of [...document.querySelectorAll('.compare-graph-delete')]) button.click();
    window.confirm = ask;
    state.savedCompareConfigs = [];
    state.subjectHistory.clear();
    state.latestBySubject.clear();
    state.latestByNode.clear();
    state.dashboardConnected = false;
    state.canConnected = false;
    switchView('nodes');
    saveSettings();
}"""

# Each graph's card: how far its content runs past it, and the plot's height to draw in.
COMPARE_CARDS = """() => [...document.querySelectorAll('.compare-graph-card')].map(c => ({
    overflow: c.scrollHeight - c.clientHeight,
    drawHeight: Number(c.querySelector('.plot-overlay')?.getAttribute('height') || 0)}))"""


async def compare_pick(card, sid, field, node=None):
    """Ticks a series in a graph's list of series, found by typing in its filter."""
    await card.locator(".plot-series-filter").fill(f"S{sid} · {field}")
    key = f"{sid}:{field}" + (f"@{node}" if node is not None else "")
    match = "=" if node is not None else "^="  # any publisher's, else that node's
    await card.locator(f'.plot-series-list input[data-key{match}"{key}"]').first.check()
    await card.locator(".plot-series-filter").fill("")


async def compare_graph(page, *series):
    """A new graph with these (subject-ID, field[, node]) series, ticked in its list; its card."""
    n = await page.locator(".compare-graph-card").count()
    await page.locator(".compare-add-btn", has_text="+ Add Graph").click()
    card = page.locator(".compare-graph-card").nth(n)
    for s in series:
        await compare_pick(card, *s)
    return card


@test("Compare: graphs that do not fit keep their height, and the list scrolls")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        for _ in range(4):
            await compare_graph(page, (1100, "value"))
        await page.wait_for_timeout(500)
        for i, card in enumerate(await page.evaluate(COMPARE_CARDS)):
            assert card["overflow"] <= 1, f"Graph {i + 1}'s content runs {card['overflow']} px past its card"
            assert card["drawHeight"] >= 40, f"Graph {i + 1} has {card['drawHeight']} px to draw in"
    finally:
        await page.evaluate(COMPARE_STOP)


# The Display row's box that lets a click on the plot pause it (off by default).
CLICK_PAUSES = "A click on the plot pauses it"

# How far behind now a graph's plot is: a live 1m plot ends 12 s (20% of its
# window) after now.
COMPARE_LAG = "(c) => Date.now() / 1000 + 12 - c.querySelector('.detail-plot-area')._plotCtx.xScale.domain()[1]"


@test("Compare: a graph resumed after Pause All moves on, and Pause All says what it does")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        first = await compare_graph(page, (1100, "value"))
        await compare_graph(page, (1100, "value"))
        pause_all = page.locator(".compare-pause-all")
        await pause_all.click()
        await first.locator(".plot-pause-btn").click()  # the first one alone goes on
        await page.wait_for_timeout(2500)  # past the 2 s glide back to live
        lag = await first.evaluate(COMPARE_LAG)
        assert lag < 0.5, f"The resumed graph is {lag:.1f} s behind"
        label = await pause_all.inner_text()
        assert label == "Pause All", f"With one graph going on, the toolbar says {label!r}"
        # Paused by a click on its plot, the first is paused again: all are.
        await first.locator(f'[aria-label="{CLICK_PAUSES}"]').check()
        await first.locator(".detail-plot-area svg").click(position={"x": 300, "y": 40})
        await page.wait_for_timeout(400)
        label = await pause_all.inner_text()
        assert label == "Resume All", f"With every graph paused, the toolbar says {label!r}"
        await pause_all.click()
        paused = await page.evaluate("state.compareGraphs.map(g => g.paused)")
        assert paused == [False, False], f"Resume All left these paused: {paused}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a graph paused by a click on its plot says so on its own pause button, to screen readers too")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        button = card.locator(".plot-pause-btn")
        assert await button.get_attribute("aria-pressed") == "false", "Running, the pause button is not an unpressed toggle"
        await card.locator(f'[aria-label="{CLICK_PAUSES}"]').check()
        await card.locator(".detail-plot-area svg").click(position={"x": 300, "y": 40})
        await page.wait_for_timeout(400)  # past the 250 ms that tells a click from a double-click
        assert await page.evaluate("state.compareGraphs[0].paused"), "A click on the plot did not pause it"
        says = await button.inner_text()
        lit = await button.evaluate("(b) => b.classList.contains('active')")
        assert says == "▶" and lit, f"Paused, the graph's own button reads {says!r}{'' if lit else ', unlit'}"
        assert await button.get_attribute("aria-pressed") == "true", "Paused, the button is not pressed for a screen reader"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a click on a plot pauses it only when the graph's Click pauses is on, and it is kept")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        plot = card.locator(".detail-plot-area svg")
        await plot.click(position={"x": 300, "y": 40})
        await page.wait_for_timeout(400)
        assert not await page.evaluate("state.compareGraphs[0].paused"), "A plain click paused the graph"
        await card.locator(f'[aria-label="{CLICK_PAUSES}"]').check()
        await plot.click(position={"x": 300, "y": 40})
        await page.wait_for_timeout(400)
        assert await page.evaluate("state.compareGraphs[0].paused"), "With Click pauses on, a click did not pause the graph"
        kept = await page.evaluate("compareGraphConfig(state.compareGraphs[0]).clickPauses")
        assert kept is True, f"Saved, the graph's Click pauses reads {kept!r}"
    finally:
        await page.evaluate(COMPARE_STOP)


# Points of a graph's first line that fall inside its plot.
COMPARE_POINTS_SHOWN = """(c) => { const line = c.querySelector('.compare-line'), d = line?.getAttribute('d') || '';
    const k = line?.transform.baseVal.consolidate()?.matrix.a ?? 1;  // a path drawn scaled (in tenths of a pixel)
    const w = Number(c.querySelector('.plot-overlay').getAttribute('width'));
    return d.split(/[ML]/).filter(Boolean).map(s => k * Number(s.split(',')[0])).filter(x => x >= 0 && x <= w).length; }"""


@test("Compare: a paused graph keeps what it showed, in a replay too")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        # 1500 at 100 Hz: its history (3600 points a field) holds 36 s.
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('1500:value', Array.from({length: 3600}, (_, i) => ({t: now - 36 + i / 100, v: 1, n: 10}))); }""")
        card = await compare_graph(page, (1500, "value"))
        await card.locator('.plot-window-btn[data-secs="30"]').click()
        await card.locator(".plot-pause-btn").click()
        shown = await card.evaluate(COMPARE_POINTS_SHOWN)
        # 36 s later the history holds none of those points; a click on the legend redraws.
        await page.evaluate("""() => { const buf = state.subjectHistory.get('1500:value'); const last = buf.at(-1).t;
            for (let i = 1; i <= 3600; i++) { buf.push({t: last + 36 + i / 100, v: 5, n: 10}); buf.shift(); } }""")
        pill = card.locator(".plot-legend-item .plot-legend-label").first
        await pill.click()
        await pill.click()
        await page.wait_for_timeout(300)
        still = await card.evaluate(COMPARE_POINTS_SHOWN)
        assert still >= shown > 100, f"Paused with {shown} points shown; redrawn, it shows {still}"
        await card.locator('[aria-label="Remove graph"]').click()

        # A replay of a recording made on 1 October: paused, the graph stays then.
        await page.evaluate("""() => { state.replayActive = true; window.e2eReplayClock = Date.UTC(2026, 9, 1, 9) / 1000;
            window.e2eReplayFeed = setInterval(() => { e2eReplayClock += 0.1;
                cacheEvent({subject_id: 1600, publisher_node_id: 10, message_type: 'Real32_1_0', rate: 10, subject_rate: 10,
                            payload_bytes: 4, attributes: [{attribute: 'value', value: 1}], timestamp_unix: e2eReplayClock}); }, 100); }""")
        await page.wait_for_timeout(500)
        card = await compare_graph(page, (1600, "value"))
        await page.wait_for_timeout(500)
        await card.locator(".plot-pause-btn").click()
        await page.locator("#viewTabNodes").click()
        await page.locator("#viewTabCompare").click()  # draws paused graphs again
        await page.wait_for_timeout(300)
        right_edge = "(c) => c.querySelector('.detail-plot-area')._plotCtx.xScale.domain()[1]"
        recorded = "Date.UTC(2026, 9, 2) / 1000"  # before then
        assert await card.evaluate(f"(c) => ({right_edge})(c) < {recorded}"), "Paused in a replay, the graph moved to today"
        await card.locator(".plot-pause-btn").click()
        await page.wait_for_timeout(500)
        assert await card.evaluate(f"(c) => ({right_edge})(c) < {recorded}"), "Resumed in a replay, the graph went to today"
    finally:
        await page.evaluate("clearInterval(window.e2eReplayFeed); state.replayActive = false;")
        await page.evaluate(COMPARE_STOP)


COMPARE_LEGEND = "(c) => [...c.querySelectorAll('.plot-legend-item')].map(e => e.dataset.series)"


@test("Compare: a series keeps to the node it was added from")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1400, "value"))  # node 10 alone publishes 1400
        await page.evaluate("e2eCompare.join1400 = true")  # then node 20 does too
        await page.wait_for_timeout(600)
        values = await page.evaluate(
            "[...new Set(plotData(compareSeriesKey(state.compareGraphs[0].series[0])).map(p => p.v))]")
        assert values == [10], f"The series of node 10's 1400 plots {values}"
        legend = await card.evaluate(COMPARE_LEGEND)
        assert legend == ["S1400 · value · n10"], f"The legend reads {legend}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a series with nothing to plot stays in the legend, and colours stay put")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1400, "value"), (1100, "value"))
        await page.wait_for_timeout(300)
        stroke = """(c) => [...c.querySelectorAll('.compare-line')]
            .find(l => l.__data__.name === 'S1100 · value · n10')?.getAttribute('stroke')"""
        colour = await card.evaluate(stroke)
        # 1400 goes quiet, and its history goes too, as after a reconnect.
        await page.evaluate("e2eCompare.mute1400 = true; state.subjectHistory.delete('1400:value')")
        await page.wait_for_timeout(300)
        legend = await card.evaluate(COMPARE_LEGEND)
        assert legend == ["S1400 · value · n10", "S1100 · value · n10"], f"The legend reads {legend}"
        silent = card.locator('.plot-legend-item[data-series="S1400 · value · n10"]')
        assert "plot-legend-silent" in await silent.get_attribute("class"), "The silent series is not marked"
        assert await card.evaluate(stroke) == colour, "S1100 changed colour when S1400 went quiet"
        # With only the silent series left, it is still there to remove.
        await card.locator('.plot-legend-item[data-series="S1100 · value · n10"] .plot-legend-remove').click()
        await page.wait_for_timeout(300)
        assert await card.evaluate(COMPARE_LEGEND) == ["S1400 · value · n10"], "The silent series left the legend"
        await silent.locator(".plot-legend-remove").click()
        await page.wait_for_timeout(300)
        series = await page.evaluate("state.compareGraphs[0].series.length")
        assert series == 0, f"Removed from the legend, the graph still has {series} series"
    finally:
        await page.evaluate(COMPARE_STOP)


COMPARE_NOTE = "() => document.querySelector('.compare-graph-card .plot-empty-window')?.textContent || ''"


async def compare_note_is(page, expected):
    try:
        await page.wait_for_function(f"({COMPARE_NOTE})() === {expected!r}", timeout=2500)
    except Exception:
        shown = await page.evaluate(COMPARE_NOTE)
        raise AssertionError(f"The plot says {shown!r}, not {expected!r}") from None


@test("Compare: a graph says why it shows nothing")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        await compare_graph(page, (1100, "value"))
        await compare_note_is(page, "")
        await page.evaluate("state.canConnected = false")
        await compare_note_is(page, "CAN bus not connected")
        await page.evaluate("state.dashboardConnected = false")
        await compare_note_is(page, "Not connected to backend")
        await page.evaluate("state.dashboardConnected = true; state.canConnected = true")
        await compare_note_is(page, "")
        # The bus goes quiet: after a while nothing is left in a 30 s window.
        await page.locator('.compare-graph-card .plot-window-btn[data-secs="30"]').click()
        await page.evaluate("""() => { clearInterval(window.e2eCompareFeed);
            for (const points of state.subjectHistory.values()) for (const p of points) p.t -= 100; }""")
        await compare_note_is(page, "No data in view")
        await page.evaluate("state.subjectHistory.clear()")
        await compare_note_is(page, "Waiting for data…")
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: saved graphs and clones keep the whole graph, and untitled ones keep apart")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        await card.locator('[aria-label="Threshold value"]').fill("82")
        await card.locator(".plot-threshold-picker .plot-compare-add").click()
        await card.locator('.plot-window-btn[data-secs="300"]').click()
        # A marker and a drawing, as Shift+click and Alt+drag leave them.
        await page.evaluate("""() => { const g = state.compareGraphs[0]; const now = Date.now() / 1000;
            g.markers.push({t: now - 5, label: 'motor on', note: '', color: '#0969da', lineStyle: 'dashed'});
            g.drawings.push({points: [{t: now - 8, y: 0.2}, {t: now - 2, y: 0.6}], color: '#ef4444', width: 2, dash: 'solid'}); }""")
        await card.locator(".compare-graph-name").fill("Motor A")
        await card.locator(".compare-graph-save").click()
        await page.locator(".compare-saved-btn").click()
        await page.locator(".compare-saved-name", has_text="Motor A").click()
        await card.locator('[aria-label="Clone graph"]').click()
        graphs = await page.evaluate(
            "state.compareGraphs.map(g => [g.thresholds.length, g.timeWindow, g.markers.length, g.drawings.length])")
        assert graphs == [[1, 300, 1, 1]] * 3, f"Original, opened from Saved graphs, and its clone: {graphs}"
        for series in ((1100, "value"), (1700, "current")):
            unnamed = await compare_graph(page, series)
            await unnamed.locator(".compare-graph-save").click()
        names = await page.evaluate("state.savedCompareConfigs.map(c => c.name)")
        assert names == ["Motor A", "Untitled", "Untitled 2"], f"Saved graphs: {names}"
    finally:
        await page.evaluate(COMPARE_STOP)


async def compare_import(page, text):
    async with page.expect_file_chooser() as chooser:
        await page.locator(".compare-add-btn", has_text="Import Workspace").click()
    await (await chooser.value).set_files({"name": "workspace.json", "mimeType": "application/json",
                                           "buffer": text.encode()})
    await page.wait_for_timeout(300)


@test("Compare: Import checks the file first, and asks before it replaces the graphs")
async def _(page):
    await page.evaluate(COMPARE_START)
    asked = []
    accept = {"answer": False}

    async def on_dialog(dialog):
        asked.append(dialog.message)
        await (dialog.accept() if accept["answer"] else dialog.dismiss())

    page.on("dialog", on_dialog)
    shown = "() => [state.compareGraphs.length, document.querySelectorAll('.compare-graph-card').length]"
    try:
        await page.wait_for_timeout(300)
        await compare_graph(page, (1100, "value"))
        await compare_graph(page, (1700, "current"))
        await compare_import(page, "not a workspace")
        assert await page.evaluate(shown) == [2, 2], "A file that is not JSON changed the graphs"
        await compare_import(page, '{"graphs": []}')
        assert asked, "Import replaced the graphs without asking"
        assert await page.evaluate(shown) == [2, 2], "Answered no, Import still replaced the graphs"
        accept["answer"] = True
        await compare_import(page, '{"graphs": [{"name": "imported", "series": [{"subjectId": 1100, "attribute": "value"}],'
                                   ' "drawings": [{"color": "#f00"}]}, 5], "saved": [null, {"name": "kept", "series": []}]}')
        assert await page.evaluate(shown) == [1, 1], f"Imported, the graphs and cards are {await page.evaluate(shown)}"
        saved = await page.evaluate("state.savedCompareConfigs.map(c => c.name)")
        assert saved == ["kept"], f"Saved graphs from the file: {saved}"
        await page.locator(".compare-saved-btn").click()
        assert await page.locator(".compare-saved-name", has_text="kept").is_visible(), "The saved graphs do not open"
        await page.keyboard.press("Escape")
    finally:
        page.remove_listener("dialog", on_dialog)
        await page.evaluate(COMPARE_STOP)


@test("Compare: plots take the colours of the theme in use, loaded in it too")
async def _(page):
    theme = "document.documentElement.getAttribute('data-theme') || 'light'"
    token = "getComputedStyle(document.documentElement).getPropertyValue('--plot-1').trim()"
    stroke = "document.querySelector('.compare-graph-card .compare-line')?.getAttribute('stroke')"
    started_in = await page.evaluate(theme)
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        await compare_graph(page, (1100, "value"))
        for _ in range(2):  # to the other theme, and back
            await page.locator("#themeToggle").click()
            await page.wait_for_timeout(400)
            line, wanted = await page.evaluate(f"[{stroke}, {token}]")
            assert line == wanted, f"In {await page.evaluate(theme)}, a line is {line}, not the theme's {wanted}"
        # Dark when the page loads, as for someone who keeps it dark.
        if await page.evaluate(theme) != "dark":
            await page.locator("#themeToggle").click()
        await page.evaluate("clearInterval(window.e2eCompareFeed); state.dashboardConnected = false;"
                            " state.canConnected = false; _writeSettingsNow()")
        await page.reload(wait_until="load")
        first, wanted = await page.evaluate(f"[PLOT_COLORS[0], {token}]")
        assert first == wanted, f"Loaded in dark, plots start with {first}, not the theme's {wanted}"
    finally:
        if await page.evaluate(theme) != started_in:
            await page.locator("#themeToggle").click()
        await page.evaluate(COMPARE_STOP)


@test("Compare: the wheel scrolls past the graphs, Ctrl+wheel zooms one")
async def _(page):
    await page.evaluate(COMPARE_START)
    cards = page.locator(".compare-cards")

    async def over_first_plot():
        await cards.evaluate("(c) => { c.scrollTop = 0; }")
        await page.wait_for_timeout(200)
        box = await page.locator(".compare-graph-card").first.locator(".plot-overlay").bounding_box()
        await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)

    try:
        await page.wait_for_timeout(300)
        for _ in range(4):
            await compare_graph(page, (1100, "value"))
        await over_first_plot()
        await page.mouse.wheel(0, 300)
        await page.wait_for_timeout(300)
        scrolled, zoom = await cards.evaluate("(c) => [c.scrollTop, state.compareGraphs[0]._zoom]")
        assert scrolled > 0 and zoom == 1, f"The wheel over a plot scrolled {scrolled} px and zoomed it to {zoom}"
        await over_first_plot()
        await page.keyboard.down("Control")
        await page.mouse.wheel(0, -300)
        await page.keyboard.up("Control")
        zoom = await page.evaluate("state.compareGraphs[0]._zoom")
        assert zoom > 1, f"Ctrl+wheel up left the plot at zoom {zoom}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: the y-axis fits what is in view, thresholds included")
async def _(page):
    await page.evaluate(COMPARE_START)
    ticks = """() => [...document.querySelectorAll('.compare-graph-card .plot-compare-overlay .panel-y-axis .tick text')]
        .map(t => Number(t.textContent.replace(/,/g, '')))"""
    try:
        # 1100 read 1000 once, four minutes ago; live, it reads 80 ± 5.
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('1100:value', [{t: now - 240, v: 1000, n: 10}, {t: now - 239.9, v: 80, n: 10}]); }""")
        await page.wait_for_timeout(500)
        card = await compare_graph(page, (1100, "value"))
        await card.locator('.plot-window-btn[data-secs="30"]').click()
        await page.wait_for_timeout(400)
        top = max(await page.evaluate(ticks))
        assert top < 100, f"With 75..85 in view, the y-axis goes up to {top}"
        await card.locator('[aria-label="Threshold value"]').fill("100")
        await card.locator(".plot-threshold-picker .plot-compare-add").click()
        await page.wait_for_timeout(400)
        y, height = await card.evaluate("""(c) => [Number(c.querySelector('.plot-threshold line').getAttribute('y1')),
            Number(c.querySelector('.plot-overlay').getAttribute('height'))]""")
        assert 0 <= y <= height, f"The threshold at 100 is drawn at y {y:.0f}, off the plot (0..{height:.0f})"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: the tooltip keeps a value's digits, and has none for a series gone quiet")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1700, "current"), (1400, "value"))
        # 1400 went quiet 10 s ago.
        await page.evaluate("""() => { e2eCompare.mute1400 = true;
            for (const p of state.subjectHistory.get('1400:value')) p.t -= 10; }""")
        await page.wait_for_timeout(300)
        x = await card.evaluate("(c) => c.querySelector('.detail-plot-area')._plotCtx.xScale(Date.now() / 1000 - 0.3)")
        box = await card.locator(".plot-overlay").bounding_box()
        await page.mouse.move(box["x"] + x, box["y"] + 20)
        await page.wait_for_timeout(200)
        values = dict(await card.evaluate("""(c) => [...c.querySelectorAll('.plot-tooltip-row')]
            .map(r => [r.querySelector('.plot-tooltip-name').textContent, r.querySelector('.plot-tooltip-val').textContent])"""))
        current = values.get("S1700 · current · n10")
        assert current and 0.0008 < float(current) < 0.0011, f"A current of about 1 mA reads {current!r}"
        quiet = values.get("S1400 · value · n10")
        assert quiet == "–", f"Quiet for 10 s, S1400 reads {quiet!r} now"
    finally:
        await page.mouse.move(0, 0)
        await page.evaluate(COMPARE_STOP)


@test("Compare: subject 0 is compared like any other")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('0:value', Array.from({length: 20}, (_, i) => ({t: now - 2 + i / 10, v: 7, n: 10}))); }""")
        card = await compare_graph(page, (0, "value"))
        legend = await card.evaluate(COMPARE_LEGEND)
        assert legend == ["S0 · value · n10"], f"The legend reads {legend}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a threshold's pill hides its line, and its style stays a line")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        await card.locator('[aria-label="Threshold value"]').fill("82")
        await card.locator('[aria-label="Threshold label"]').fill("limit")
        await card.locator(".plot-threshold-picker .plot-compare-add").click()
        await page.wait_for_timeout(300)
        pill = card.locator('.plot-legend-item[data-series="limit"]')
        await pill.locator(".plot-legend-label").click()
        await page.wait_for_timeout(300)
        assert await card.locator(".plot-threshold").count() == 0, "Hidden from the legend, the threshold is still drawn"
        await pill.locator(".plot-legend-label").click()
        await page.wait_for_timeout(300)
        assert await card.locator(".plot-threshold").count() == 1, "Shown again, the threshold is not drawn"
        for _ in range(4):
            await pill.locator(".plot-legend-style").click()
        style = await page.evaluate("state.compareGraphs[0].thresholds[0].style")
        assert style == "solid", f"Four clicks on its style from dashed made it {style!r}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: with Fill Rate on, a cleared history leaves no old line")
async def _(page):
    await page.evaluate(COMPARE_START)
    drawn = "(c) => [...c.querySelectorAll('.compare-line')].filter(l => l.getAttribute('d')).length"
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        await card.locator('input[aria-label="Fill rate interpolation"]').check()
        await page.wait_for_timeout(1500)
        assert await card.evaluate(drawn) == 1, "With Fill Rate on, the series is not drawn"
        # A disconnect clears the history, and nothing comes after it.
        await page.evaluate("clearInterval(window.e2eCompareFeed); state.subjectHistory.clear()")
        await page.wait_for_timeout(1500)
        assert await card.evaluate(drawn) == 0, "The history cleared, Fill Rate still draws the old line"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: series are picked, shown, removed and saved graphs opened by keyboard")
async def _(page):
    # The graph is made before anything is heard: its list says so, then fills as messages come.
    await page.evaluate(COMPARE_START)
    await page.evaluate("clearInterval(window.e2eCompareFeed); state.subjectHistory.clear()")
    try:
        card = await compare_graph(page)
        heard = await card.locator(".plot-series-list").inner_text()
        assert "Nothing heard yet" in heard, f"Before any message, the list says {heard!r}"
        await page.evaluate(COMPARE_START)
        await page.wait_for_timeout(1500)
        await card.locator(".plot-series-filter").focus()
        await page.keyboard.type("1100")
        await page.keyboard.press("Tab")
        await page.keyboard.press("Space")
        await page.wait_for_timeout(300)
        assert await page.evaluate("state.compareGraphs[0].series.length") == 1, "Typed and ticked by keyboard, no series"
        name = "S1100 · value · n10"
        label = card.locator(f'.plot-legend-item[data-series="{name}"] .plot-legend-label')
        await label.focus()
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(200)
        hidden = await page.evaluate(f"state.compareGraphs[0]._hidden.has({name!r})")
        assert hidden and await label.get_attribute("aria-pressed") == "false", "Enter on a series' pill does not hide it"
        await card.locator(f'.plot-legend-item[data-series="{name}"] .plot-legend-remove').focus()
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(200)
        assert await page.evaluate("state.compareGraphs[0].series.length") == 0, "Enter on × does not remove the series"
        name_box = card.locator(".compare-graph-name")
        assert await name_box.get_attribute("aria-label") == "Graph name", "The graph's name box has no label"
        await name_box.fill("Keys")
        await card.locator(".compare-graph-save").click()
        await page.locator(".compare-saved-btn").focus()
        await page.keyboard.press("Enter")
        assert await page.locator(".compare-saved-btn").get_attribute("aria-expanded") == "true", "The menu does not say it is open"
        await page.keyboard.press("Tab")
        focused = await page.evaluate("document.activeElement.className + ' ' + document.activeElement.textContent")
        assert focused == "compare-saved-name Keys", f"Tab from Saved graphs goes to {focused!r}"
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(200)
        graphs = await page.evaluate("[state.compareGraphs.length, state.savedCompareConfigs.length]")
        assert graphs == [2, 1], f"Enter on a saved graph: [graphs, saved graphs] = {graphs}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: the marker form saves on Enter and closes on Escape")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        box = await card.locator(".plot-overlay").bounding_box()
        form = card.locator(".plot-marker-form")
        for x, keys in ((0.6, ("t", "h", "r", "o", "t", "t", "l", "e", "Enter")), (0.3, ("Escape",))):
            await page.keyboard.down("Shift")
            await page.mouse.click(box["x"] + box["width"] * x, box["y"] + 40)
            await page.keyboard.up("Shift")
            await page.wait_for_timeout(200)
            assert await form.count() == 1, "Shift+click opens no marker form"
            for key in keys:
                await page.keyboard.press(key)
            await page.wait_for_timeout(200)
            assert await form.count() == 0, f"{keys[-1]} leaves the marker form open"
        markers = await page.evaluate("state.compareGraphs[0].markers.map(m => m.label)")
        assert markers == ["throttle"], f"After Enter, then Escape, the markers are {markers}"
    finally:
        await page.evaluate(COMPARE_STOP)


# Counts the page's mousemove listeners and the graphs' crosshair-sync ones
# that are added and not yet removed, from now on.
COUNT_LISTENERS = """() => {
    if (!window.e2eListeners) {
        const counted = window.e2eListeners = {moves: new Set(), syncs: new Set()};
        const proto = EventTarget.prototype, add = proto.addEventListener, remove = proto.removeEventListener;
        const kept = (target, type) => (type === 'mousemove' && target === window ? counted.moves
            : type === 'crosshair-sync' ? counted.syncs : null);
        proto.addEventListener = function (type, fn, opts) { kept(this, type)?.add(fn); return add.call(this, type, fn, opts); };
        proto.removeEventListener = function (type, fn, opts) { kept(this, type)?.delete(fn); return remove.call(this, type, fn, opts); };
        window.e2eListenersOff = () => { proto.addEventListener = add; proto.removeEventListener = remove; delete window.e2eListeners; };
    }
    return [e2eListeners.moves.size, e2eListeners.syncs.size];
}"""


@test("Compare: graphs made, dragged and removed leave no listeners behind")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        before = await page.evaluate(COUNT_LISTENERS)
        for _ in range(5):
            card = await compare_graph(page, (1100, "value"))
            await page.wait_for_timeout(200)
            box = await card.locator(".plot-overlay").bounding_box()
            await page.mouse.move(box["x"] + 200, box["y"] + 30)
            await page.mouse.down()  # a drag pans the plot
            await page.mouse.move(box["x"] + 260, box["y"] + 30)
            await page.mouse.up()
            await card.locator('[aria-label="Remove graph"]').click()
        # A crosshair moving over the graphs lets removed ones' handlers go.
        await page.evaluate("""document.querySelector('.compare-cards')
            .dispatchEvent(new CustomEvent('crosshair-sync', {detail: {t: 0, source: null}}))""")
        after = await page.evaluate(COUNT_LISTENERS)
        assert after == before, f"Five graphs later, [page mousemove, crosshair-sync] listeners went {before} -> {after}"
    finally:
        await page.evaluate("window.e2eListenersOff?.()")
        await page.evaluate(COMPARE_STOP)


# A graph's first line: how many points it draws, the highest point (least y),
# the plot's width and height.
COMPARE_LINE = """(c) => { const line = c.querySelector('.compare-line'), d = line?.getAttribute('d') || '';
    const k = line?.transform.baseVal.consolidate()?.matrix.a ?? 1;  // a path drawn scaled (in tenths of a pixel)
    const ys = d.split(/[ML]/).filter(Boolean).map(s => k * Number(s.split(',')[1]));
    const overlay = c.querySelector('.plot-overlay');
    return {points: ys.length, top: Math.min(...ys), width: Number(overlay.getAttribute('width')),
            height: Number(overlay.getAttribute('height'))}; }"""


@test("Compare: a line draws what is in view, at most two points a pixel, and no spike is lost")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        # Six minutes of 1500 at 10 Hz: 300 points of them in a 30 s window.
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('1500:value', Array.from({length: 3600}, (_, i) => ({t: now - 360 + i / 10, v: Math.sin(i / 5), n: 10}))); }""")
        card = await compare_graph(page, (1500, "value"))
        await card.locator('.plot-window-btn[data-secs="30"]').click()
        await page.wait_for_timeout(400)
        line = await card.evaluate(COMPARE_LINE)
        assert line["points"] < 400, f"A 30 s window shows 300 points; the line draws {line['points']}"
        # 36 s of 1600 at 100 Hz, one spike in it, all in view: more points than pixels.
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('1600:value', Array.from({length: 3600}, (_, i) => ({t: now - 36 + i / 100, v: i === 1800 ? 50 : Math.sin(i / 20), n: 10}))); }""")
        card = await compare_graph(page, (1600, "value"))
        await card.locator('.plot-window-btn[data-secs="0"]').click()
        await page.wait_for_timeout(400)
        line = await card.evaluate(COMPARE_LINE)
        assert line["points"] <= 2 * line["width"] + 4, f"{line['width']:.0f} px wide, the line draws {line['points']} points"
        assert line["top"] < 0.1 * line["height"], f"The spike is lost: the line's top is at y {line['top']:.0f}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a redraw writes lines in whole tenths of a pixel, and filters a publisher's points once")
async def _(page):
    await page.evaluate(COMPARE_START)
    # The first line's path: whole numbers only, scaled back, its stroke as wide as before.
    written = """(c) => { const l = c.querySelector('.compare-line');
        return {fractions: /\\./.test(l.getAttribute('d')), transform: l.getAttribute('transform'),
                stroke: l.getAttribute('vector-effect')}; }"""
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value", 10))
        await page.wait_for_timeout(300)
        path = await card.evaluate(written)
        assert path == {"fractions": False, "transform": "scale(0.1)", "stroke": "non-scaling-stroke"}, \
            f"The line is written {path}"
        assert await card.evaluate(COMPARE_POINTS_SHOWN) > 0, "Scaled back, the line's points are not in the plot"
        # Asked twice while nothing new came, a publisher's points are filtered once; a new point makes them anew.
        same = await page.evaluate("""() => { clearInterval(window.e2eCompareFeed);
            return plotData('1100:value@10') === plotData('1100:value@10'); }""")
        assert same, "A publisher's points are filtered again with nothing new in the history"
        fresh = await page.evaluate("""() => { const before = plotData('1100:value@10');
            cacheEvent({subject_id: 1100, publisher_node_id: 10, message_type: 'Real32_1_0', rate: 10, subject_rate: 10,
                payload_bytes: 4, attributes: [{attribute: 'value', value: 99}], timestamp_unix: Date.now() / 1000});
            const after = plotData('1100:value@10');
            return after !== before && after[after.length - 1].v === 99; }""")
        assert fresh, "After a new point, a publisher's points are the old ones"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: synced graphs pause, take a time window and zoom together, and a graph that joins takes their view")
async def _(page):
    await page.evaluate(COMPARE_START)
    view = "(i) => { const g = state.compareGraphs[i]; return [g.paused, g.pausedAt, g.timeWindow, g._zoom]; }"
    try:
        await page.wait_for_timeout(300)
        a = await compare_graph(page, (1100, "value"))
        b = await compare_graph(page, (1100, "value"))
        c = await compare_graph(page, (1100, "value"))
        for card in (a, b):
            await card.locator(".compare-sync-btn").click()
        assert await a.locator(".compare-sync-btn").get_attribute("aria-pressed") == "true", "Sync is not shown as on"
        await a.locator(".plot-pause-btn").click()
        va, vb, vc = [await page.evaluate(view, i) for i in range(3)]
        assert va[0] and va[:2] == vb[:2], f"Paused, the synced graphs read {va} and {vb}"
        assert not vc[0], "A graph not synced was paused with them"
        assert await b.locator(".plot-pause-btn").inner_text() == "▶", "The other synced graph's button does not say it is paused"
        await b.locator('.plot-window-btn[data-secs="300"]').click()
        assert (await page.evaluate(view, 0))[2] == 300, "A window picked on one synced graph is not the other's"
        assert "active" in (await a.locator('.plot-window-btn[data-secs="300"]').get_attribute("class")), "Its 5m button is not lit"
        assert (await page.evaluate(view, 2))[2] == 60, "A graph not synced took the window"
        await a.locator(".compare-edit-btn").click()  # its plot, not its editing rows, under the pointer
        await a.scroll_into_view_if_needed()
        box = await a.locator(".plot-overlay").bounding_box()
        await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        await page.keyboard.down("Control")
        await page.mouse.wheel(0, -300)
        await page.keyboard.up("Control")
        za, zb = (await page.evaluate(view, 0))[3], (await page.evaluate(view, 1))[3]
        assert za > 1 and za == zb, f"Zoomed, the synced graphs read {za} and {zb}"
        await b.locator(".plot-pause-btn").click()  # resumed from the other: both go on
        assert not (await page.evaluate(view, 0))[0], "Resumed from one synced graph, the other stays paused"
        await c.locator(".compare-sync-btn").click()  # joins: takes their view
        assert await page.evaluate(view, 2) == await page.evaluate(view, 0), "A graph that joins keeps its own view"
        assert await page.evaluate("compareGraphConfig(state.compareGraphs[0]).sync") is True, "Sync is not kept with the graph"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: synced graphs show each other's markers, and a graph that leaves no longer does")
async def _(page):
    await page.evaluate(COMPARE_START)
    marks = "(i) => [...document.querySelectorAll('.compare-graph-card')[i].querySelectorAll('.plot-marker text')].map(t => t.textContent)"
    try:
        await page.wait_for_timeout(300)
        a = await compare_graph(page, (1100, "value"))
        b = await compare_graph(page, (1100, "value"))
        await compare_graph(page, (1100, "value"))  # not synced
        for card in (a, b):
            await card.locator(".compare-sync-btn").click()
        await b.locator(".plot-pause-btn").click()  # both paused: no tick redraws them
        await page.evaluate("""() => { const g = state.compareGraphs[0];
            g.markers.push({t: g.pausedAt - 5, label: 'motor on', note: '', color: '', lineStyle: 'dashed'});
            g._fingerprint = ''; _renderOneGraph(g); }""")
        shown = [await page.evaluate(marks, i) for i in range(3)]
        assert shown == [["motor on"], ["motor on"], []], f"The markers each graph shows: {shown}"
        said = await b.locator(".plot-marker title").text_content()
        assert "marked on" in said, f"On hover, a marker from another graph says {said!r}"
        await b.locator(".compare-sync-btn").click()  # leaves the group
        assert await page.evaluate(marks, 1) == [], "A graph that left the group still shows its markers"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a synced graph above another synced graph leaves its time labels to it, and draws taller")
async def _(page):
    await page.evaluate(COMPARE_START)
    labels = "(c) => [...c.querySelectorAll('.plot-x-axis .tick text')].filter(t => t.textContent).length"
    height = "(c) => Number(c.querySelector('.plot-overlay').getAttribute('height'))"
    try:
        await page.wait_for_timeout(300)
        a, b, c = [await compare_graph(page, (1100, "value")) for _ in range(3)]
        for card in (a, b, c):
            await card.locator(".compare-edit-btn").click()  # all alike: header, legend, plot
        for card in (a, b):
            await card.locator(".compare-sync-btn").click()
        await page.wait_for_timeout(400)
        assert await a.evaluate(labels) == 0, "A synced graph above another synced graph keeps its time labels"
        assert await b.evaluate(labels) > 0 and await c.evaluate(labels) > 0, "The last synced graph, or one not synced, has none"
        assert await a.evaluate(height) > await b.evaluate(height) + 8, "Without its labels, the graph above draws no taller"
        await b.locator(".compare-sync-btn").click()  # B leaves: nothing synced below A now
        await page.wait_for_timeout(300)
        assert await a.evaluate(labels) > 0, "With no synced graph below, a synced graph has no time labels"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: Collapse All collapses every graph, Expand All expands them, and the button says which it does")
async def _(page):
    await page.evaluate(COMPARE_START)
    button = page.locator(".compare-collapse-all")
    collapsed = "() => [...document.querySelectorAll('.compare-graph-card')].map(c => c.classList.contains('collapsed'))"
    try:
        await page.wait_for_timeout(300)
        first, *_ = [await compare_graph(page, (1100, "value")) for _ in range(3)]
        await first.locator('[aria-label="Collapse the graph to its plot"]').click()  # one collapsed on its own
        await page.wait_for_timeout(200)
        assert await button.inner_text() == "Collapse All", f"With one graph collapsed, the toolbar says {await button.inner_text()!r}"
        await button.click()
        assert await page.evaluate(collapsed) == [True, True, True], "Collapse All left some graphs expanded"
        await page.wait_for_timeout(200)
        assert await button.inner_text() == "Expand All", f"With every graph collapsed, the toolbar says {await button.inner_text()!r}"
        await button.click()
        assert await page.evaluate(collapsed) == [False, False, False], "Expand All left some graphs collapsed"
        kept = await page.evaluate("state.compareGraphs.map(g => compareGraphConfig(g).collapsed)")
        assert kept == [False, False, False], f"The graphs keep {kept}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a collapsed graph is its plot, a line of names and, at its side, its pause, window and Sync")
async def _(page):
    await page.evaluate(COMPARE_START)
    shown = "(e) => e.getClientRects().length > 0"
    plot_height = "(c) => Number(c.querySelector('.plot-overlay').getAttribute('height'))"
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"), (1700, "current"))
        await card.locator(".compare-edit-btn").click()  # folded: the header alone above the legend
        await page.wait_for_timeout(300)
        tall = await card.evaluate(plot_height)
        await card.locator('[aria-label="Collapse the graph to its plot"]').click()
        await page.wait_for_timeout(400)
        assert not await card.locator(".compare-card-header").evaluate(shown), "Collapsed, the graph keeps its header"
        assert not await card.locator(".compare-card-editor").evaluate(shown), "Collapsed, the graph keeps its editing rows"
        names = await card.evaluate("""(c) => [...c.querySelectorAll('.plot-legend-label')].map(l => Math.round(l.getBoundingClientRect().top))""")
        assert len(names) == 2 and len(set(names)) == 1, f"Collapsed, the names are not one line: their tops are {names}"
        values = await card.evaluate("(c) => [...c.querySelectorAll('.plot-legend-value, .plot-legend-head')].filter(e => e.getClientRects().length).length")
        assert values == 0, f"Collapsed, the legend still shows {values} values or headings"
        assert await card.evaluate(plot_height) > tall + 20, "Collapsed, the plot is no taller"
        side = card.locator(".compare-side")
        await side.locator('[aria-label="Time window"]').select_option("300")
        await side.locator(".plot-pause-btn").click()
        await side.locator(".compare-sync-btn").click()
        g = await page.evaluate("(() => { const g = state.compareGraphs[0]; return [g.timeWindow, g.paused, g.sync, compareGraphConfig(g).collapsed]; })()")
        assert g == [300, True, True, True], f"From the side: window, paused, synced, kept collapsed read {g}"
        await side.locator('[aria-label="Expand the graph: its header and legend"]').click()
        await page.wait_for_timeout(300)
        assert await card.locator(".compare-card-header").evaluate(shown), "Expanded, the graph has no header"
        back = await card.evaluate("(c) => ['.plot-pause-btn', '.compare-sync-btn'].map(s => !!c.querySelector(`.compare-time-controls ${s}`))")
        assert back == [True, True], f"Expanded, the pause and Sync buttons are back in the header: {back}"
        assert "active" in (await card.locator('.plot-window-btn[data-secs="300"]').get_attribute("class")), "The 5m button is not lit"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: Min/Max follows the lowest and highest of the last samples, and an Add says what it is missing")
async def _(page):
    await page.evaluate(COMPARE_START)
    toasts = "() => [...document.querySelectorAll('#toastContainer .toast')].map(t => t.textContent)"
    try:
        # 1500 rose 0, 1, ... 19 over the last 20 s.
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('1500:value', Array.from({length: 20}, (_, i) => ({t: now - 19.5 + i, v: i, n: 10}))); }""")
        card = await compare_graph(page, (1500, "value"))
        await card.locator('.plot-window-btn[data-secs="30"]').click()
        await card.locator(".plot-derived-type").select_option("min_max")
        window_box = card.locator('[aria-label="Window size (samples)"]')
        assert await window_box.is_visible(), "Min/Max asks for no window of samples"
        await window_box.fill("5")
        await card.locator('[aria-label="Source A"]').select_option("1500:value@10")
        await card.locator('[aria-label="Add derived series"]').click()
        await page.wait_for_timeout(400)
        rows = await card.evaluate(COMPARE_LEGEND_ROWS)
        last = {name[:4]: values[1] for name, values in rows.items() if name.startswith(("Min5", "Max5"))}
        assert last == {"Min5": "15", "Max5": "19"}, f"Over its last 5 samples, 15 to 19, Min/Max ends at {last} ({list(rows)})"
        # Delta with no Source B, then a threshold with no value: each Add says what it is missing.
        await card.locator(".plot-derived-type").select_option("delta")
        await card.locator('[aria-label="Add derived series"]').click()
        await card.locator('[aria-label="Add threshold line"]').click()
        said = await page.evaluate(toasts)
        assert any("Source B" in t for t in said) and any("threshold" in t for t in said), f"The Adds say {said}"
        assert await page.evaluate("state.compareGraphs[0].derivedSeries.length") == 1, "Delta was added with no Source B"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a graph scrolled out of view is not drawn, and catches up in view")
async def _(page):
    await page.evaluate(COMPARE_START)
    cards = page.locator(".compare-cards")
    line = "(c) => c.querySelector('.compare-line')?.getAttribute('d')"
    try:
        await page.wait_for_timeout(300)
        for _ in range(4):
            await compare_graph(page, (1100, "value"))
        await cards.evaluate("(c) => { c.scrollTop = 0; }")
        await page.wait_for_timeout(500)
        first, last = page.locator(".compare-graph-card").first, page.locator(".compare-graph-card").last
        before = [await first.evaluate(line), await last.evaluate(line)]
        await page.wait_for_timeout(1000)
        after = [await first.evaluate(line), await last.evaluate(line)]
        assert after[0] != before[0], "The graph in view is not drawn"
        assert after[1] == before[1], "The graph out of view is drawn all the same"
        await cards.evaluate("(c) => { c.scrollTop = c.scrollHeight; }")
        await page.wait_for_timeout(500)
        assert await last.evaluate(line) != after[1], "Scrolled into view, the graph does not catch up"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: removing a graph with markers or drawings asks first")
async def _(page):
    await page.evaluate(COMPARE_START)
    asked = []
    answer = {"yes": False}

    async def on_dialog(dialog):
        asked.append(dialog.message)
        await (dialog.accept() if answer["yes"] else dialog.dismiss())

    page.on("dialog", on_dialog)
    graphs = "state.compareGraphs.length"
    try:
        await page.wait_for_timeout(300)
        plain = await compare_graph(page, (1100, "value"))
        await plain.locator('[aria-label="Remove graph"]').click()
        assert not asked and await page.evaluate(graphs) == 0, "A graph with no markers is not removed at once"
        card = await compare_graph(page, (1100, "value"))
        await page.evaluate("""() => state.compareGraphs[0].markers.push(
            {t: Date.now() / 1000 - 5, label: 'motor on', note: '', color: '#0969da', lineStyle: 'dashed'})""")
        await card.locator('[aria-label="Remove graph"]').click()
        assert asked and "1 marker" in asked[-1], f"Removing a graph with a marker asks {asked}"
        assert await page.evaluate(graphs) == 1, "Answered no, the graph is removed all the same"
        answer["yes"] = True
        await card.locator('[aria-label="Remove graph"]').click()
        assert await page.evaluate(graphs) == 0, "Answered yes, the graph stays"
    finally:
        page.remove_listener("dialog", on_dialog)
        await page.evaluate(COMPARE_STOP)


@test("Compare: a graph's editing rows fold away under Edit, and a new graph starts on them")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        editor, edit = card.locator(".compare-card-editor"), card.locator(".compare-edit-btn")
        assert await editor.is_visible(), "A new graph does not start on its editing rows"
        await edit.click()
        assert not await editor.is_visible(), "Edit does not fold the editing rows away"
        assert await edit.get_attribute("aria-expanded") == "false", "Edit does not say it is folded"
        header, plot, whole = await card.evaluate("""(c) => [c.querySelector('.compare-card-header'),
            c.querySelector('.detail-plot-area'), c].map(e => e.getBoundingClientRect().height)""")
        assert header < 50 and plot > 0.8 * whole, f"Folded: a {header:.0f} px header, a {plot:.0f} px plot in {whole:.0f} px"
        assert await card.locator(".plot-window-btn").first.is_visible(), "Folded, the time window is out of reach"
        # A graph that has its series, opened from Saved graphs, starts on its plot.
        await card.locator(".compare-graph-save").click()
        await page.locator(".compare-saved-btn").click()
        await page.locator(".compare-saved-name").first.click()
        opened = page.locator(".compare-graph-card").nth(1)
        assert not await opened.locator(".compare-card-editor").is_visible(), "A saved graph opens on its editing rows"
        await edit.click()
        assert await editor.is_visible(), "Edit does not bring the editing rows back"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: series are found by any part of their name, and ticked on and off in one list")
async def _(page):
    await page.evaluate(COMPARE_START)
    keys = "(c) => [...c.querySelectorAll('.plot-series-list input')].map(i => i.dataset.key)"
    ticked = "(c) => [...c.querySelectorAll('.plot-series-list input:checked')].map(i => i.dataset.key)"
    try:
        await page.evaluate("e2eCompare.join1400 = true")  # node 20 publishes 1400 too
        await page.wait_for_timeout(400)
        card = await compare_graph(page)
        offered = await card.evaluate(keys)
        assert offered == ["1100:value@10", "1400:value@10", "1400:value@20", "1700:current@10"], f"The list offers {offered}"
        find = card.locator(".plot-series-filter")
        await find.fill("1400")
        assert await card.evaluate(keys) == ["1400:value@10", "1400:value@20"], "Filtered by 1400, the list shows others"
        for key in ("1400:value@10", "1400:value@20"):
            await card.locator(f'.plot-series-list input[data-key="{key}"]').check()
        await card.locator('.plot-series-list input[data-key="1400:value@10"]').uncheck()
        await page.wait_for_timeout(300)
        legend = await card.evaluate(COMPARE_LEGEND)
        assert legend == ["S1400 · value · n20"], f"Ticked on, then n10 off, the legend reads {legend}"
        await card.locator('.plot-legend-item[data-series="S1400 · value · n20"] .plot-legend-remove').click()
        await page.wait_for_timeout(300)
        assert await card.evaluate(ticked) == [], "Removed from the legend, the series stays ticked in the list"
        await find.fill("current n10")  # words in any order, of the name, the node or the type
        assert await card.evaluate(keys) == ["1700:current@10"], "Two words do not find the series"
        await find.fill("no such thing")
        matched = await card.locator(".plot-series-list").inner_text()
        assert "No series match" in matched, f"Nothing matching, the list says {matched!r}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: with no graph, the tab says how to start, and starts one")
async def _(page):
    await page.evaluate(COMPARE_START)
    empty = page.locator(".compare-empty")
    try:
        await page.wait_for_timeout(300)
        assert await page.evaluate("state.compareGraphs.length") == 0, "The test needs no graph to start from"
        assert await empty.is_visible(), "With no graph, the tab is blank"
        words = await empty.inner_text()
        assert "No graphs yet" in words and "Compare button" in words, f"The empty tab says {words!r}"
        await empty.locator("button", has_text="Add a graph").click()
        await page.wait_for_timeout(300)
        assert await page.evaluate("state.compareGraphs.length") == 1, "Add a graph adds none"
        assert not await empty.is_visible(), "With a graph, the empty tab's words stay"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: its everyday buttons are plain, as in the Graph tab: colour is for the unusual")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        await compare_graph(page, (1100, "value"))
        coloured = await page.evaluate("""() => {
            const probe = document.createElement('span');
            probe.style.color = 'var(--accent)';
            document.body.append(probe);
            const accent = getComputedStyle(probe).color;
            probe.remove();
            return [...document.querySelectorAll('.compare-toolbar button, .compare-graph-save, .compare-card-editor .plot-compare-add')]
                .filter(b => getComputedStyle(b).color === accent).map(b => b.textContent.trim()); }""")
        assert not coloured, f"These everyday buttons are in the accent colour: {coloured}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a strip counts the graphs, and picks out quiet series and paused graphs")
async def _(page):
    await page.evaluate(COMPARE_START)
    strip = page.locator(".compare-status")
    says = "() => document.querySelector('.compare-status')?.innerText.replace(/\\s+/g, ' ') ?? ''"
    try:
        await page.wait_for_timeout(300)
        for _ in range(3):
            await compare_graph(page, (1100, "value"))
        last = await compare_graph(page, (1400, "value"))  # the fourth, out of view at the top
        await page.wait_for_timeout(300)
        text = await page.evaluate(says)
        assert "4 graphs · 4 series" in text and "nothing unusual" in text, f"The strip says {text!r}"
        await page.evaluate("e2eCompare.mute1400 = true")
        try:
            await page.wait_for_function(f"({says})().includes('1 series quiet')", timeout=5000)
        except Exception:
            raise AssertionError(f"1400 gone quiet, the strip says {await page.evaluate(says)!r}") from None
        await page.locator(".compare-cards").evaluate("(c) => { c.scrollTop = 0; }")
        await strip.locator('[data-focus="quiet"]').click()
        await page.wait_for_timeout(300)
        shown = await last.evaluate("""(c) => { const r = c.getBoundingClientRect(), v = c.parentElement.getBoundingClientRect();
            return r.top >= v.top - 1 && r.bottom <= v.bottom + 1; }""")
        assert shown, "The quiet count does not bring its graph into view"
        await page.locator(".compare-pause-all").click()
        await page.wait_for_function(f"({says})().includes('4 paused')", timeout=2000)
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a window longer than the history keeps says so")
async def _(page):
    await page.evaluate(COMPARE_START)
    kept = "(c) => { const k = c.querySelector('.compare-kept'); return k && !k.classList.contains('hidden') ? k.textContent : ''; }"
    beyond = "(c) => [...c.querySelectorAll('.plot-window-btn--beyond')].map(b => b.textContent)"
    try:
        # 1500 at 100 Hz: its history is full, 3600 points over 36 s.
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('1500:value', Array.from({length: 3600}, (_, i) => ({t: now - 36 + i / 100, v: 1, n: 10}))); }""")
        card = await compare_graph(page, (1500, "value"))
        await page.wait_for_timeout(400)
        assert await card.evaluate(kept) == "kept 36 s", f"A 1m window over 36 s of history reads {await card.evaluate(kept)!r}"
        assert await card.evaluate(beyond) == ["1m", "5m", "15m"], f"Windows marked as not filling: {await card.evaluate(beyond)}"
        await card.locator('.plot-window-btn[data-secs="30"]').click()
        await page.wait_for_timeout(300)
        assert await card.evaluate(kept) == "", "A 30 s window, which the history fills, still says what is kept"
        # 1100 has been heard for a moment only: its history is not full, nothing is said.
        young = await compare_graph(page, (1100, "value"))
        await page.wait_for_timeout(400)
        assert await young.evaluate(kept) == "" and await young.evaluate(beyond) == [], "A history still filling is called short"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a plot carries its graph's name, not the word Compare")
async def _(page):
    await page.evaluate(COMPARE_START)
    label = "(c) => c.querySelector('.compare-panel-label')?.textContent ?? ''"
    named = "(c) => c.querySelector('.detail-plot-area svg').getAttribute('aria-label')"
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        await page.wait_for_timeout(300)
        assert await card.evaluate(label) == "", f"An untitled graph's plot reads {await card.evaluate(label)!r}"
        await card.locator(".compare-graph-name").fill("Motor A")
        await page.wait_for_timeout(300)
        assert await card.evaluate(label) == "Motor A", f"Named Motor A, its plot reads {await card.evaluate(label)!r}"
        aria = await card.evaluate(named)
        assert "Motor A" in aria and "S1100 · value · n10" in aria, f"The plot is announced as {aria!r}"
    finally:
        await page.evaluate(COMPARE_STOP)


# A graph's legend rows: series -> [label, last, min, max].
COMPARE_LEGEND_ROWS = """(c) => Object.fromEntries([...c.querySelectorAll('.plot-legend-item')].map(r =>
    [r.dataset.series, [r.querySelector('.plot-legend-label').textContent,
     ...[...r.querySelectorAll('.plot-legend-value')].map(v => v.textContent)]]))"""


@test("Compare: the legend lists each series' last, lowest and highest value in view, lined up")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        # 1500 read 1 to 10 over the last 10 s, and 99 an hour ago; 1600's value is in volts.
        await page.evaluate("""() => { const now = Date.now() / 1000;
            state.subjectHistory.set('1500:value', [{t: now - 3600, v: 99, n: 10},
                ...Array.from({length: 10}, (_, i) => ({t: now - 10 + i, v: i + 1, n: 10}))]);
            for (const t of [now - 1, now]) cacheEvent({subject_id: 1600, publisher_node_id: 10, message_type: 'Scalar_1_0',
                rate: 1, subject_rate: 1, payload_bytes: 4, attributes: [{attribute: 'value', value: 12, unit: 'volt'}],
                timestamp_unix: t}); }""")
        await page.wait_for_timeout(300)  # 1400 heard
        card = await compare_graph(page, (1500, "value"), (1400, "value"), (1600, "value"))
        await card.locator('.plot-window-btn[data-secs="30"]').click()
        await page.wait_for_timeout(400)
        rows = await card.evaluate(COMPARE_LEGEND_ROWS)
        assert rows.get("S1500 · value · n10", [])[1:] == ["10", "1", "10"], f"S1500 in view: {rows.get('S1500 · value · n10')}"
        assert rows.get("S1400 · value · n10", [])[1:] == ["10", "10", "10"], f"S1400 in view: {rows.get('S1400 · value · n10')}"
        assert "volt" in rows.get("S1600 · value · n10", [""])[0], f"S1600's row names no unit: {rows.get('S1600 · value · n10')}"
        lefts = await card.evaluate("""(c) => [...c.querySelectorAll('.plot-legend-item')]
            .map(r => Math.round(r.querySelector('.plot-legend-value').getBoundingClientRect().left))""")
        assert len(set(lefts)) == 1, f"The values do not line up: their columns start at {lefts}"
        # 1400 goes quiet, its history cleared: its row stays, with no values.
        await page.evaluate("e2eCompare.mute1400 = true; state.subjectHistory.delete('1400:value')")
        await page.wait_for_timeout(1300)
        quiet = (await card.evaluate(COMPARE_LEGEND_ROWS)).get("S1400 · value · n10", [])[1:]
        assert quiet == ["–", "–", "–"], f"Gone quiet, S1400 reads {quiet}"
    finally:
        await page.evaluate(COMPARE_STOP)


# Parts of a graph shown, hidden or placed by an inline style, as "tag.class: style".
COMPARE_INLINE_STYLES = """(c) => [...c.querySelectorAll('[style]')]
    .filter(e => /display|cursor|position/.test(e.getAttribute('style')))
    .map(e => `${e.tagName.toLowerCase()}.${e.getAttribute('class')}: ${e.getAttribute('style')}`)"""


@test("Compare: a graph's parts show and hide by class, not inline style, and a dragged plot shows a grabbing hand")
async def _(page):
    await page.evaluate(COMPARE_START)
    # Which of the derived row's Source B and Window boxes show.
    derived_shown = """(c) => ['Source B', 'Window size (samples)'].map(l =>
        c.querySelector(`.plot-derived-section [aria-label="${l}"]`).getClientRects().length > 0)"""
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        assert await card.evaluate(derived_shown) == [True, False], "Delta (A−B) should ask for Source B, not a window"
        await card.locator(".plot-derived-type").select_option("rolling_avg")
        assert await card.evaluate(derived_shown) == [False, True], "Rolling Avg should ask for a window, not Source B"
        plot = card.locator(".detail-plot-area svg")
        box = await plot.bounding_box()
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        await page.mouse.move(x, y)  # the tooltip shows
        await page.mouse.down()
        await page.mouse.move(x + 60, y, steps=4)
        cursor = await card.evaluate("(c) => getComputedStyle(c.querySelector('.plot-overlay')).cursor")
        await page.mouse.up()
        await page.mouse.move(box["x"] + box["width"] / 2, box["y"] - 200)  # out: the tooltip hides
        assert cursor == "grabbing", f"Dragged, the plot shows the {cursor!r} cursor"
        await card.locator(".plot-legend-swatch").first.click()  # its colour picker opens
        inline = await card.evaluate(COMPARE_INLINE_STYLES)
        assert not inline, f"Shown, hidden or placed by inline styles: {inline}"
    finally:
        await page.evaluate(COMPARE_STOP)


# The colours a graph's threshold (line and legend pill), drawing and marker
# are in, and the theme's red and accent, all as computed ("rgb(...)").
COMPARE_MARK_COLOURS = """(c) => {
    const drawn = (sel, prop) => { const e = c.querySelector(sel); return e ? getComputedStyle(e)[prop] : null; };
    const token = (name) => { const probe = document.createElement('i');
        probe.style.color = `var(${name})`; document.body.appendChild(probe);
        const rgb = getComputedStyle(probe).color; probe.remove(); return rgb; };
    return {threshold: drawn('.plot-threshold line', 'stroke'),
            pill: drawn('.plot-legend-item[data-threshold-idx] .plot-legend-swatch', 'backgroundColor'),
            drawing: drawn('.plot-drawings path', 'stroke'), marker: drawn('.plot-marker line', 'stroke'),
            red: token('--error'), accent: token('--accent')}; }"""


@test("Compare: thresholds, drawings and markers left at their default colour take the theme's")
async def _(page):
    theme = "document.documentElement.getAttribute('data-theme') || 'light'"
    started_in = await page.evaluate(theme)
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        await card.locator('[aria-label="Threshold value"]').fill("82")
        await card.locator('[aria-label="Add threshold line"]').click()
        box = await card.locator(".plot-overlay").bounding_box()
        x, y = box["x"] + box["width"] * 0.5, box["y"] + box["height"] / 2
        await page.keyboard.down("Alt")  # Alt+drag draws
        await page.mouse.move(x, y)
        await page.mouse.down()
        await page.mouse.move(x + 80, y + 20, steps=4)
        await page.mouse.up()
        await page.keyboard.up("Alt")
        await page.keyboard.down("Shift")  # Shift+click marks
        await page.mouse.click(x + 40, box["y"] + 40)
        await page.keyboard.up("Shift")
        await page.keyboard.type("start")
        await page.keyboard.press("Enter")
        for _ in range(2):  # in this theme, then the other
            await page.wait_for_timeout(400)
            c, now_in = await card.evaluate(COMPARE_MARK_COLOURS), await page.evaluate(theme)
            assert c["threshold"] == c["pill"] == c["red"], \
                f"In {now_in}, a threshold is {c['threshold']} (pill {c['pill']}), not the theme's red {c['red']}"
            assert c["drawing"] == c["red"], f"In {now_in}, a drawing is {c['drawing']}, not the theme's red {c['red']}"
            assert c["marker"] == c["accent"], f"In {now_in}, a marker is {c['marker']}, not the theme's accent {c['accent']}"
            await page.locator("#themeToggle").click()
    finally:
        if await page.evaluate(theme) != started_in:
            await page.locator("#themeToggle").click()
        await page.evaluate(COMPARE_STOP)


# Each graph: is its line clipped by a clip path of its own, as tall as its plot?
COMPARE_OWN_CLIPS = """() => [...document.querySelectorAll('.compare-graph-card')].map(card => {
    const id = card.querySelector('.compare-lines')?.getAttribute('clip-path')?.match(/#([^)]+)/)?.[1];
    const clip = id && document.getElementById(id);
    return !!clip && card.contains(clip)
        && clip.querySelector('rect').getAttribute('height') === card.querySelector('.plot-overlay').getAttribute('height'); })"""


@test("Compare: each graph clips its lines to its own plot, not to the first graph's")
async def _(page):
    await page.evaluate(COMPARE_START)
    try:
        await page.wait_for_timeout(300)
        await compare_graph(page, (1100, "value"))  # its editing rows open: a short plot
        second = await compare_graph(page, (1100, "value"))
        await second.locator(".compare-edit-btn").click()  # folded away: a taller plot
        await second.scroll_into_view_if_needed()
        await page.wait_for_timeout(400)
        own = await page.evaluate(COMPARE_OWN_CLIPS)
        assert own == [True, True], f"Graphs clipped by a clip path of their own, as tall as their plot: {own}"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: drawings and markers scrolled past the y-axis are cut there, not drawn over it")
async def _(page):
    await page.evaluate(COMPARE_START)
    # Are these parts of a graph clipped by its plot's own clip path?
    clipped = """(c, sels) => sels.map(sel => {
        const id = c.querySelector(sel)?.getAttribute('clip-path')?.match(/#([^)]+)/)?.[1];
        const clip = id && document.getElementById(id);
        return !!clip && c.contains(clip); })"""
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        # A drawing and a marker made 70 s ago: in a 1m window, partly past the y-axis.
        await page.evaluate("""() => { const g = state.compareGraphs[0], now = Date.now() / 1000;
            g.drawings.push({points: [{t: now - 70, y: 0.5}, {t: now - 20, y: 0.6}], color: '', width: 2, dash: 'solid'});
            g.markers.push({t: now - 65, label: 'gone', note: '', color: '', lineStyle: 'dashed'});
            g._fingerprint = ''; _renderOneGraph(g); }""")
        cut = await card.evaluate(clipped, [".plot-drawings", ".plot-markers"])
        assert cut == [True, True], f"Clipped to the plot, the drawings and markers: {cut}"
        # A drawing dragged past the y-axis is cut there while it is drawn, too.
        box = await card.locator(".plot-overlay").bounding_box()
        y = box["y"] + box["height"] / 2
        await page.keyboard.down("Alt")
        await page.mouse.move(box["x"] + 60, y)
        await page.mouse.down()
        await page.mouse.move(box["x"] - 30, y, steps=4)
        drawing = await card.evaluate(clipped, [".plot-drawing-temp"])
        await page.mouse.up()
        await page.keyboard.up("Alt")
        assert drawing == [True], "A drawing dragged past the y-axis is drawn over it"
    finally:
        await page.evaluate(COMPARE_STOP)


@test("Compare: a series' and a derived series' colours are picked from the legend")
async def _(page):
    await page.evaluate(COMPARE_START)
    # Picks a colour in a legend row's colour box, as the browser's picker would.
    pick = "(i, hex) => { i.value = hex; i.dispatchEvent(new Event('change')); }"
    strokes = "(c) => [...c.querySelectorAll('.compare-line')].map(l => l.getAttribute('stroke'))"
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        await card.locator(".plot-derived-type").select_option("rate")
        await card.locator('[aria-label="Source A"]').select_option("1100:value@10")
        await card.locator('[aria-label="Add derived series"]').click()
        await page.wait_for_timeout(1200)  # two points of the rate at least
        for row, hex in (('[data-derived-id]', '#123456'), (':not([data-derived-id])', '#654321')):
            await card.locator(f".plot-legend-item{row} .plot-legend-swatch").click()
            await card.locator(f".plot-legend-item{row} .plot-legend-swatch input").evaluate(pick, hex)
        await page.wait_for_timeout(300)
        colours = await page.evaluate("[state.compareGraphs[0].series[0].color, state.compareGraphs[0].derivedSeries[0].color]")
        assert colours == ["#654321", "#123456"], f"The series and derived series are coloured {colours}"
        assert sorted(await card.evaluate(strokes)) == ["#123456", "#654321"], f"The lines are {await card.evaluate(strokes)}"
    finally:
        await page.evaluate(COMPARE_STOP)


# A graph's Markers row: its markers as listed, its drawings' count, its hint.
COMPARE_MARKS = """(c) => ({marks: [...c.querySelectorAll('.compare-mark-go')].map(b => b.textContent),
    drawings: c.querySelector('.compare-marks-drawings')?.textContent ?? '',
    hint: c.querySelector('.compare-marks-hint')?.textContent ?? ''})"""


@test("Compare: a graph lists its markers and drawings, shows a marker gone off screen, and removes them")
async def _(page):
    await page.evaluate(COMPARE_START)
    asked = []

    async def on_dialog(dialog):
        asked.append(dialog.message)
        await dialog.accept()

    page.on("dialog", on_dialog)
    try:
        await page.wait_for_timeout(300)
        card = await compare_graph(page, (1100, "value"))
        await page.wait_for_timeout(300)
        row = await card.evaluate(COMPARE_MARKS)
        assert row["marks"] == [] and "Shift+click" in row["hint"] and "Alt+drag" in row["hint"], \
            f"With nothing marked, the Markers row reads {row}"
        # A marker kept from ten minutes ago: off screen in a 1m window.
        await page.evaluate("""() => { const g = state.compareGraphs[0];
            g.markers.push({t: Date.now() / 1000 - 600, label: 'start', note: '', color: '', lineStyle: 'dashed'});
            g._fingerprint = ''; _renderOneGraph(g); }""")
        row = await card.evaluate(COMPARE_MARKS)
        assert len(row["marks"]) == 1 and row["marks"][0].endswith("start"), f"The Markers row lists {row['marks']}"
        await card.locator(".compare-mark-go").click()
        await page.wait_for_timeout(300)
        assert await page.evaluate("state.compareGraphs[0].paused"), "Shown from the list, the graph does not hold still"
        where = await card.evaluate("""(c) => Number(c.querySelector('.plot-marker line')?.getAttribute('x1'))
            / Number(c.querySelector('.plot-overlay').getAttribute('width'))""")
        assert 0.4 < where < 0.6, f"Shown from the list, the marker is at {where:.2f} of the plot's width, not its middle"
        await card.locator(".compare-mark-delete").click()
        assert await page.evaluate("state.compareGraphs[0].markers.length") == 0, "× leaves the marker in the graph"
        # A drawing made on the plot is counted, and cleared from the row once asked.
        box = await card.locator(".plot-overlay").bounding_box()
        x, y = box["x"] + box["width"] * 0.5, box["y"] + box["height"] / 2
        await page.keyboard.down("Alt")
        await page.mouse.move(x, y)
        await page.mouse.down()
        await page.mouse.move(x + 60, y + 20, steps=4)
        await page.mouse.up()
        await page.keyboard.up("Alt")
        await page.wait_for_timeout(300)
        row = await card.evaluate(COMPARE_MARKS)
        assert row["drawings"] == "1 drawing", f"After a drawing, the Markers row reads {row}"
        await card.locator(".compare-marks-clear").click()
        assert asked and "1 drawing" in asked[-1], f"Clear asks {asked}"
        assert await page.evaluate("state.compareGraphs[0].drawings.length") == 0, "Clear leaves the drawing"
    finally:
        page.remove_listener("dialog", on_dialog)
        await page.evaluate(COMPARE_STOP)


# ── DSDL tab ──
#
# /api/dsdl/* is answered by _DsdlServer from a few types shaped as the
# backend sends them; saving, deleting and compiling change them as the
# backend would. Each test opens the tab on a freshly loaded page, so what
# the tab remembers (selection, editor, search) starts empty.

def _dsdl_type(full_name, port=None, source="regulated", compiled=True, text="uint8 value\n@sealed\n"):
    *namespace, short_name, major, minor = full_name.split(".")
    return {"full_name": full_name, "namespace": ".".join(namespace), "short_name": short_name,
            "version": f"{major}.{minor}", "kind": "service" if "\n---\n" in text else "message",
            "fixed_port_id": port, "source": source, "compiled": compiled, "source_text": text}


# Two types share the class name Scalar_1_0, as 46 public ones do.
DSDL_TYPES = [
    _dsdl_type("uavcan.node.Heartbeat.1.0", port=7509, text="uint32 uptime\n@sealed\n"),
    _dsdl_type("uavcan.si.unit.temperature.Scalar.1.0", text="float32 kelvin\n@sealed\n"),
    _dsdl_type("uavcan.si.unit.voltage.Scalar.1.0", text="float32 volt\n@sealed\n"),
    _dsdl_type("myapp.Reading.1.0", source="custom", text="uint16 value\n@sealed\n"),
]


class _DsdlServer:
    """The backend's /api/dsdl/* routes, over DSDL_TYPES."""

    def __init__(self):
        self.types = {t["full_name"]: dict(t) for t in DSDL_TYPES}
        self.custom_namespaces = {"myapp"}
        self.last_custom_compiled = 1.0e9
        self.requests = []          # (method, path, JSON body)
        self.gates = {}             # path -> an asyncio.Event its answer waits for
        self.compile_answer = ({"ok": True}, 200)

    def _status(self):
        custom = sum(t["source"] == "custom" for t in self.types.values())
        return {"paths": [], "public_compilable": True, "compiled": True, "last_compiled": self.last_custom_compiled,
                "last_public_compiled": 1.0e9, "last_custom_compiled": self.last_custom_compiled,
                "source_types": len(self.types) - custom, "custom_types": custom}

    def _tree(self):
        tree = {}
        for t in self.types.values():
            root, *rest = t["namespace"].split(".")
            node = tree.setdefault(root, {"children": {}, "types": []})
            for part in rest:
                node = node["children"].setdefault(part, {"children": {}, "types": []})
            # The names search looks in: "type name" is a field, "type NAME = value" a constant.
            lines = [line.split("#")[0].split() for line in t["source_text"].split("\n")]
            named = [words for words in lines if len(words) >= 2 and words[0][0] != "@"]
            node["types"].append({key: t[key] for key in ("short_name", "full_name", "version", "kind",
                                                          "fixed_port_id", "source", "compiled")}
                                 | {"field_names": [w[1] for w in named if "=" not in w],
                                    "constant_names": [w[1] for w in named if "=" in w]})
        for namespace in sorted(self.custom_namespaces):  # empty ones too
            root, *rest = namespace.split(".")
            node = tree.setdefault(root, {"children": {}, "types": []})
            node["_source"] = "custom"
            for part in rest:
                node = node["children"].setdefault(part, {"children": {}, "types": []})
                node["_source"] = "custom"
        return {"namespaces": tree}

    @staticmethod
    def _detail(t):
        sections = [[]]
        for words in (line.split() for line in t["source_text"].split("\n")):
            if words == ["---"]:
                sections.append([])
            elif len(words) == 2 and words[0][0] not in "@#":
                sections[-1].append({"type": words[0], "name": words[1]})
        fields = sections[0] if len(sections) == 1 else {"request": sections[0], "response": sections[1]}
        composite = sorted({f["type"].split("[")[0] for section in sections for f in section if "." in f["type"]})
        # A test can give a type's whole detail: its fields' comments, say.
        return {**t, "source_file": "", "fields": fields, "constants": [], "dependencies": composite,
                **t.get("detail", {})}

    def _answer(self, method, path, body):
        if path == "/api/dsdl/status":
            return self._status(), 200
        if path == "/api/dsdl/namespaces":
            return self._tree(), 200
        if path == "/api/dsdl/custom/namespaces":
            return {"namespaces": sorted(self.custom_namespaces)}, 200
        if method == "GET" and path.startswith("/api/dsdl/type/"):
            t = self.types.get(path.removeprefix("/api/dsdl/type/"))
            return (self._detail(t), 200) if t else ({"error": "Type not found"}, 404)
        if method == "POST" and path == "/api/dsdl/custom/namespace":
            if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*", body["namespace"]):
                return {"error": "Namespace must be lowercase dotted identifiers (e.g. myapp.sensors)"}, 400
            self.custom_namespaces.add(body["namespace"])
            return {"namespace": body["namespace"], "path": ""}, 201
        if method == "POST" and path == "/api/dsdl/custom/type":
            name = f"{body['namespace']}.{body['type_name']}.{body['version']}"
            old = self.types.get(name)
            if old and not body.get("overwrite"):
                return {"error": f"Type '{name}' already exists"}, 400
            if old and old["compiled"]:
                return {"error": f"Cannot edit '{name}': type is already compiled."}, 409
            self.types[name] = _dsdl_type(name, port=body.get("fixed_port_id"), source="custom", compiled=False,
                                          text=body["source_text"])
            self.custom_namespaces.add(body["namespace"])  # created with its first type, as the server does
            return {"full_name": name, "path": ""}, 201
        if method == "DELETE" and path.startswith("/api/dsdl/custom/namespace/"):
            name = path.removeprefix("/api/dsdl/custom/namespace/")
            inside = lambda ns: ns == name or ns.startswith(f"{name}.")
            if any(inside(t["namespace"]) for t in self.types.values()):
                return {"error": f"Namespace '{name}' still has types: delete them first"}, 400
            if name not in self.custom_namespaces:
                return {"error": f"Namespace not found: {name}"}, 404
            self.custom_namespaces = {ns for ns in self.custom_namespaces if not inside(ns)}
            return {"namespace": name, "deleted": True}, 200
        if method == "DELETE" and path.startswith("/api/dsdl/custom/type/"):
            name = path.removeprefix("/api/dsdl/custom/type/")
            if self.types.pop(name, None) is None:
                return {"error": f"Type not found: {name}"}, 404
            return {"full_name": name, "deleted": True}, 200
        return {"error": "not found"}, 404

    async def handle(self, route):
        request = route.request
        path = unquote(urlparse(request.url).path)
        body = request.post_data_json if request.post_data else None
        self.requests.append((request.method, path, body))
        if path in self.gates:
            await self.gates[path].wait()
        if (request.method, path) == ("POST", "/api/dsdl/compile"):
            answer, status = self.compile_answer
        else:
            answer, status = self._answer(request.method, path, body)
        await route.fulfill(json=answer, status=status, headers={"Access-Control-Allow-Origin": "*"})


async def dsdl_open(page, server, setup=None, connected=True, wait=True):
    """The DSDL tab on a freshly loaded page, connected to `server` or not.
    `setup` runs in the page before the tab opens (nodes, messages); `wait`
    waits for its tree."""
    await page.evaluate("""() => { state.dashboardConnected = false; _writeSettingsNow();
        localStorage.removeItem('cynitor.dsdl.state'); }""")
    await page.reload(wait_until="load")
    await page.route("**/api/dsdl/**", server.handle)
    await page.evaluate(f"state.dashboardConnected = {json.dumps(connected)}")
    if setup:
        await page.evaluate(setup)
    await page.evaluate("switchView('dsdl')")
    if wait:
        await page.wait_for_selector("#dsdlTree .dsdl-ns-row" if connected else "#dsdlTree .dsdl-tree-empty",
                                     timeout=5000)


async def dsdl_close(page, server):
    await page.evaluate("""() => { switchView('nodes'); state.dashboardConnected = false;
        state.latestNodesPayload = null; state.latestBySubject.clear(); state.latestByNode.clear();
        localStorage.removeItem('cynitor.dsdl.state'); }""")
    await page.unroute("**/api/dsdl/**", server.handle)


@test("DSDL: a subject's bus dot goes on its own type, not on another with its class name")
async def _(page):
    server = _DsdlServer()
    # Subject 1100 is temperature by its publisher's registers; its messages
    # name only their class, Scalar_1_0, which voltage has too.
    nodes = {"node_count": 1, "nodes": {"10": _graph_node(10, "org.example.thermo", [1100], [], uid_byte=1)},
             "subject_types": {"1100": {"type": "uavcan.si.unit.temperature.Scalar_1_0", "set_by": "registers"}}}
    setup = f"""() => {{
        state.latestNodesPayload = {json.dumps(nodes)};
        cacheEvent({{subject_id: 1100, publisher_node_id: 10, message_type: 'Scalar_1_0', rate: 10,
                    subject_rate: 10, payload_bytes: 4, attributes: [], timestamp_unix: Date.now() / 1000}});
    }}"""
    await dsdl_open(page, server, setup)
    try:
        await page.wait_for_selector(".dsdl-type-row .dsdl-bus-dot", state="attached", timeout=WAIT_MS)
        dotted = await page.evaluate("""[...document.querySelectorAll('.dsdl-type-row')]
            .filter(row => row.querySelector('.dsdl-bus-dot')).map(row => row.dataset.type)""")
        assert dotted == ["uavcan.si.unit.temperature.Scalar.1.0"], f"Marked active on the bus: {dotted}"
    finally:
        await dsdl_close(page, server)


async def dsdl_new_type(page, namespace="myapp"):
    """The editor for a new type, opened from its namespace's own button."""
    await page.locator(f'#dsdlCustomTree .dsdl-ns-row[data-ns="{namespace}"]').hover()
    await page.locator(f'[data-add-type="{namespace}"]').click()
    await page.wait_for_selector("#dsdlEditorName")


async def dsdl_save(page):
    """Save, and what the editor says once the server has answered."""
    async with page.expect_response("**/api/dsdl/custom/type"):
        await page.locator("#dsdlEditorSave").click()
    await page.wait_for_function("document.getElementById('dsdlEditorStatus').textContent !== 'Saving…'",
                                 timeout=WAIT_MS)
    return await page.locator("#dsdlEditorStatus").inner_text()


@test("DSDL: a type just created saves again, as an edit")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        await dsdl_new_type(page)
        await page.locator("#dsdlEditorName").fill("Draft")
        await page.locator("#dsdlEditorSource").fill("uint8 a\n@sealed\n")
        first = await dsdl_save(page)
        await page.locator("#dsdlEditorSource").fill("uint8 a\nuint8 b\n@sealed\n")
        second = await dsdl_save(page)
        assert (first, second) == ("Saved", "Saved"), f"Saved, then saved again: {first!r}, {second!r}"
        kept = server.types["myapp.Draft.1.0"]["source_text"]
        assert kept == "uint8 a\nuint8 b\n@sealed\n", f"The server keeps {kept!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: opened before connecting, the tab stops saying 'Not connected.' once connected")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server, connected=False)
    try:
        before = await page.locator("#dsdlDetail").inner_text()
        # What connectDashboard does with the tab open.
        await page.evaluate("state.dashboardConnected = true; DsdlView.init()")
        await page.wait_for_selector("#dsdlTree .dsdl-ns-row", timeout=5000)
        after = await page.locator("#dsdlDetail").inner_text()
        assert "Not connected" in before and "Select a type to inspect" in after, \
            f"Before connecting the pane said {before!r}; after, {after!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a compiled custom type can be deleted, and the tab says how to change it")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        row = page.locator('#dsdlCustomTree .dsdl-type-row[data-type="myapp.Reading.1.0"]')
        hint = await row.get_attribute("title")
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await row.click()
        await page.wait_for_selector('.dsdl-doc-title:has-text("Reading")', timeout=WAIT_MS)
        assert await page.locator("#dsdlDeleteBtn").count() == 1, "A compiled custom type cannot be deleted"
        assert await page.locator("#dsdlEditBtn").count() == 0, "A compiled custom type offers Edit"
        pane = await page.locator(".dsdl-doc-head").inner_text()
        assert "python_compiled_messages" not in hint and "new version" in hint and "new version" in pane, \
            f"The row says {hint!r}; the pane, {pane!r}"
        await page.locator("#dsdlDeleteBtn").click()
        await page.locator("#dsdlDelOk").click()
        await page.wait_for_selector('.dsdl-type-row[data-type="myapp.Reading.1.0"]', state="detached", timeout=WAIT_MS)
        assert "myapp.Reading.1.0" not in server.types, "The server still has the type"
    finally:
        await dsdl_close(page, server)


# The theme's --error, as the browser draws it.
ERROR_COLOUR = """(() => { const probe = document.createElement('span'); probe.style.color = 'var(--error)';
    document.body.append(probe); const colour = getComputedStyle(probe).color; probe.remove(); return colour; })()"""


@test("DSDL: compile and editor errors are drawn in the error colour")
async def _(page):
    server = _DsdlServer()
    server.compile_answer = ({"error": "myapp.Reading.1.0: Reading: Either `@sealed` or `@extent ...` are required"}, 422)
    await dsdl_open(page, server)
    try:
        error = await page.evaluate(ERROR_COLOUR)
        await page.locator("#dsdlCustomCompileBtn").click()
        await page.wait_for_selector(".dsdl-compile-error", timeout=WAIT_MS)
        compile_error = await page.evaluate("getComputedStyle(document.querySelector('.dsdl-compile-error')).color")
        await dsdl_new_type(page)
        await page.locator("#dsdlEditorSave").click()  # nothing filled in
        await page.wait_for_selector("#dsdlEditorStatus.dsdl-editor-error", timeout=WAIT_MS)
        editor_error = await page.evaluate("getComputedStyle(document.getElementById('dsdlEditorStatus')).color")
        assert compile_error == editor_error == error, \
            f"The error colour is {error}; a compile error is drawn {compile_error}, an editor error {editor_error}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: the editor's example is a type that compiles")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        await dsdl_new_type(page)
        example = await page.locator("#dsdlEditorSource").get_attribute("placeholder")
        # The compiler refuses a type that is neither @sealed nor has an @extent.
        assert "@sealed" in example or "@extent" in example, f"The example: {example!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a fixed port ID the compiler would refuse is not saved, and the editor says which it takes")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        await dsdl_new_type(page)
        await page.locator("#dsdlEditorName").fill("Foo")
        await page.locator("#dsdlEditorSource").fill("uint8 a\n@sealed\n")
        said = {}
        for port in ("abc", "-5", "100"):
            await page.locator("#dsdlEditorPort").fill(port)
            await page.locator("#dsdlEditorSave").click()
            await page.wait_for_timeout(300)
            said[port] = await page.locator("#dsdlEditorStatus").inner_text()
        sent = [request for request in server.requests if request[0] == "POST"]
        assert not sent and all("6144" in text for text in said.values()), f"Sent {sent}; the editor said {said}"
        await page.locator("#dsdlEditorPort").fill("7000")
        assert await dsdl_save(page) == "Saved", "A port in range is refused"
        assert server.types["myapp.Foo.1.0"]["fixed_port_id"] == 7000, "Saved without its port"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a compile keeps its button busy until it ends, and is not asked for twice")
async def _(page):
    server = _DsdlServer()
    compiled = server.gates["/api/dsdl/compile"] = asyncio.Event()

    def asked(path):
        return sum(request[1] == path for request in server.requests)

    await dsdl_open(page, server)
    try:
        await page.locator("#dsdlCustomCompileBtn").click()
        # A compile writes files as it goes: the status polled every 4 s
        # changes, and the tab redraws its headers mid-compile.
        server.last_custom_compiled += 1
        reloads = asked("/api/dsdl/namespaces")
        for _ in range(40):
            if asked("/api/dsdl/namespaces") > reloads:
                break
            await page.wait_for_timeout(200)
        await page.wait_for_timeout(300)
        button = await page.evaluate("""(() => { const b = document.getElementById('dsdlCustomCompileBtn');
            return {busy: b.classList.contains('dsdl-spin'), disabled: b.disabled}; })()""")
        await page.evaluate("document.getElementById('dsdlCustomCompileBtn').click()")
        await page.wait_for_timeout(300)
        assert button == {"busy": True, "disabled": True} and asked("/api/dsdl/compile") == 1, \
            f"Redrawn mid-compile, the button is {button}; compiles asked for: {asked('/api/dsdl/compile')}"
        compiled.set()
        await page.wait_for_function("!document.getElementById('dsdlCustomCompileBtn').disabled", timeout=WAIT_MS)
    finally:
        compiled.set()
        await dsdl_close(page, server)


@test("DSDL: the namespace Create and delete-confirm buttons keep their colours under the pointer")
async def _(page):
    server = _DsdlServer()
    background = "(id) => getComputedStyle(document.getElementById(id)).backgroundColor"

    async def resting_and_hovered(button_id):
        resting = await page.evaluate(background, button_id)
        await page.locator(f"#{button_id}").hover()
        await page.wait_for_timeout(400)  # past the hover transition
        return resting, await page.evaluate(background, button_id)

    await dsdl_open(page, server)
    try:
        await page.locator("#dsdlCustomAddNs").click()
        create = await resting_and_hovered("dsdlNsOk")
        await page.locator("#dsdlNsCancel").click()
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('.dsdl-type-row[data-type="myapp.Reading.1.0"]').click()
        await page.locator("#dsdlDeleteBtn").click()
        delete = await resting_and_hovered("dsdlDelOk")
        assert create[0] == create[1] and delete[0] == delete[1], \
            f"At rest and under the pointer: Create {create}, Delete {delete}"
    finally:
        await dsdl_close(page, server)


# The field names cut off by their card, or with their type's text run into them.
FIELD_NAMES_UNREADABLE = """() => [...document.querySelectorAll('#dsdlDetail .dsdl-fcol-name')].filter((cell) => {
    const card = cell.closest('.dsdl-card').getBoundingClientRect();
    const text = (node) => { const range = document.createRange(); range.selectNodeContents(node);
        return range.getBoundingClientRect(); };
    const name = text(cell), type = text(cell.previousElementSibling);
    return name.right > card.right + 0.5 || type.right > name.left;
}).map((cell) => cell.textContent)"""


@test("DSDL: long field types wrap, and never cut off or run into the field names")
async def _(page):
    server = _DsdlServer()
    long_type = "uavcan.si.unit.electric_current.Scalar.1.0"
    server.types["myapp.Pair.1.0"] = _dsdl_type("myapp.Pair.1.0", source="custom", compiled=False,
                                                text=f"{long_type} a\n{long_type} b\n@sealed\n")
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('.dsdl-type-row[data-type="myapp.Pair.1.0"]').click()
        await page.wait_for_selector('.dsdl-doc-title:has-text("Pair")', timeout=WAIT_MS)
        await dsdl_new_type(page)  # the editor takes half the room
        await page.wait_for_timeout(300)
        width = await page.evaluate("Math.round(document.getElementById('dsdlDetail').getBoundingClientRect().width)")
        unreadable = await page.evaluate(FIELD_NAMES_UNREADABLE)
        assert not unreadable, f"In a {width} px pane these field names are cut off or run into: {unreadable}"
    finally:
        await dsdl_close(page, server)


async def dsdl_show(page, full_name):
    """A public type's detail, opened from the tree."""
    parts = full_name.split(".")
    for depth in range(1, len(parts) - 2):
        row = page.locator(f'#dsdlTree .dsdl-ns-row[data-ns="{".".join(parts[:depth])}"]')
        if await row.get_attribute("aria-expanded") != "true" and not await row.locator(".dsdl-chev.open").count():
            await row.click()
    await page.locator(f'.dsdl-type-row[data-type="{full_name}"]').click()
    await page.wait_for_selector(f'.dsdl-doc-title:has-text("{parts[-3]}")', timeout=WAIT_MS)


@test("DSDL: when the server goes away the tab says so, and comes back with it")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        await dsdl_show(page, "uavcan.node.Heartbeat.1.0")
        await page.evaluate("disconnectAll({persist: false})")
        tree = await page.locator("#dsdlTree").inner_text()
        pane = await page.locator("#dsdlDetail").inner_text()
        assert "Connect to server" in tree and "Not connected" in pane, \
            f"Disconnected, the tree reads {tree!r} and the pane {pane!r}"
        await page.locator("#dsdlSearch").fill("Heartbeat")
        await page.wait_for_timeout(400)  # the search's debounce
        searched = await page.locator("#dsdlTreePanel .dsdl-tree-scroll").inner_text()
        assert "Heartbeat" not in searched, f"Searched while disconnected, the tree shows {searched!r}"
        await page.locator("#dsdlSearch").fill("")
        await page.wait_for_timeout(400)
        # What connectDashboard does with the tab open.
        await page.evaluate("state.dashboardConnected = true; DsdlView.init()")
        await page.wait_for_selector('.dsdl-doc-title:has-text("Heartbeat")', timeout=WAIT_MS)
    finally:
        await dsdl_close(page, server)


@test("DSDL: the tree says it is loading until the types arrive")
async def _(page):
    server = _DsdlServer()
    arrived = server.gates["/api/dsdl/namespaces"] = asyncio.Event()
    await dsdl_open(page, server, wait=False)
    try:
        await page.wait_for_timeout(300)
        loading = await page.locator("#dsdlTree").inner_text()
        arrived.set()
        await page.wait_for_selector("#dsdlTree .dsdl-ns-row", timeout=WAIT_MS)
        assert "Loading" in loading, f"While the types load, the tree reads {loading!r}"
    finally:
        arrived.set()
        await dsdl_close(page, server)


@test("DSDL: the time since the last compile follows the clock")
async def _(page):
    server = _DsdlServer()
    server.last_custom_compiled = time.time() - 120
    await dsdl_open(page, server)
    try:
        status = "#dsdlCustomHeader .dsdl-tree-status"
        before = await page.locator(status).inner_text()
        # Three hours on; the status, polled every 4 s, has not changed.
        await page.evaluate("(() => { const now = Date.now; Date.now = () => now() + 3 * 3600 * 1000; })()")
        await page.wait_for_timeout(4500)
        after = await page.locator(status).inner_text()
        assert "2m ago" in before and "3h ago" in after, \
            f"Compiled two minutes ago, the header said {before!r}; three hours on, {after!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a refused namespace or delete is said where it was asked for")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        await dsdl_new_type(page)  # an editor open: its status line is about the type in it
        await page.locator("#dsdlCustomAddNs").click()
        await page.locator("#dsdlNsInput").fill("My-NS")
        await page.locator("#dsdlNsOk").click()
        await page.wait_for_timeout(300)
        in_dialog = await page.locator("#dsdlNsDialog").inner_text()
        in_editor = await page.locator("#dsdlEditorStatus").inner_text()
        await page.locator("#dsdlNsCancel").click()
        # Gone from the server since it was shown: deleting it is refused.
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('.dsdl-type-row[data-type="myapp.Reading.1.0"]').click()
        await page.wait_for_selector("#dsdlDeleteBtn", timeout=WAIT_MS)
        del server.types["myapp.Reading.1.0"]
        await page.locator("#dsdlDeleteBtn").click()
        await page.locator("#dsdlDelOk").click()
        await page.wait_for_timeout(300)
        bar = page.locator(".dsdl-confirm-bar")
        in_bar = await bar.inner_text() if await bar.count() else "(no confirmation bar)"
        assert "lowercase" in in_dialog and not in_editor and "not found" in in_bar, \
            f"The namespace dialog says {in_dialog!r}, the editor {in_editor!r}; the delete bar {in_bar!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a public recompile leaves the custom types' compile error alone")
async def _(page):
    server = _DsdlServer()
    server.compile_answer = ({"error": "myapp.Reading.1.0: Reading: Either `@sealed` or `@extent ...` are required"}, 422)
    await dsdl_open(page, server)
    try:
        await page.locator("#dsdlCustomCompileBtn").click()
        await page.wait_for_selector("#dsdlCustomHeader + .dsdl-compile-error", timeout=WAIT_MS)
        server.compile_answer = ({"ok": True}, 200)
        async with page.expect_response("**/api/dsdl/compile"):
            await page.locator("#dsdlRecompileBtn").click()
        await page.wait_for_timeout(300)
        left = await page.locator("#dsdlCustomHeader + .dsdl-compile-error").count()
        assert left == 1, "A public recompile wiped the custom types' compile error, though they did not compile"
    finally:
        await dsdl_close(page, server)


@test("DSDL: the editor asks before unsaved changes are dropped")
async def _(page):
    server = _DsdlServer()
    asked, answer = [], {"accept": False}

    async def on_dialog(dialog):
        asked.append(dialog.message)
        await (dialog.accept() if answer["accept"] else dialog.dismiss())

    async def source():
        return await page.locator("#dsdlEditorSource").input_value() if await page.locator("#dsdlEditorSource").count() else None

    page.on("dialog", on_dialog)
    await dsdl_open(page, server)
    try:
        await dsdl_new_type(page)
        await page.locator("#dsdlEditorClose").click()  # nothing written: closed without a word
        assert not asked and await source() is None, f"An empty editor: asked {asked}, left {await source()!r}"
        draft = "uint8 lots_of_work\n@sealed\n"
        await dsdl_new_type(page)
        await page.locator("#dsdlEditorSource").fill(draft)
        await page.locator("#dsdlEditorClose").click()  # declined: the draft stays
        after_close = await source()
        await dsdl_new_type(page)  # a new type over the draft: declined too
        after_new = await source()
        answer["accept"] = True
        await page.locator("#dsdlEditorClose").click()
        assert len(asked) == 3 and after_close == after_new == draft and await source() is None, \
            f"Asked {len(asked)} times; the draft after a declined close {after_close!r}, " \
            f"after a declined new type {after_new!r}; once agreed, the editor holds {await source()!r}"
    finally:
        page.remove_listener("dialog", on_dialog)
        await dsdl_close(page, server)


# The "Active on bus" chips and toggle not cut off by their section.
BUS_SHOWN = """() => { const section = document.getElementById('dsdlBusActivity').getBoundingClientRect();
    const inside = (el) => el && el.getBoundingClientRect().bottom <= section.bottom + 0.5;
    const toggle = document.querySelector('#dsdlBusActivity .dsdl-bus-toggle');
    return {chips: [...document.querySelectorAll('#dsdlBusActivity .dsdl-bus-chip')].filter(inside).length,
            toggle: toggle?.textContent.trim() ?? null, toggleShown: inside(toggle)}; }"""


@test("DSDL: a type busy on the bus shows a few of its publishers, and how many more")
async def _(page):
    server = _DsdlServer()
    nodes = {"node_count": 12, "nodes": {str(nid): _graph_node(nid, f"org.example.dev{nid}", [7509], [], uid_byte=nid)
                                         for nid in range(10, 22)}}
    setup = f"""() => {{
        state.latestNodesPayload = {json.dumps(nodes)};
        cacheEvent({{subject_id: 7509, publisher_node_id: 10, message_type: 'Heartbeat_1_0', rate: 1,
                    subject_rate: 12, payload_bytes: 7, attributes: [], timestamp_unix: Date.now() / 1000}});
    }}"""
    await dsdl_open(page, server, setup)
    try:
        await dsdl_show(page, "uavcan.node.Heartbeat.1.0")
        await page.wait_for_selector("#dsdlBusActivity .dsdl-bus-toggle", state="attached", timeout=WAIT_MS)
        folded = await page.evaluate(BUS_SHOWN)
        assert folded["toggleShown"] and folded["toggle"] == f"+{12 - folded['chips']} more", \
            f"12 publishers, folded: {folded}"
        await page.locator("#dsdlBusActivity .dsdl-bus-toggle").click()
        await page.wait_for_timeout(100)
        unfolded = await page.evaluate(BUS_SHOWN)
        assert unfolded == {"chips": 12, "toggle": "show less", "toggleShown": True}, f"12 publishers, unfolded: {unfolded}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: the type tree works by keyboard, one Tab stop and the arrows")
async def _(page):
    server = _DsdlServer()
    focused = "[document.activeElement.dataset.type ?? document.activeElement.dataset.ns ?? document.activeElement.id," \
              " document.activeElement.getAttribute('aria-expanded')]"
    await dsdl_open(page, server)
    try:
        await page.locator("#dsdlRecompileBtn").focus()
        await page.keyboard.press("Tab")
        into = await page.evaluate(focused)
        for key in ("ArrowRight", "ArrowDown", "ArrowRight", "ArrowDown", "Enter"):
            await page.keyboard.press(key)
        await page.wait_for_timeout(500)
        shown = (await page.locator("#dsdlDetail").inner_text()).split("\n")[0]
        on_type = await page.evaluate(focused)
        await page.keyboard.press("ArrowLeft")  # to its namespace
        await page.keyboard.press("ArrowLeft")  # which closes
        closed = await page.evaluate(focused)
        await page.keyboard.press("Tab")  # out of the tree in one step
        out = await page.evaluate("document.activeElement.closest('#dsdlTree') === null")
        assert (into, on_type, shown, closed) == (["uavcan", "false"], ["uavcan.node.Heartbeat.1.0", None],
                                                  "uavcan.node.Heartbeat", ["uavcan.node", "false"]) and out, \
            f"Tab into the tree: {into}; → ↓ → ↓ Enter: {on_type}, showing {shown!r}; ← ←: {closed}; " \
            f"one Tab leaves the tree: {out}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a custom namespace's buttons and the 'Depends on' links work by keyboard")
async def _(page):
    server = _DsdlServer()
    server.types["myapp.Pair.1.0"] = _dsdl_type("myapp.Pair.1.0", source="custom", compiled=False,
                                                text="uavcan.si.unit.temperature.Scalar.1.0 a\n@sealed\n")
    focused = "document.activeElement.dataset.ns ?? document.activeElement.getAttribute('aria-label')"
    await dsdl_open(page, server)
    try:
        await page.locator("#dsdlCustomAddNs").focus()  # the Custom header's last button
        await page.keyboard.press("Tab")  # the custom tree's stop: its namespace
        stops = [await page.evaluate(focused)]
        for _ in range(2):
            await page.keyboard.press("Tab")  # on through the namespace's own buttons
            stops.append(await page.evaluate(focused))
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(300)
        editor = await page.locator("#dsdlEditorName").count()
        assert stops == ["myapp", "Add sub-namespace", "Add type"] and editor, \
            f"Tab, Tab, Tab from the Custom header: {stops}; Enter opened an editor: {bool(editor)}"
        await page.locator("#dsdlEditorClose").click()
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('.dsdl-type-row[data-type="myapp.Pair.1.0"]').click()
        await page.wait_for_selector(".dsdl-dep-chip", timeout=WAIT_MS)
        await page.locator(".dsdl-dep-chip").focus()
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(500)
        shown = (await page.locator("#dsdlDetail").inner_text()).split("\n")[0]
        assert shown == "uavcan.si.unit.temperature.Scalar", f"Enter on the dependency shows {shown!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: screen readers hear the editor's fields and the results, and the bus dots hold still on request")
async def _(page):
    server = _DsdlServer()
    server.compile_answer = ({"error": "myapp.Reading.1.0: Reading: Either `@sealed` or `@extent ...` are required"}, 422)
    nodes = {"node_count": 1, "nodes": {"10": _graph_node(10, "org.example.dev", [7509], [], uid_byte=1)}}
    setup = f"""() => {{
        state.latestNodesPayload = {json.dumps(nodes)};
        cacheEvent({{subject_id: 7509, publisher_node_id: 10, message_type: 'Heartbeat_1_0', rate: 1,
                    subject_rate: 1, payload_bytes: 7, attributes: [], timestamp_unix: Date.now() / 1000}});
    }}"""
    await dsdl_open(page, server, setup)
    try:
        await dsdl_new_type(page)
        fields = await page.evaluate("""['dsdlEditorNs', 'dsdlEditorName', 'dsdlEditorVer', 'dsdlEditorPort', 'dsdlEditorSource']
            .map((id) => [...document.getElementById(id).labels].map((label) => label.textContent.trim()).join('') || null)""")
        status = await page.locator("#dsdlEditorStatus").get_attribute("role")
        await page.locator("#dsdlCustomAddNs").click()
        cancel = await page.locator("#dsdlNsCancel").get_attribute("aria-label")
        await page.locator("#dsdlNsCancel").click()
        await page.locator("#dsdlCustomCompileBtn").click()
        await page.wait_for_selector(".dsdl-compile-error", timeout=WAIT_MS)
        compile_error = await page.locator(".dsdl-compile-error").get_attribute("role")
        tip = await page.locator(".dsdl-info-tip").get_attribute("aria-label") or ""
        await page.emulate_media(reduced_motion="reduce")
        pulse = await page.evaluate("getComputedStyle(document.querySelector('.dsdl-type-row .dsdl-bus-dot'), '::after').animationName")
        await page.emulate_media(reduced_motion="no-preference")
        assert fields == ["Namespace", "Type name", "Version", "Port ID", "Source"] and cancel \
            and (status, compile_error) == ("status", "alert") and "compiled" in tip and pulse == "none", \
            f"Fields named {fields}; × named {cancel!r}; roles: status {status!r}, compile error {compile_error!r}; " \
            f"the ? says {tip!r}; with reduced motion the bus dot pulses: {pulse!r}"
    finally:
        await page.emulate_media(reduced_motion="no-preference")
        await dsdl_close(page, server)


def _layout(low, high, sealed=True, extent=None, union=False):
    return {"union": union, "sealed": sealed, "extent_bytes": high if extent is None else extent,
            "size_bytes": [low, high]}


# Each field card's title and, beside it, its size.
CARD_TITLES = """() => [...document.querySelectorAll('#dsdlDetail .dsdl-card-label')]
    .filter((label) => label.querySelector('.dsdl-card-meta'))
    .map((label) => [label.firstChild.textContent.trim(), label.querySelector('.dsdl-card-meta').textContent])"""


@test("DSDL: a type's pane says whether it is a union, deprecated, and how many bytes it takes")
async def _(page):
    server = _DsdlServer()
    server.types["uavcan.node.Heartbeat.1.0"]["layout"] = _layout(7, 7, sealed=False, extent=12)
    value = _dsdl_type("uavcan.register.Value.1.0", text="uavcan.primitive.Empty.1.0 empty\nuint8 natural8\n@sealed\n")
    value.update(deprecated=True, layout=_layout(1, 259, union=True))
    info = _dsdl_type("uavcan.node.GetInfo.1.0", port=430, text="@sealed\n---\nuint8[<=50] name\n@extent 448 * 8\n")
    info["layout"] = {"request": _layout(0, 0), "response": _layout(1, 51, sealed=False, extent=448)}
    server.types.update({value["full_name"]: value, info["full_name"]: info})
    await dsdl_open(page, server)
    try:
        seen = {}
        for name in ("uavcan.node.Heartbeat.1.0", "uavcan.register.Value.1.0", "uavcan.node.GetInfo.1.0"):
            await dsdl_show(page, name)
            badges = await page.locator(".dsdl-badge-row").inner_text()
            seen[name] = (await page.evaluate(CARD_TITLES), "Deprecated" in badges)
        assert seen == {
            "uavcan.node.Heartbeat.1.0": ([["Fields", "7 bytes · extent 12 bytes"]], False),
            "uavcan.register.Value.1.0": ([["One of", "1–259 bytes · sealed"]], True),
            "uavcan.node.GetInfo.1.0": ([["Request", "0 bytes · sealed"],
                                         ["Response", "1–51 bytes · extent 448 bytes"]], False),
        }, f"Card titles and sizes, and a Deprecated badge: {seen}"
    finally:
        await dsdl_close(page, server)


# Each field's and constant's name, and the comment shown under it: [text, full comment on hover].
FIELD_COMMENTS = """() => [...document.querySelectorAll('#dsdlDetail .dsdl-fcol-name')].map((cell) => {
    const doc = cell.querySelector('.dsdl-fcol-doc');
    return [cell.firstChild.textContent.trim(), doc ? [doc.textContent, doc.title] : null]; })"""


@test("DSDL: a type's pane gives its description, and the comments on its fields and constants")
async def _(page):
    server = _DsdlServer()
    heartbeat = server.types["uavcan.node.Heartbeat.1.0"]
    heartbeat["doc"] = "Abstract node status information.\nAll nodes publish it.\n\nThe rest of the story."
    heartbeat["detail"] = {
        "fields": [{"type": "uint32", "name": "uptime", "doc": "[second]\nSeconds since the node started."},
                   {"type": "uint8", "name": "vendor_specific_status_code", "doc": ""}],
        "constants": [{"type": "uint16", "name": "MAX_PUBLICATION_PERIOD", "value": "1", "doc": "[second]"}],
    }
    await dsdl_open(page, server)
    try:
        await dsdl_show(page, "uavcan.node.Heartbeat.1.0")
        summary = await page.locator(".dsdl-doc-summary").all_inner_texts()
        comments = await page.evaluate(FIELD_COMMENTS)
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('.dsdl-type-row[data-type="myapp.Reading.1.0"]').click()  # no comments at all
        await page.wait_for_selector('.dsdl-doc-title:has-text("Reading")', timeout=WAIT_MS)
        bare = (await page.locator(".dsdl-doc-summary").count(), await page.evaluate(FIELD_COMMENTS))
        assert summary == ["Abstract node status information. All nodes publish it."] and comments == [
            ["uptime", ["[second]", "[second]\nSeconds since the node started."]],
            ["vendor_specific_status_code", None],
            ["MAX_PUBLICATION_PERIOD", ["[second]", "[second]"]],
        ] and bare == (0, [["value", None]]), \
            f"Description {summary}; comments {comments}; a type without any: {bare}"
    finally:
        await dsdl_close(page, server)


# What the pane says of a fault, and the source lines it marks.
PROBLEM_SHOWN = """() => [document.querySelector('#dsdlDetail .dsdl-problem')?.textContent.trim() ?? null,
    [...document.querySelectorAll('#dsdlDetail .dsdl-src-problem')].map((line) => line.textContent)]"""


@test("DSDL: a custom type that does not compile says why, with its line marked in the source")
async def _(page):
    server = _DsdlServer()
    typo = _dsdl_type("myapp.Typo.1.0", source="custom", compiled=False, text="uint8 a\nuint9x b\n@sealed\n")
    typo["problem"] = {"message": "Syntax error", "line": 2}
    unsealed = _dsdl_type("myapp.Unsealed.1.0", source="custom", compiled=False, text="uint8 a\n")
    unsealed["problem"] = {"message": "Either `@sealed` or `@extent ...` are required.", "line": None}
    server.types.update({t["full_name"]: t for t in (typo, unsealed)})
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        shown = {}
        for name in ("Typo", "Unsealed", "Reading"):
            await page.locator(f'.dsdl-type-row[data-type="myapp.{name}.1.0"]').click()
            await page.wait_for_selector(f'.dsdl-doc-title:has-text("{name}")', timeout=WAIT_MS)
            shown[name] = await page.evaluate(PROBLEM_SHOWN)
        assert shown == {
            "Typo": ["Does not compile — line 2: Syntax error", ["2uint9x b"]],
            "Unsealed": ["Does not compile: Either `@sealed` or `@extent ...` are required.", []],
            "Reading": [None, []],
        }, f"What each pane says, and the source lines it marks: {shown}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a first type is written from a 'New type' button, its namespace typed in")
async def _(page):
    server = _DsdlServer()
    del server.types["myapp.Reading.1.0"]  # no custom types, no namespace yet
    server.custom_namespaces.clear()
    await dsdl_open(page, server)
    try:
        new_type = page.get_by_role("button", name="New type")
        assert await new_type.count() == 1, "No 'New type' button where the custom types go"
        await new_type.click()
        await page.locator("#dsdlEditorNs").fill("myapp")
        await page.locator("#dsdlEditorName").fill("Reading")
        await page.locator("#dsdlEditorSource").fill("uint16 value\n@sealed\n")
        said = await dsdl_save(page)
        await page.wait_for_selector('#dsdlCustomTree .dsdl-type-row[data-type="myapp.Reading.1.0"]',
                                     state="attached", timeout=WAIT_MS)
        assert said == "Saved" and "myapp.Reading.1.0" in server.types, f"Saving said {said!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: 'New version' opens a copy of a custom type at the next version not taken")
async def _(page):
    server = _DsdlServer()
    taken = _dsdl_type("myapp.Reading.1.1", source="custom", text="uint16 value\nuint8 more\n@sealed\n")
    server.types[taken["full_name"]] = taken
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('.dsdl-type-row[data-type="myapp.Reading.1.0"]').click()  # compiled
        await page.wait_for_selector('.dsdl-doc-title:has-text("Reading")', timeout=WAIT_MS)
        new_version = page.get_by_role("button", name="New version")
        assert await new_version.count() == 1, "A compiled custom type offers no 'New version'"
        await new_version.click()
        await page.wait_for_selector("#dsdlEditorName", timeout=WAIT_MS)
        copy = await page.evaluate("""['dsdlEditorNs', 'dsdlEditorName', 'dsdlEditorVer', 'dsdlEditorSource']
            .map((id) => document.getElementById(id).value)""")
        assert copy == ["myapp", "Reading", "1.2", "uint16 value\n@sealed\n"], f"The editor opens on {copy}"
        said = await dsdl_save(page)
        assert said == "Saved" and "myapp.Reading.1.2" in server.types, f"Saving the new version said {said!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: an empty custom namespace can be deleted, one with types cannot")
async def _(page):
    server = _DsdlServer()
    server.custom_namespaces.add("spare")  # no types in it
    await dsdl_open(page, server)
    try:
        offered = {ns: await page.locator(f'[data-delete-ns="{ns}"]').count() for ns in ("spare", "myapp")}
        assert offered == {"spare": 1, "myapp": 0}, f"A delete button for each namespace: {offered}"
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="spare"]').hover()
        await page.locator('[data-delete-ns="spare"]').click()
        await page.wait_for_selector('#dsdlCustomTree .dsdl-ns-row[data-ns="spare"]', state="detached", timeout=WAIT_MS)
        assert "spare" not in server.custom_namespaces, "The server still has it"
    finally:
        await dsdl_close(page, server)


@test("DSDL: types are listed by name, then by version")
async def _(page):
    server = _DsdlServer()
    # As the server sends them: by file name, port-IDs first.
    for name, port in (("Version.1.0", None), ("ExecuteCommand.1.10", 435), ("ExecuteCommand.1.9", 435),
                       ("Health.1.0", None)):
        added = _dsdl_type(f"uavcan.node.{name}", port=port)
        server.types[added["full_name"]] = added
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlTree .dsdl-ns-row[data-ns="uavcan"]').click()
        await page.locator('#dsdlTree .dsdl-ns-row[data-ns="uavcan.node"]').click()
        listed = await page.evaluate("""[...document.querySelectorAll(
            '.dsdl-ns-row[data-ns="uavcan.node"] + .dsdl-ns-children > .dsdl-type-row')].map((row) => row.dataset.type)""")
        assert [t.removeprefix("uavcan.node.") for t in listed] == [
            "ExecuteCommand.1.9", "ExecuteCommand.1.10", "Health.1.0", "Heartbeat.1.0", "Version.1.0"], f"Listed: {listed}"
    finally:
        await dsdl_close(page, server)


async def dsdl_search(page, term):
    """Searched, and what the count under the search box says."""
    await page.locator("#dsdlSearch").fill(term)
    await page.wait_for_timeout(400)  # the search's debounce
    count = page.locator("#dsdlSearchCount")
    return await count.inner_text() if await count.count() else None


@test("DSDL: the search sits above the trees and says how many types match")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        above = await page.evaluate("""document.getElementById('dsdlSearch').getBoundingClientRect().bottom
            <= document.querySelector('#dsdlTreePanel .dsdl-tree-scroll').getBoundingClientRect().top""")
        said = {term: await dsdl_search(page, term) for term in ("Scalar", "Heartbeat", "zzz", "")}
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').hover()
        await page.locator('[data-hide-ns="myapp"]').click()
        said["Reading, its namespace hidden"] = await dsdl_search(page, "Reading")
        assert above and said == {
            "Scalar": "2 types match", "Heartbeat": "1 type matches", "zzz": "No type matches", "": "",
            "Reading, its namespace hidden": "No type matches, 1 more in hidden namespaces",
        }, f"Search above the trees: {above}; the count says {said}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: your own types come first, above the public ones")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        first = await page.evaluate("""document.getElementById('dsdlCustomHeader').getBoundingClientRect().top
            < document.getElementById('dsdlPublicHeader').getBoundingClientRect().top""")
        assert first, "The custom types come after all the public ones"
    finally:
        await dsdl_close(page, server)


@test("DSDL: the search finds a type by its compiled name, as messages and recordings give it")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        found = {}
        for term in ("Heartbeat_1_0", "uavcan.node.Heartbeat_1_0"):
            await dsdl_search(page, term)
            found[term] = await page.evaluate(
                "[...document.querySelectorAll('#dsdlTreePanel .dsdl-type-row')].map((row) => row.dataset.type)")
        assert found == {term: ["uavcan.node.Heartbeat.1.0"] for term in found}, f"Found: {found}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: the search finds a type by one of its constants")
async def _(page):
    server = _DsdlServer()
    server.types["uavcan.node.Heartbeat.1.0"]["source_text"] = "uint16 MAX_PUBLICATION_PERIOD = 1\nuint32 uptime\n@sealed\n"
    await dsdl_open(page, server)
    try:
        count = await dsdl_search(page, "max_publication")
        found = await page.evaluate(
            "[...document.querySelectorAll('#dsdlTreePanel .dsdl-type-row')].map((row) => row.dataset.type)")
        assert found == ["uavcan.node.Heartbeat.1.0"], f"Found {found}; the count says {count!r}"
    finally:
        await dsdl_close(page, server)


# A theme colour, --muted for one, as the browser draws it.
TOKEN_COLOUR = """(name) => { const probe = document.createElement('span'); probe.style.color = `var(--${name})`;
    document.body.append(probe); const colour = getComputedStyle(probe).color; probe.remove(); return colour; }"""

# The computed `property` of the element `selector` finds.
COLOUR_OF = "([selector, property]) => getComputedStyle(document.querySelector(selector))[property]"


@test("DSDL: the tree keeps colour for the unusual: a service's tag, a type not compiled yet")
async def _(page):
    server = _DsdlServer()
    pending = _dsdl_type("myapp.Pending.1.0", source="custom", compiled=False)
    service = _dsdl_type("uavcan.node.GetInfo.1.0", port=430, text="@sealed\n---\nuint8 name\n@sealed\n")
    server.types.update({t["full_name"]: t for t in (pending, service)})
    await dsdl_open(page, server)
    try:
        tokens = {name: await page.evaluate(TOKEN_COLOUR, name) for name in ("muted", "accent", "warn")}
        seen = {
            "message tag": await page.evaluate(COLOUR_OF, ['[data-type="uavcan.node.Heartbeat.1.0"] .dsdl-kind', "color"]),
            "its fill": await page.evaluate(COLOUR_OF, ['[data-type="uavcan.node.Heartbeat.1.0"] .dsdl-kind', "backgroundColor"]),
            "service tag": await page.evaluate(COLOUR_OF, ['[data-type="uavcan.node.GetInfo.1.0"] .dsdl-kind', "color"]),
            "not compiled": await page.evaluate(COLOUR_OF, ['[data-type="myapp.Pending.1.0"] .dsdl-type-name', "color"]),
            "its namespace": await page.evaluate(COLOUR_OF, ['.dsdl-ns-row[data-ns="myapp"] .dsdl-ns-label', "color"]),
        }
        assert seen == {"message tag": tokens["muted"], "its fill": "rgba(0, 0, 0, 0)", "service tag": tokens["accent"],
                        "not compiled": tokens["warn"], "its namespace": tokens["warn"]}, \
            f"Drawn {seen}; the theme's muted, accent and warn are {tokens}"
    finally:
        await dsdl_close(page, server)


# Each badge of the type shown, by its text: its colour.
BADGE_COLOURS = """() => Object.fromEntries([...document.querySelectorAll('#dsdlDetail .dsdl-badge-row .dsdl-badge')]
    .map((badge) => [badge.textContent.trim(), getComputedStyle(badge).color]))"""


@test("DSDL: a type's pane keeps colour for the unusual: not compiled, deprecated")
async def _(page):
    server = _DsdlServer()
    server.types["uavcan.node.Heartbeat.1.0"]["detail"] = {
        "constants": [{"type": "uint16", "name": "MAX_PUBLICATION_PERIOD", "value": "1", "doc": ""}]}
    pending = _dsdl_type("myapp.Pending.1.0", source="custom", compiled=False)
    pending["deprecated"] = True
    server.types[pending["full_name"]] = pending
    await dsdl_open(page, server)
    try:
        tokens = {name: await page.evaluate(TOKEN_COLOUR, name) for name in ("muted", "text", "warn")}
        await dsdl_show(page, "uavcan.node.Heartbeat.1.0")
        heartbeat = await page.evaluate(BADGE_COLOURS)
        cells = [await page.evaluate(COLOUR_OF, [f"#dsdlDetail {cell}", "color"])
                 for cell in (".dsdl-fcol-type", ".dsdl-fcol-val")]
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        shown = {}
        for name in ("Reading", "Pending"):
            await page.locator(f'.dsdl-type-row[data-type="myapp.{name}.1.0"]').click()
            await page.wait_for_selector(f'.dsdl-doc-title:has-text("{name}")', timeout=WAIT_MS)
            shown[name] = await page.evaluate(BADGE_COLOURS)
        await dsdl_new_type(page)
        await page.locator("#dsdlEditorSource").fill("uint8 a\n@sealed\n---\nuint8 b\n@sealed\n")
        await page.wait_for_selector(".dsdl-preview-label-res", timeout=WAIT_MS)
        response = await page.evaluate(COLOUR_OF, [".dsdl-preview-label-res", "color"])
        plain, flagged = tokens["muted"], tokens["warn"]
        seen = {"Message": heartbeat["Message"], "Compiled": heartbeat["Compiled"], "Custom": shown["Reading"]["Custom"],
                "Not compiled": shown["Pending"]["Not compiled"], "Deprecated": shown["Pending"]["Deprecated"],
                "field type, constant value": cells, "preview's Response": response}
        assert seen == {"Message": plain, "Compiled": plain, "Custom": plain, "Not compiled": flagged,
                        "Deprecated": flagged, "field type, constant value": [tokens["text"]] * 2,
                        "preview's Response": plain}, f"Drawn {seen}; the theme's {tokens}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: a type's buttons keep their height when the note beside them wraps")
async def _(page):
    server = _DsdlServer()
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('.dsdl-type-row[data-type="myapp.Reading.1.0"]').click()  # compiled: a note by its buttons
        await page.wait_for_selector("#dsdlNewVersionBtn", timeout=WAIT_MS)
        await dsdl_new_type(page)  # the editor beside it narrows the pane
        await page.wait_for_timeout(300)
        heights = await page.evaluate("""['dsdlEditorSave', 'dsdlNewVersionBtn', 'dsdlDeleteBtn']
            .map((id) => Math.round(document.getElementById(id).getBoundingClientRect().height))""")
        assert heights[0] == heights[1] == heights[2], f"Save, New version and Delete are {heights} tall"
    finally:
        await dsdl_close(page, server)


@test("DSDL: the editor's preview shows its own source, not a closed editor's")
async def _(page):
    server = _DsdlServer()
    page.once("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))  # closing a draft asks
    await dsdl_open(page, server)
    try:
        await dsdl_new_type(page)
        # Typed, closed, and another editor opened, within the preview's 250 ms.
        await page.evaluate("""() => {
            const source = document.getElementById('dsdlEditorSource');
            source.value = 'uint8 stale_field\\n@sealed\\n';
            source.dispatchEvent(new Event('input', {bubbles: true}));
            document.getElementById('dsdlEditorClose').click();
            document.querySelector('[data-add-type="myapp"]').click();
        }""")
        await page.wait_for_selector("#dsdlEditorSource", timeout=WAIT_MS)
        await page.wait_for_timeout(500)
        preview = await page.locator("#dsdlPreviewContent").inner_text()
        assert "stale_field" not in preview, f"A new, empty editor's preview shows {preview!r}"
    finally:
        await dsdl_close(page, server)


# Inline styling in the DSDL tab and on the page's body: any property but a
# CSS variable, and any value in px.
INLINE_STYLING = """() => [...document.querySelectorAll('#dsdlContainer [style]'), document.body]
    .flatMap((el) => [...el.style].filter((prop) => !prop.startsWith('--') || el.style.getPropertyValue(prop).includes('px'))
    .map((prop) => `${el.id || el.tagName.toLowerCase()} ${prop}: ${el.style.getPropertyValue(prop)}`))"""


async def drag_by(page, selector, dx=0, dy=0):
    """Pressed on `selector` and moved by dx, dy; the caller lets go."""
    box = await page.locator(selector).bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await page.mouse.move(x, y)
    await page.mouse.down()
    await page.mouse.move(x + dx, y + dy, steps=4)


@test("DSDL: the tab is styled by its CSS, in rem, its splits still dragged and its rows indented as before")
async def _(page):
    server = _DsdlServer()
    width = "(id) => Math.round(document.getElementById(id).getBoundingClientRect().width)"
    height = "(id) => Math.round(document.getElementById(id).getBoundingClientRect().height)"
    await dsdl_open(page, server)
    try:
        await dsdl_show(page, "uavcan.si.unit.temperature.Scalar.1.0")
        # Each depth a step of 1.125rem: a namespace's row at 0.5rem past it, a type's at 1rem.
        indents = await page.evaluate("""[
            getComputedStyle(document.querySelector('.dsdl-ns-row[data-ns="uavcan.si.unit.temperature"]')).paddingLeft,
            getComputedStyle(document.querySelector('.dsdl-type-row[data-type="uavcan.si.unit.temperature.Scalar.1.0"]')).paddingLeft]""")
        await dsdl_new_type(page)
        # Each split dragged, and what it moved measured before the next.
        moved, while_dragging = [], None
        for handle, measure, part, dx, dy in (("#dsdlEditorHandle", width, "dsdlEditorPanel", 60, 0),
                                              ("#dsdlPreviewHandle", height, "dsdlEditorSource", 0, 40),
                                              ("#dsdlSplitHandle", width, "dsdlTreePanel", 50, 0)):
            before = await page.evaluate(measure, part)
            await drag_by(page, handle, dx, dy)
            if handle == "#dsdlSplitHandle":
                while_dragging = await page.evaluate(INLINE_STYLING)
            await page.mouse.up()
            moved.append(await page.evaluate(measure, part) - before)
        styled = await page.evaluate(INLINE_STYLING)
        # As before: a split follows the pointer, so these are not quite 60 and 40.
        assert indents == ["62px", "88px"] and all(abs(m - e) <= 2 for m, e in zip(moved, (62, 46, 50))), \
            f"Indents {indents} (62px, 88px before); dragged by 60, 40 and 50 px, the editor, source and tree " \
            f"moved {moved} (62, 46 and 50 before)"
        assert not styled and not while_dragging, f"Styled inline: {styled}; while a split is dragged: {while_dragging}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: the tree keeps its width across reloads, saved in rem, a px one from earlier versions too")
async def _(page):
    server = _DsdlServer()
    width = "() => Math.round(document.getElementById('dsdlTreePanel').getBoundingClientRect().width)"

    async def reopen(dsdl_state=None):
        await page.evaluate(f"""() => {{ switchView('nodes'); state.dashboardConnected = false; _writeSettingsNow();
            {f"localStorage.setItem('cynitor.dsdl.state', {json.dumps(json.dumps(dsdl_state))});" if dsdl_state else ""} }}""")
        await page.reload(wait_until="load")
        await page.evaluate("state.dashboardConnected = true; switchView('dsdl')")
        await page.wait_for_selector("#dsdlTree .dsdl-ns-row", timeout=5000)

    await page.route("**/api/dsdl/**", server.handle)
    try:
        await reopen({"treeWidth": 402})  # as an earlier version saved it, in px
        legacy = await page.evaluate(width)
        await drag_by(page, "#dsdlSplitHandle", dx=-40)
        await page.mouse.up()
        saved = await page.evaluate("JSON.parse(localStorage.getItem('cynitor.dsdl.state'))")
        await reopen()
        kept = await page.evaluate(width)
        assert (legacy, round(saved.get("treeWidthRem") or 0, 3), kept) == (402, 22.625, 362), \
            f"A px width saved earlier gives {legacy} px; dragged to 362 px it is saved as {saved}, and reopens at {kept} px"
    finally:
        await dsdl_close(page, server)


@test("DSDL: an editor open on a custom type locks itself once the type is compiled")
async def _(page):
    server = _DsdlServer()
    server.types["myapp.Reading.1.0"]["compiled"] = False
    usable = """() => [...document.getElementById('dsdlEditorPanel').querySelectorAll('input, textarea, button')]
        .filter(control => !control.disabled).map(control => control.id)"""
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('#dsdlCustomTree .dsdl-type-row[data-type="myapp.Reading.1.0"]').click()
        await page.locator("#dsdlEditBtn").click()
        await page.wait_for_selector("#dsdlEditorSource", timeout=WAIT_MS)
        before = await page.evaluate(usable)
        # Compiled from this tab: the tree it reloads has the type compiled.
        server.types["myapp.Reading.1.0"]["compiled"] = True
        await page.locator("#dsdlCustomCompileBtn").click()
        await page.wait_for_selector("#dsdlEditorPanel .dsdl-editor-locked-banner", timeout=WAIT_MS)
        banner = await page.locator(".dsdl-editor-locked-banner").inner_text()
        after = await page.evaluate(usable)
        assert (before, after) == (["dsdlEditorSave", "dsdlEditorClose", "dsdlEditorPort", "dsdlEditorSource"],
                                   ["dsdlEditorClose"]) and "new version" in banner, \
            f"Usable while editing: {before}; once compiled: {after}, and the editor says {banner!r}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: 'New version' from a locked editor opens a draft that can be edited")
async def _(page):
    server = _DsdlServer()
    server.types["myapp.Reading.1.0"]["compiled"] = False
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        await page.locator('#dsdlCustomTree .dsdl-type-row[data-type="myapp.Reading.1.0"]').click()
        await page.locator("#dsdlEditBtn").click()
        server.types["myapp.Reading.1.0"]["compiled"] = True
        await page.locator("#dsdlCustomCompileBtn").click()
        await page.wait_for_selector(".dsdl-editor-locked-banner", timeout=WAIT_MS)
        # What the lock's banner advises.
        await page.locator("#dsdlNewVersionBtn").click()
        await page.wait_for_function("document.getElementById('dsdlEditorVer')?.value === '1.1'", timeout=WAIT_MS)
        try:
            await page.locator("#dsdlEditorSource").click(timeout=WAIT_MS)
            await page.keyboard.type("uint8 extra\n")
            typed = await page.locator("#dsdlEditorSource").input_value()
        except Exception as e:
            typed = f"not clicked into: {str(e).splitlines()[0]}"
        save = await page.evaluate("getComputedStyle(document.getElementById('dsdlEditorSave')).opacity")
        assert "uint8 extra" in typed and save == "1", \
            f"The new version's source reads {typed!r}; its Save is drawn at opacity {save}"
    finally:
        await dsdl_close(page, server)


@test("DSDL: after a compile that fails, the tree shows at once what did compile")
async def _(page):
    server = _DsdlServer()
    server.types["myapp.Reading.1.0"]["compiled"] = False
    server.types["other.Broken.1.0"] = _dsdl_type("other.Broken.1.0", source="custom", compiled=False,
                                                  text="uint8 x\n")
    server.custom_namespaces.add("other")
    server.compile_answer = ({"error": "other.Broken.1.0: Either `@sealed` or `@extent ...` are required"}, 422)
    await dsdl_open(page, server)
    try:
        await page.locator('#dsdlCustomTree .dsdl-ns-row[data-ns="myapp"]').click()
        # The compile compiles myapp, and fails in other. The status the tab
        # polls stays as it was, so no poll reloads the tree in its stead.
        server.types["myapp.Reading.1.0"]["compiled"] = True
        await page.locator("#dsdlCustomCompileBtn").click()
        compiled = '#dsdlCustomTree .dsdl-type-row.dsdl-type-compiled[data-type="myapp.Reading.1.0"]'
        try:
            await page.wait_for_selector(compiled, timeout=WAIT_MS)
            shown = True
        except Exception:
            shown = False
        error = await page.locator("#dsdlCustomHeader ~ .dsdl-compile-error").inner_text()
        assert shown and "other.Broken.1.0" in error, \
            f"myapp.Reading.1.0 shown compiled: {shown}; the error under the header: {error!r}"
    finally:
        await dsdl_close(page, server)


# How many lines an element's text takes, and its words drawn across two.
SPLIT_WORDS = r"""(el) => {
    const text = el.firstChild, range = document.createRange(), split = [];
    range.selectNodeContents(text);
    const lines = new Set([...range.getClientRects()].map(rect => Math.round(rect.top))).size;
    for (const word of text.data.matchAll(/\S+/g)) {
        range.setStart(text, word.index);
        range.setEnd(text, word.index + word[0].length);
        if (range.getClientRects().length > 1) split.push(word[0]);
    }
    return { lines, split };
}"""


@test("DSDL: a compile error wraps between words, never inside one")
async def _(page):
    server = _DsdlServer()
    server.compile_answer = ({"error": "myapp.Reading.1.0: Reading: Either `@sealed` or `@extent ...` are required. "
                                       "The smallest valid extent for this type (i.e., its max bit length) is 8 bits "
                                       "(1 bytes). If you are not sure what this means, add the following line near "
                                       "the end of this definition: `@extent 64 * 8`"}, 422)
    await dsdl_open(page, server)
    try:
        await page.locator("#dsdlCustomCompileBtn").click()
        error = page.locator("#dsdlCustomHeader ~ .dsdl-compile-error")
        await error.wait_for(timeout=WAIT_MS)
        drawn = await error.evaluate(SPLIT_WORDS)
        assert drawn["lines"] > 1 and not drawn["split"], \
            f"Over {drawn['lines']} lines, these words are cut in two: {drawn['split']}"
    finally:
        await dsdl_close(page, server)


# ── Record tab ──
#
# /api/recordings* and /api/rawlogs are answered by _RecordServer from
# recordings shaped as the backend sends them; starting, stopping, renaming
# and deleting change them as the backend would. Each test opens the tab on a
# freshly loaded page.

def _recording(rid, name, live=False, start=None, **fields):
    start = start or time.time() - 3600 + rid
    return {"id": rid, "name": name, "start_unix": start, "end_unix": None if live else start + 60,
            "filter": {}, "notes": None, "created_at": start, "max_length_seconds": 3600.0,
            "max_events": 100000, "stop_on_limit": True, "auto_stopped": False, "event_count": 120,
            "events_source": "dedicated", "subjects": [100], "duration_seconds": 60.0, **fields}


class _RecordServer:
    """The backend's /api/recordings* and /api/rawlogs routes, over a few recordings."""

    def __init__(self, recordings=()):
        self.recordings = {r["id"]: r for r in recordings}
        self.requests = []          # (method, path, JSON body)
        self.gates = {}             # (method, path) -> an asyncio.Event its answer waits for
        self.failures = {}          # (method, path) -> (status, error) it answers instead
        self.rawlogs = {"active": None, "logs": []}  # GET /api/rawlogs

    def _answer(self, method, path, body):
        if (method, path) in self.failures:
            status, error = self.failures[(method, path)]
            return {"error": error}, status
        recs = self.recordings
        if path == "/api/rawlogs":
            return self.rawlogs, 200
        if path == "/api/recordings/buffer":
            return {"buffer": {"retention_seconds": 86400, "max_events": 5000000, "event_count": 0,
                               "oldest_event_unix": None, "newest_event_unix": None, "db_size_bytes": 4096}}, 200
        if path == "/api/recordings":
            if method == "GET":
                return {"recordings": sorted(recs.values(), key=lambda r: -r["start_unix"])}, 200
            rid = max(recs, default=0) + 1
            recs[rid] = _recording(rid, body["name"], live=True, start=time.time(), event_count=0,
                                   filter=body.get("filter") or {}, max_length_seconds=body.get("max_length_seconds"),
                                   max_events=body.get("max_events"), stop_on_limit=bool(body.get("stop_on_limit")))
            return {"recording": recs[rid]}, 201
        if path == "/api/recordings/quick":  # the last seconds of the history, saved: 42 events in them
            rid, now = max(recs, default=0) + 1, time.time()
            recs[rid] = _recording(rid, body["name"], start=now - body["last_seconds"], end_unix=now, event_count=42,
                                   filter=body.get("filter") or {}, max_length_seconds=None, max_events=None,
                                   stop_on_limit=False)
            return {"recording": recs[rid]}, 201
        match = re.fullmatch(r"/api/recordings/(\d+)(/stop|/export)?", path)
        rec = recs.get(int(match[1])) if match else None
        if rec is None:
            return {"error": "Recording not found"}, 404
        if match[2] == "/stop":
            rec["end_unix"] = time.time()
        elif method == "PATCH":  # absent or null fields keep their value, as on the backend
            rec.update({key: value for key, value in body.items() if value is not None})
        elif method == "DELETE":
            del recs[rec["id"]]
            return {"deleted": True, "purged": False}, 200
        return {"recording": rec}, 200

    async def handle(self, route):
        request = route.request
        path = unquote(urlparse(request.url).path)
        body = request.post_data_json if request.post_data else None
        self.requests.append((request.method, path, body))
        if (request.method, path) in self.gates:
            await self.gates[(request.method, path)].wait()
        answer, status = self._answer(request.method, path, body)
        cors = {"Access-Control-Allow-Origin": "*"}
        if path.endswith("/export") and status == 200:
            name = re.sub(r"[^A-Za-z0-9_.-]", "_", answer["recording"]["name"])
            await route.fulfill(body="recording_id,timestamp_unix\n", status=200, headers={
                **cors, "Content-Type": "text/csv", "Content-Disposition": f'attachment; filename="{name}.csv"'})
        else:
            await route.fulfill(json=answer, status=status, headers=cors)

    def count(self, method, path):
        return sum(1 for m, p, _ in self.requests if (m, p) == (method, path))


async def record_open(page, server, setup=None, can=True):
    """The Record tab on a freshly loaded page, served by `server`, with CAN
    connected or not. `setup` runs in the page before the tab opens."""
    await page.evaluate("() => { state.dashboardConnected = false; _writeSettingsNow(); }")
    await page.reload(wait_until="load")
    await page.route("**/api/recordings**", server.handle)
    await page.route("**/api/rawlogs**", server.handle)
    await page.evaluate(f"state.dashboardConnected = true; state.canConnected = {json.dumps(can)};")
    if setup:
        await page.evaluate(setup)
    await page.evaluate("switchView('record')")
    await page.wait_for_selector("#recordList .record-card, #recordList .record-empty", timeout=5000)


async def record_close(page, server):
    if not page.url.startswith(BASE_URL):  # a download that went wrong took the page away
        await page.goto(BASE_URL, wait_until="load")
    await page.evaluate("""() => { switchView('nodes'); state.dashboardConnected = false; state.canConnected = false;
        state.latestNodesPayload = null; state.latestBySubject.clear(); state.latestByNode.clear(); }""")
    await page.unroute("**/api/recordings**", server.handle)
    await page.unroute("**/api/rawlogs**", server.handle)


@test("Record: an export downloads the file, and one that fails keeps the dashboard and says why")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence")])
    await record_open(page, server)
    try:
        csv = page.locator('.record-card [data-action="export-csv"]')
        async with page.expect_download() as download:
            await csv.click()
        assert (await download.value).suggested_filename == "boot_sequence.csv"
        del server.recordings[1]  # deleted from another dashboard since the list was drawn
        await csv.click()
        await page.wait_for_timeout(1000)
        assert page.url.startswith(BASE_URL), f"The export replaced the dashboard with {page.url}"
        toasts = await page.locator("#toastContainer .toast").all_inner_texts()
        assert any("Recording not found" in t for t in toasts), f"Toasts shown: {toasts}"
    finally:
        await record_close(page, server)


# Errors thrown, or promises rejected and not handled, from here on.
COLLECT_ERRORS = """() => { window.e2eErrors = [];
    window.addEventListener('error', (e) => e2eErrors.push(String(e.message)));
    window.addEventListener('unhandledrejection', (e) => e2eErrors.push(String(e.reason))); }"""


@test("Record: the tab opens without errors")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence")])
    await record_open(page, server, setup=COLLECT_ERRORS)
    try:
        await page.wait_for_timeout(500)
        errors = await page.evaluate("e2eErrors")
        assert not errors, f"Opening the tab threw: {errors}"
    finally:
        await record_close(page, server)


@test("Record: Start makes one recording however fast it is clicked")
async def _(page):
    server = _RecordServer()
    server.gates[("POST", "/api/recordings")] = gate = asyncio.Event()
    await record_open(page, server)
    try:
        await page.locator("#recName").fill("bench run")
        await page.locator("#recStart").dblclick()
        await page.wait_for_timeout(300)
        gate.set()
        await page.wait_for_selector(".record-card.live", timeout=WAIT_MS)
        await page.wait_for_timeout(300)
        posts = server.count("POST", "/api/recordings")
        assert posts == 1, f"A double-click on Start sent {posts} requests: {len(server.recordings)} recordings"
    finally:
        await record_close(page, server)


@test("Record: Start is off, and says why, until CAN is connected")
async def _(page):
    server = _RecordServer()
    await record_open(page, server, can=False)
    try:
        start = page.locator("#recStart")
        why = await page.locator("#recStartWhy").all_inner_texts()
        assert await start.is_disabled() and why and "CAN" in why[0], \
            f"With CAN not connected, Start is disabled: {await start.is_disabled()}, and the tab says {why}"
        await page.evaluate("state.canConnected = true; updateSemaphores();")
        assert await start.is_enabled(), "Start stays disabled once CAN is connected"
        assert not await page.locator("#recStartWhy").inner_text(), "The reason stays once CAN is connected"
    finally:
        await record_close(page, server)


@test("Record: Edit limits shows a recording's own limits, and sends only what is changed")
async def _(page):
    server = _RecordServer([
        _recording(1, "no limits", live=True, max_length_seconds=None, max_events=None, stop_on_limit=False),
        _recording(2, "ten minutes", live=True, max_length_seconds=600.0)])
    await record_open(page, server)
    limits = ("max_length_seconds", "max_events", "stop_on_limit")
    try:
        for rid in (1, 2):  # opened and applied as it is, nothing changes
            before = {key: server.recordings[rid][key] for key in limits}
            await page.locator(f'.record-card[data-id="{rid}"] [data-action="edit-limits"]').click()
            shown = await page.evaluate("[el('recEditLength'), el('recEditMaxEvents')].map(s => s.selectedOptions[0].text)")
            await page.locator("#recEditApply").click()
            await page.wait_for_timeout(500)
            after = {key: server.recordings[rid][key] for key in limits}
            assert after == before, f"The dialog showed {shown} for {before}; Apply made it {after}"
        await page.locator('.record-card[data-id="1"] [data-action="edit-limits"]').click()
        await page.locator("#recEditMaxEvents").select_option("10000")
        await page.locator("#recEditApply").click()
        await page.wait_for_timeout(500)
        patches = [body for method, _, body in server.requests if method == "PATCH"]
        assert patches == [{"max_events": 10000}], f"Requests sent: {patches}"
    finally:
        await record_close(page, server)


@test("Record: a recording's times count whole seconds, never 60 of them")
async def _(page):
    now = time.time()
    server = _RecordServer([
        _recording(1, "two minutes less a bit", start=now - 7200, end_unix=now - 7200 + 119.6),
        _recording(2, "an hour less a bit", start=now - 18000, end_unix=now - 18000 + 3599.7)])
    await record_open(page, server)
    try:
        times = await page.locator(".record-card .rec-bar-right").all_inner_texts()
        assert any(t.startswith("1m 59s") for t in times) and any(t.startswith("59m 59s") for t in times) \
            and not any("60s" in t or "60m" in t for t in times), f"Times shown: {times}"
    finally:
        await record_close(page, server)


# Every toast shown from here on, in window.e2eToasts.
COLLECT_TOASTS = """() => { window.e2eToasts = [];
    new MutationObserver((changes) => changes.forEach((c) => c.addedNodes.forEach((n) => e2eToasts.push(n.textContent))))
        .observe(el('toastContainer'), {childList: true}); }"""


@test("Record: a list that cannot be loaded says so once, in place, and keeps what it showed")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence")])
    await record_open(page, server, setup=COLLECT_TOASTS)
    try:
        server.failures[("GET", "/api/recordings")] = (500, "database is locked")
        for _ in range(3):  # three polls
            await page.evaluate("fetchRecordings()")
            await page.wait_for_timeout(200)
        toasts = await page.evaluate("e2eToasts")
        note = await page.locator("#recordList .record-list-error").all_inner_texts()
        assert not toasts and len(note) == 1 and "database is locked" in note[0], \
            f"After three failed loads, toasts: {toasts}; a note in the list: {note}"
        assert await page.locator(".record-card").count() == 1, "The recordings shown before are gone"
        del server.failures[("GET", "/api/recordings")]
        await page.evaluate("fetchRecordings()")
        await page.wait_for_timeout(300)
        assert not await page.locator("#recordList .record-list-error").count(), "The note stays once the list loads"
    finally:
        await record_close(page, server)


@test("Record: opening the tab asks for each list once, and nothing while disconnected")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence")])
    await record_open(page, server)
    try:
        await page.wait_for_timeout(500)
        asked = {path: server.count("GET", path) for path in ("/api/recordings", "/api/recordings/buffer", "/api/rawlogs")}
        assert set(asked.values()) == {1}, f"Requests on opening the tab: {asked}"
        await page.evaluate("state.dashboardConnected = false")
        before = len(server.requests)
        await page.wait_for_timeout(2500)  # the raw logs are polled every 2 s
        assert not server.requests[before:], f"Requests with the backend disconnected: {server.requests[before:]}"
    finally:
        await record_close(page, server)


@test("Record: Play is off, saying why, for a recording with nothing to replay")
async def _(page):
    server = _RecordServer([
        _recording(1, "old bookmark", events_source="global"),
        _recording(2, "nothing came", event_count=0),
        _recording(3, "boot sequence")])
    await record_open(page, server, can=False)  # replay needs CAN disconnected
    try:
        plays = {}
        for rid in (1, 2, 3):
            play = page.locator(f'.record-card[data-id="{rid}"] [data-action="play"]')
            plays[rid] = (await play.is_disabled(), await play.get_attribute("title"))
        assert plays[1][0] and plays[2][0] and not plays[3][0], f"Play (disabled, title) by recording: {plays}"
    finally:
        await record_close(page, server)


# Node 10 publishes subject 100 at 10 Hz; nodes 10 and 11 send Heartbeat at
# 1 Hz each: 12 messages a second in all, for as long as window.e2eRateFeed runs.
RECORD_RATES = """() => {
    state.latestNodesPayload = {node_count: 2, nodes: {
        '10': {node_id: 10, name: 'org.example.imu', publishers: [100, 7509], subscribers: [], servers: [], clients: []},
        '11': {node_id: 11, name: 'org.example.esc', publishers: [7509], subscribers: [], servers: [], clients: []}}};
    const ev = (subject_id, publisher_node_id, rate, subject_rate) => ({subject_id, publisher_node_id,
        message_type: 'Real64_1_0', rate, subject_rate, attributes: [], timestamp_unix: Date.now() / 1000});
    const feed = () => { cacheEvent(ev(100, 10, 10, 10)); cacheEvent(ev(7509, 10, 1, 2)); cacheEvent(ev(7509, 11, 1, 2)); };
    feed();
    window.e2eRateFeed = setInterval(feed, 500);
}"""


@test("Record: the size estimate is for what the selection would record, at the rate it is sent now")
async def _(page):
    server = _RecordServer()
    await record_open(page, server, setup=RECORD_RATES)
    try:
        await page.evaluate("state.recordFilterDraft.subject_ids = [100]; _refreshSelection();")
        selected = await page.locator("#recDiskHint").inner_text()
        await page.evaluate("state.recordFilterDraft.subject_ids = []; _refreshSelection();")
        everything = await page.locator("#recDiskHint").inner_text()
        assert "~10 msg/s" in selected and "~12 msg/s" in everything, \
            f"With subject 100 selected: {selected!r}; with nothing selected (everything): {everything!r}"
    finally:
        await page.evaluate("clearInterval(window.e2eRateFeed); state.recordFilterDraft.subject_ids = []; saveSettings();")
        await record_close(page, server)


@test("Record: a subject several nodes publish lists them all, at its total rate, and lights up for each")
async def _(page):
    server = _RecordServer()
    await record_open(page, server, setup=RECORD_RATES)  # node 11's Heartbeat comes last
    try:
        await page.wait_for_timeout(2300)  # a picker refresh
        heartbeat = await page.evaluate("_subjectsPicker.getRow('subject-7509').getData()")
        assert (heartbeat["owner"], heartbeat["rate"]) == ("2 nodes", 2), \
            f"Subject 7509, from nodes 10 and 11 at 1 Hz each, reads owner {heartbeat['owner']!r}, rate {heartbeat['rate']!r}"
        await page.locator("#recNodesPicker .tabulator-row", has_text="org.example.imu").click()
        lit = await page.evaluate("""[...document.querySelectorAll('#recSubjectsPicker .tabulator-row.rec-row-highlighted')]
            .map((row) => row.querySelector('.tabulator-cell').textContent)""")
        assert sorted(lit) == ["100", "7509"], f"Node 10 lights up subjects {lit}"
    finally:
        await page.evaluate("clearInterval(window.e2eRateFeed)")
        await record_close(page, server)


@test("Record: the pickers work by keyboard, their refreshes keeping the focus")
async def _(page):
    server = _RecordServer()
    await record_open(page, server, setup=RECORD_RATES)
    try:
        await page.locator("#recSubjectsPicker .tabulator-tableholder").focus()
        await page.keyboard.press("ArrowDown")  # the first row: subject 100
        await page.wait_for_timeout(2500)  # the pickers refresh every 2 s
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(300)
        added = await page.evaluate("state.recordFilterDraft.subject_ids")
        assert added == [100], f"Enter on subject 100's row, after a refresh, selected {added}"
        await page.locator("#recSelectionPicker .tabulator-tableholder").focus()
        await page.keyboard.press("ArrowDown")
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(300)
        left = await page.evaluate("state.recordFilterDraft.subject_ids")
        kept = await page.evaluate("el('recSelectionPicker').contains(document.activeElement)")
        assert left == [] and kept, f"Delete left the selection at {left}; the focus stayed in it: {kept}"
    finally:
        await page.evaluate("clearInterval(window.e2eRateFeed); state.recordFilterDraft.subject_ids = []; saveSettings();")
        await record_close(page, server)


@test("Record: a row keeps the keyboard focus when its table is drawn again")
async def _(page):
    server = _RecordServer()
    await record_open(page, server, setup="() => { _addToSelection({kind: 'subject', id: 100}); }")
    try:
        await page.locator("#recSelectionPicker .tabulator-tableholder").focus()
        await page.keyboard.press("ArrowDown")
        await page.wait_for_timeout(200)
        await page.evaluate("_selectionPicker.redraw()")  # as a resize does
        await page.wait_for_timeout(200)
        focused = await page.evaluate("document.activeElement.className")
        assert "tabulator-row" in focused, f"After a redraw the focus is on {focused!r}, not the row"
        await page.keyboard.press("Delete")
        await page.wait_for_timeout(300)
        left = await page.evaluate("state.recordFilterDraft.subject_ids")
        assert left == [], f"Delete after the redraw left the selection at {left}"
    finally:
        await page.evaluate("state.recordFilterDraft.subject_ids = []; saveSettings();")
        await record_close(page, server)


@test("Record: Edit limits takes the focus, keeps Tab inside, and gives the focus back")
async def _(page):
    server = _RecordServer([_recording(1, "field test", live=True)])
    await record_open(page, server)
    try:
        await page.locator('.record-card[data-id="1"] [data-action="edit-limits"]').focus()
        await page.keyboard.press("Enter")
        await page.wait_for_selector("#recEditLimitsBackdrop:not(.hidden)")
        focused = await page.evaluate("document.activeElement.id || document.activeElement.textContent")
        assert focused == "recEditLength", f"Opened, the dialog leaves the focus on {focused!r}"
        await page.locator("#recEditApply").focus()
        await page.keyboard.press("Tab")
        wrapped = await page.evaluate("document.activeElement.id || document.activeElement.textContent")
        assert wrapped == "recEditLength", f"Tab from Apply goes to {wrapped!r}"
        await page.keyboard.press("Escape")
        back = await page.evaluate("document.activeElement.dataset.action || document.activeElement.tagName")
        assert back == "edit-limits", f"Closed, the dialog leaves the focus on {back!r}"
    finally:
        await record_close(page, server)


@test("Record: a recording's progress bars say what they measure, and how far it got")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence")])  # 60 s of a 1 h limit, 120 of 100,000 events
    await record_open(page, server)
    try:
        card = page.locator('.record-card[data-id="1"]')
        read = {}
        for name in ("Time", "Events"):
            bar = card.get_by_role("progressbar", name=name)
            read[name] = await bar.get_attribute("aria-valuetext") if await bar.count() == 1 else None
        assert read == {"Time": "1m 0s / 1h 0m", "Events": "120 / 100,000"}, f"Progress bars by name: {read}"
    finally:
        await record_close(page, server)


@test("Record: the recording dots hold still on request")
async def _(page):
    server = _RecordServer([_recording(1, "field test", live=True)])
    await page.emulate_media(reduced_motion="reduce")
    try:
        await record_open(page, server)
        moving = await page.evaluate("""[getComputedStyle(document.querySelector('.record-card .record-dot.rec')).animationName,
            getComputedStyle(el('viewTabRecord'), '::after').animationName]""")
        assert moving == ["none", "none"], f"With reduced motion asked for, the card's and the tab's dots run {moving}"
    finally:
        await page.emulate_media(reduced_motion="no-preference")
        await record_close(page, server)


@test("Record: Start is at the top of the tab, above the recordings however many they are")
async def _(page):
    server = _RecordServer([_recording(rid, f"run {rid}") for rid in range(1, 21)])
    await record_open(page, server)
    try:
        start = await page.locator("#recStart").bounding_box()
        recordings = await page.locator("#recordList").bounding_box()
        assert start["y"] + start["height"] <= 800 and start["y"] < recordings["y"], \
            f"In a window 800 px high, Start recording is at {start['y']:.0f} px, the recordings at {recordings['y']:.0f} px"
        hint = await page.locator(".record-header").inner_text()
        assert "on the right" not in hint, f"The tab's hint reads {hint!r}"
    finally:
        await record_close(page, server)


@test("Record: a recording needs no name, taking its start time, and the next one starts with an empty name")
async def _(page):
    server = _RecordServer()
    await record_open(page, server)
    try:
        await page.locator("#recName").fill("")
        await page.locator("#recStart").click()
        await page.wait_for_selector(".record-card.live", timeout=WAIT_MS)
        await page.locator("#recName").fill("bench run")
        await page.locator("#recNotes").fill("PWM at 50 %")
        await page.locator("#recStart").click()
        await page.wait_for_timeout(800)
        names = [body["name"] for method, path, body in server.requests if (method, path) == ("POST", "/api/recordings")]
        assert len(names) == 2 and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", names[0]) and names[1] == "bench run", \
            f"Recordings started: {names}"
        left = [await page.locator(f).input_value() for f in ("#recName", "#recNotes")]
        assert left == ["", ""], f"After a start the name and notes read {left}"
    finally:
        await record_close(page, server)


@test("Record: Save the last minutes keeps what the bus sent before it was pressed")
async def _(page):
    server = _RecordServer()
    await record_open(page, server)
    try:
        assert await page.locator("#recSaveLast").count() == 1, "The record bar has no Save the last"
        await page.locator("#recSaveLastSeconds").select_option("300")
        await page.locator("#recSaveLast").click()
        await page.wait_for_selector(".record-card", timeout=WAIT_MS)
        saves = [body for method, path, body in server.requests if (method, path) == ("POST", "/api/recordings/quick")]
        assert len(saves) == 1 and saves[0]["last_seconds"] == 300, f"Saves asked for: {saves}"
        card = await page.locator(".record-card").first.inner_text()
        assert "5m 0s" in card and "42" in card, f"The saved recording's card reads {card!r}"
    finally:
        await record_close(page, server)


# Node 10 serves register access (384), node 11 calls it, and 384 is selected.
RECORD_SERVICE = """() => {
    state.latestNodesPayload = {node_count: 2, nodes: {
        '10': {node_id: 10, name: 'org.example.imu', publishers: [], subscribers: [], servers: [384], clients: []},
        '11': {node_id: 11, name: 'org.example.esc', publishers: [], subscribers: [], servers: [], clients: [384]}}};
    state.recordFilterDraft.service_ids = [384];
}"""


@test("Record: a service says it records all its calls on the bus")
async def _(page):
    server = _RecordServer()
    await record_open(page, server, setup=RECORD_SERVICE)
    try:
        said = "all its calls on the bus"
        picked = await page.evaluate("_subjectsPicker.getRow('service-384').getCell('label').getElement().title")
        selected = await page.evaluate("_selectionPicker.getRow('service-384').getData().label")
        heading = await page.evaluate("el('recSubjectsPicker').previousElementSibling.textContent")
        assert said in picked and said in selected and said in heading, \
            f"Service 384 reads {picked!r} hovered in the picker, {selected!r} in the selection, " \
            f"and the picker's heading {heading!r}"
    finally:
        await page.evaluate("state.recordFilterDraft.service_ids = []; saveSettings();")
        await record_close(page, server)


@test("Record: a recording's name is shown whole, its times under it")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence"),
                            _recording(2, "ESC thermal soak, node 12, 8 h at full PWM", auto_stopped=True)])
    await page.set_viewport_size({"width": 1440, "height": 900})  # the recordings take a narrow column
    try:
        await record_open(page, server)
        cut = await page.evaluate("""[...document.querySelectorAll('.record-card .record-name')]
            .filter((name) => name.scrollWidth > name.clientWidth + 1).map((name) => name.textContent)""")
        assert not cut, f"Names cut short: {cut}"
    finally:
        await page.set_viewport_size({"width": 1280, "height": 800})
        await record_close(page, server)


@test("Record: an auto-stopped recording says which limit stopped it, in amber only for the events cap")
async def _(page):
    now = time.time()
    server = _RecordServer([
        _recording(1, "capped", start=now - 9000, end_unix=now - 9000 + 600, auto_stopped=True,
                   max_events=1000, event_count=1000),
        _recording(2, "timed", start=now - 5000, end_unix=now - 5000 + 1802, auto_stopped=True,
                   max_length_seconds=1800.0, event_count=50)])
    await record_open(page, server)
    try:
        read = {}
        for rid in (1, 2):
            card = page.locator(f'.record-card[data-id="{rid}"]')
            badge = card.locator(".record-badge")
            read[rid] = (await badge.text_content(), "record-badge-warn" in await badge.get_attribute("class"),
                         "auto-stopped" in await card.get_attribute("class"))
        assert read == {1: ("stopped at 1,000 events", True, True), 2: ("stopped at 30m 0s", False, False)}, \
            f"(badge, amber badge, amber card) by recording: {read}"
    finally:
        await record_close(page, server)


@test("Record: a recording running while CAN is down says it waits for CAN")
async def _(page):
    server = _RecordServer([_recording(1, "overnight", live=True)])
    await record_open(page, server, can=False)
    try:
        card = page.locator('.record-card[data-id="1"]')
        badges = await card.locator(".record-badge").all_text_contents()
        dot = (await card.locator(".record-dot").get_attribute("class")).split()
        assert "waiting for CAN" in badges and "rec" not in dot, f"With CAN down: badges {badges}, dot {dot}"
        await page.evaluate("state.canConnected = true; renderRecordList();")
        badges = await card.locator(".record-badge").all_text_contents()
        dot = (await card.locator(".record-dot").get_attribute("class")).split()
        assert "waiting for CAN" not in badges and "rec" in dot, f"With CAN up: badges {badges}, dot {dot}"
    finally:
        await record_close(page, server)


@test("Record: the recording being replayed is marked on its card")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence"), _recording(2, "motor test")])
    await record_open(page, server, can=False)
    try:
        await page.evaluate("""() => { _applyReplayStatus({active: true, recording_id: 2, position_s: 0, duration_s: 60,
            speed: 1, paused: false, events_emitted: 0, total_events: 120, finished: false}); showReplayStrip(); }""")
        marked = await page.evaluate("[...document.querySelectorAll('.record-card.replaying')].map((c) => c.dataset.id)")
        badges = await page.locator('.record-card[data-id="2"] .record-badge').all_text_contents()
        assert marked == ["2"] and "replaying" in badges, f"Cards marked: {marked}; recording 2's badges: {badges}"
        await page.evaluate("hideReplayStrip()")
        assert not await page.locator(".record-card.replaying").count(), "A card stays marked after the replay"
    finally:
        await page.evaluate("hideReplayStrip()")
        await record_close(page, server)


@test("Record: the two pickers line up, however many lines their headings take")
async def _(page):
    server = _RecordServer()
    await page.set_viewport_size({"width": 1440, "height": 900})  # side by side, headings of different lengths
    try:
        await record_open(page, server)
        tops = await page.evaluate("['recSubjectsPicker', 'recNodesPicker'].map((id) => Math.round(el(id).getBoundingClientRect().top))")
        assert tops[0] == tops[1], f"The subjects and nodes pickers start at {tops}"
    finally:
        await page.set_viewport_size({"width": 1280, "height": 800})
        await record_close(page, server)


@test("Record: a card shows its main actions, the others in a menu that stays open as the card is drawn again")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence")])
    await record_open(page, server)
    try:
        card = page.locator('.record-card[data-id="1"]')
        shown = [text.strip() for text in await card.locator("button:visible, summary:visible").all_inner_texts()]
        assert shown == ["▶ Play", "CSV", "JSONL", "⋯"], f"A stopped recording's card shows {shown}"
        await card.locator("summary").click()
        await page.evaluate("renderRecordList()")  # as every second while something records
        menu = [text.strip() for text in await card.locator(".record-card-menu button:visible").all_inner_texts()]
        assert menu == ["Record again", "Rename", "Delete"], f"Its menu, drawn again, holds {menu}"
        await card.locator('.record-card-menu [data-action="duplicate"]').click()
        await page.wait_for_timeout(500)
        assert server.count("POST", "/api/recordings") == 1, "Record again started no recording"
        assert not await page.locator(".record-card-menu:visible").count(), "The menu stays open after its action"
    finally:
        await record_close(page, server)


# A raw log is running, and one from yesterday is saved.
RAW_LOGS = {"active": {"name": "cynitor-20261008-101500.log", "frames": 1200, "bytes": 64000,
                       "started_unix": 1.79e9, "error": None},
            "logs": [{"name": "cynitor-20261008-101500.log", "bytes": 64000, "modified_unix": 1.79e9},
                     {"name": "cynitor-20261007-091500.log", "bytes": 265000, "modified_unix": 1.79e9}]}
RAW_LOG_SPEED = '#rawLogPanel select[data-rawlog="speed"]'


@test("Record: a raw log's playback speed sits with the saved logs, and is remembered")
async def _(page):
    server = _RecordServer()
    server.rawlogs = RAW_LOGS
    await record_open(page, server)
    try:
        await page.wait_for_selector(RAW_LOG_SPEED, timeout=WAIT_MS)
        assert not await page.locator('.rawlog-active select[data-rawlog="speed"]').count(), \
            "The playback speed sits beside the running log's Stop"
        await page.locator(RAW_LOG_SPEED).select_option("10")
        await record_close(page, server)
        await record_open(page, server)  # the page loaded again
        await page.wait_for_selector(RAW_LOG_SPEED, timeout=WAIT_MS)
        speed = await page.locator(RAW_LOG_SPEED).input_value()
        assert speed == "10", f"Loaded again, the playback speed reads {speed}"
    finally:
        await page.evaluate("state.rawLogPlaybackSpeed = 1; saveSettings();")
        await record_close(page, server)


@test("Record: the Record tab's dot shows while a raw log runs")
async def _(page):
    server = _RecordServer()
    server.rawlogs = RAW_LOGS
    await record_open(page, server)
    try:
        await page.wait_for_selector("#rawLogPanel .rawlog-active .record-dot", timeout=WAIT_MS)
        tab = await page.locator("#viewTabRecord").get_attribute("class")
        assert "recording-active" in tab, f"A raw log runs, and the Record tab reads {tab!r}"
    finally:
        await record_close(page, server)


# The Record tab's style rules that set a length in px other than a 1px line,
# or set anything !important; and what its elements are styled with inline,
# custom properties aside unless in px (as for the DSDL tab, INLINE_STYLING).
RECORD_CSS_FAULTS = r"""() => {
    // A style rule has cssRules too (nesting), and a shorthand using var() has
    // no longhand values: the rules' own text is what is read.
    const rules = [];
    const walk = (list) => { for (const rule of list) {
        if (rule instanceof CSSStyleRule) rules.push(rule);
        if (rule.cssRules) walk(rule.cssRules); } };
    for (const sheet of document.styleSheets) { try { walk(sheet.cssRules); } catch (e) { /* another origin */ } }
    const ruleFaults = rules.filter((rule) => /record|rec-|rawlog/.test(rule.selectorText)
            && /!important|(^|[^.\d])([2-9]|\d\d+)(\.\d+)?px/.test(rule.style.cssText))
        .map((rule) => `${rule.selectorText} { ${rule.style.cssText} }`);
    const inline = [...document.querySelectorAll('#recordContainer [style]')]
        .filter((el) => !el.closest('.tabulator'))  // Tabulator sizes its own parts
        .flatMap((el) => [...el.style]
            .filter((prop) => !prop.startsWith('--') || el.style.getPropertyValue(prop).includes('px'))
            .map((prop) => `${el.className} ${prop}: ${el.style.getPropertyValue(prop)}`));
    return [...ruleFaults, ...inline];
}"""


@test("Record: the tab is styled by its CSS, in rem, without !important")
async def _(page):
    server = _RecordServer([_recording(1, "field test", live=True), _recording(2, "capped", auto_stopped=True,
                                                                               max_events=1000, event_count=1000)])
    await record_open(page, server)
    try:
        faults = await page.evaluate(RECORD_CSS_FAULTS)
        assert not faults, "Styled in px, !important or inline:\n" + "\n".join(faults)
        widths = await page.evaluate("""[...document.querySelectorAll('.record-card[data-id="2"] .rec-bar-fill')]
            .map((fill) => Math.round(fill.getBoundingClientRect().width / fill.parentElement.getBoundingClientRect().width * 100))""")
        assert widths[1] == 100, f"A recording at its events cap fills {widths[1]}% of its Events bar"
    finally:
        await record_close(page, server)


@test("Record: the history chip names the history, and is not drawn while it is unknown")
async def _(page):
    server = _RecordServer()
    server.failures[("GET", "/api/recordings/buffer")] = (500, "database is locked")
    await record_open(page, server)
    try:
        await page.wait_for_timeout(300)
        assert not await page.locator("#recBufferChip").is_visible(), "An empty chip is drawn in the header"
        del server.failures[("GET", "/api/recordings/buffer")]
        await page.evaluate("fetchRecordBuffer()")
        await page.wait_for_timeout(300)
        chip = await page.locator("#recBufferChip").inner_text()
        assert chip.startswith("History"), f"The chip reads {chip!r}"
    finally:
        await record_close(page, server)


RECORD_TYPES = """() => {
    state.latestNodesPayload = {node_count: 2, nodes: {
        '10': {node_id: 10, name: 'org.example.imu', publishers: [100, 7509], subscribers: [], servers: [], clients: []},
        '11': {node_id: 11, name: 'org.example.esc', publishers: [7509], subscribers: [], servers: [], clients: []}}};
    const ev = (subject_id, publisher_node_id, message_type) => ({subject_id, publisher_node_id, message_type,
        rate: 100.5, subject_rate: 100.5, attributes: [], timestamp_unix: Date.now() / 1000});
    cacheEvent(ev(100, 10, 'Real64_1_0')); cacheEvent(ev(7509, 10, 'Heartbeat_1_0')); cacheEvent(ev(7509, 11, 'Heartbeat_1_0'));
}"""

# The headers and cells of a table whose text is cut short.
CUT_SHORT = """(id) => [...el(id).querySelectorAll('.tabulator-col-title, .tabulator-cell')]
    .filter((part) => part.scrollWidth > part.clientWidth).map((part) => part.textContent.trim())"""


async def set_log_panel(page, shown):
    """Opens or closes the log panel with its button; returns whether it was open."""
    was_open = not await page.evaluate("state.logPanelCollapsed")
    if was_open != shown:
        await page.locator("#logPanelCollapseBtn").click()
        await page.wait_for_timeout(300)  # the tables take their new widths
    return was_open


@test("Record: the pickers show types and node names whole, the columns of numbers as wide as theirs")
async def _(page):
    server = _RecordServer()
    await page.set_viewport_size({"width": 1440, "height": 900})  # two pickers side by side, beside the recordings
    log_was_open = None
    try:
        await record_open(page, server, setup=RECORD_TYPES)
        log_was_open = await set_log_panel(page, False)  # the room it takes is another test's
        cut = {picker: await page.evaluate(CUT_SHORT, picker) for picker in ("recSubjectsPicker", "recNodesPicker")}
        assert cut == {"recSubjectsPicker": [], "recNodesPicker": []}, f"Cut short: {cut}"
        hover = await page.evaluate("_subjectsPicker.getRow('subject-7509').getCell('label').getElement().title")
        assert hover == "Heartbeat_1_0", f"Heartbeat's type, hovered, reads {hover!r}"
    finally:
        if log_was_open is not None:
            await set_log_panel(page, log_was_open)
        await page.set_viewport_size({"width": 1280, "height": 800})
        await record_close(page, server)


@test("Record: the tab takes one column when the log panel leaves too little room for two")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence")])
    await page.set_viewport_size({"width": 1440, "height": 900})
    columns = "getComputedStyle(document.querySelector('.record-layout')).gridTemplateColumns.split(' ').length"
    log_was_open = None
    try:
        await record_open(page, server, setup=RECORD_TYPES)
        log_was_open = await set_log_panel(page, False)
        beside = await page.evaluate(columns)
        await set_log_panel(page, True)
        under = await page.evaluate(columns)
        cut = {picker: await page.evaluate(CUT_SHORT, picker) for picker in ("recSubjectsPicker", "recNodesPicker")}
        sideways = await page.evaluate("el('recordContainer').scrollWidth > el('recordContainer').clientWidth")
        assert (beside, under) == (2, 1) and cut == {"recSubjectsPicker": [], "recNodesPicker": []} and not sideways, \
            f"Columns with the log panel shut, then open: {beside}, {under}; then cut short: {cut}; scrolls sideways: {sideways}"
    finally:
        if log_was_open is not None:
            await set_log_panel(page, log_was_open)
        await page.set_viewport_size({"width": 1280, "height": 800})
        await record_close(page, server)


@test("Record: a service's row in the picker is not cut short")
async def _(page):
    server = _RecordServer()
    await page.set_viewport_size({"width": 1440, "height": 900})
    try:
        await record_open(page, server, setup=RECORD_SERVICE)
        cut = await page.evaluate(CUT_SHORT, "recSubjectsPicker")
        assert not cut, f"Cut short: {cut}"
    finally:
        await page.evaluate("state.recordFilterDraft.service_ids = []; saveSettings();")
        await page.set_viewport_size({"width": 1280, "height": 800})
        await record_close(page, server)


@test("Record: an empty picker says why, and what to do, in the tab's own text")
async def _(page):
    server = _RecordServer()
    await record_open(page, server, can=False)
    try:
        said = """['recSubjectsPicker', 'recNodesPicker'].map((id) =>
            el(id).querySelector('.tabulator-placeholder').innerText.replace(/\\s+/g, ' ').trim())"""
        down = await page.evaluate(said)
        assert all("Connect a CAN interface" in words for words in down), f"With CAN down, the pickers say {down}"
        await page.evaluate("state.canConnected = true; _refreshPickerTables();")
        quiet = await page.evaluate(said)
        assert quiet[0].startswith("Waiting for traffic") and quiet[1].startswith("Waiting for nodes"), \
            f"With CAN up and nothing sent yet, they say {quiet}"
        drawn = await page.evaluate("""(() => { const style = getComputedStyle(
            el('recSelectionPicker').querySelector('.tabulator-placeholder-contents'));
            return [parseFloat(style.fontSize) / parseFloat(getComputedStyle(document.documentElement).fontSize), style.fontWeight]; })()""")
        assert drawn == [0.8125, "400"], f"An empty selection's words are {drawn[0]}rem, weight {drawn[1]}"
    finally:
        await record_close(page, server)


@test("Record: the search lists the recordings whose name, notes or selection hold what is typed, and the one being made")
async def _(page):
    server = _RecordServer([_recording(1, "boot sequence", notes="The IMU came up late"),
                            _recording(2, "motor test", filter={"node_ids": [12]}),
                            _recording(3, "field test", live=True)])
    await record_open(page, server)
    listed = "[...document.querySelectorAll('#recordList .record-name')].map((name) => name.textContent)"
    try:
        found = {}
        for typed in ("imu", "Node: 12", "nothing like it"):
            await page.locator("#recSearch").fill(typed, timeout=WAIT_MS)
            found[typed] = (await page.evaluate(listed), await page.locator("#recSearchCount").inner_text())
        assert found == {"imu": (["field test", "boot sequence"], "2 of 3"),
                         "Node: 12": (["field test", "motor test"], "2 of 3"),
                         "nothing like it": (["field test"], "1 of 3")}, f"Searched for, listed: {found}"
        await page.locator("#recSearch").fill("")
        assert len(await page.evaluate(listed)) == 3, "An empty search does not list every recording"
    finally:
        await record_close(page, server)


@test("Replay strip: an hour-long replay reads h:mm:ss, its counters are not read out each second, and its end counts every event")
async def _(page):
    try:
        await page.evaluate("""() => {
            state.recordings = [{id: 1, name: 'long soak'}];
            _applyReplayStatus({active: true, recording_id: 1, position_s: 300, duration_s: 7480, speed: 1,
                                paused: false, events_emitted: 1499, total_events: 1500, finished: false});
            showReplayStrip(); }""")
        shown = await page.locator("#replayTime").inner_text()
        assert shown == "0:05:00 / 2:04:40", f"The strip's time reads {shown!r}"
        live = await page.evaluate("[...document.querySelectorAll('#replayStrip [aria-live]')].map((e) => e.id)")
        assert live == ["replayStripLabel"], f"Read out as they change: {live}"
        # The replay's end, through the handler the WebSocket's messages go to.
        await page.evaluate("""() => { connectWs();
            state.ws.onmessage({data: JSON.stringify({type: 'replay_ended', recording_id: 1, finished: true})}); }""")
        await page.wait_for_selector("#replayStrip.replay-strip-finished", timeout=WAIT_MS)
        events = await page.locator("#replayEvents").inner_text()
        assert events.startswith("1,500 / 1,500"), f"At its end the strip counts {events!r}"
    finally:
        await page.evaluate("disconnectWs(); hideReplayStrip(); state.recordings = [];")


# ── Debug tab ──
#
# _DebugServer answers /api/* as the backend does with a CAN session up on
# vcan0, where frames come in at exactly 100 a second, so a rate the tab
# shows can be checked; it also plays the server end of /ws. Each test opens
# the tab on a freshly loaded page.

# What get_can_link_diagnostics finds on a vcan: no controller to report on.
VCAN_LINK = {"operstate": "unknown", "state": None, "bitrate": None, "dbitrate": None, "berr_tx": None,
             "berr_rx": None, "restart_ms": None, "restarts": None, "bus_errors": None, "arbitration_lost": None,
             "error_warning": None, "error_passive": None, "bus_off": None}


class _DebugServer:
    """The backend's /api/* and /ws for the Debug tab, over a CAN session on vcan0."""

    def __init__(self):
        self.started = time.monotonic()
        self.requests = []          # (monotonic time, path)
        self.answers = {}           # path -> (JSON, status) given instead of the usual answer
        self.ws = None              # the server end of the dashboard's socket
        self.received = []          # what the dashboard sent on it
        self.ring = []              # frames captured, oldest first
        self.queued = None          # captured since the dashboard subscribed, not sent yet (None: not subscribed)
        self.right_after = []       # frames the bus carries the moment the dashboard subscribes
        self.can = True             # a CAN session is up
        self.capturing = False      # its transport capture is on (it stays on until CAN disconnects)
        self.link = VCAN_LINK       # what the transport diagnostics say of the controller (or a function giving it)
        self.dropped = 0            # frames the stream lost to a full queue

    def _transport(self):
        frames_in = 1000 + int(100 * (time.monotonic() - self.started))
        stats = {"in_frames": frames_in, "in_frames_cyphal": frames_in, "in_frames_cyphal_accepted": frames_in,
                 "in_frames_errored": 0, "in_frames_loopback": 0, "out_frames": 40, "out_frames_timeout": 0,
                 "out_frames_loopback": 0, "media_acceptance_filtering_efficiency": 1.0, "lost_loopback_frames": 0}
        return {"connected": True, "interface": "vcan0", "statistics": stats, "capture_active": self.capturing,
                "protocol": {"mtu": 7, "transfer_id_modulo": 32, "max_nodes": 128, "is_fd": False},
                "link": self.link() if callable(self.link) else self.link, "bus_utilization": 12.0}

    def _answer(self, path):
        if path in self.answers:
            return self.answers[path]
        if path == "/api/status":
            return {"status": "running" if self.can else "idle", "can_interface": "vcan0" if self.can else None,
                    "can_bitrate": None, "can_data_bitrate": None, "can_fd": False,
                    "available_interfaces": ["vcan0"], "available_adapters": [
                        {"interface": "vcan0", "label": "vcan0 (SocketCAN)", "needs_bitrate": False}],
                    "bus_utilization": 12.0 if self.can else None, "dropped": None, "last_error": None,
                    "cyphal_v11": None}, 200
        if path == "/api/can/transport":
            return (self._transport() if self.can else {"connected": False}), 200
        if path == "/api/can/capture":
            return {"active": True, "stats": self._stats(), "frames": self.ring[-500:]}, 200
        quiet = {"/api/nodes": {"node_count": 0, "nodes": {}}, "/api/replay/status": {"active": False},
                 "/api/recordings": {"recordings": []}, "/api/rawlogs": {"active": None, "logs": []}}
        return (quiet[path], 200) if path in quiet else ({"error": "Not found"}, 404)

    def _stats(self):
        n, errors = len(self.ring), sum(1 for frame in self.ring if frame.get("error"))
        return {"active": True, "captured": n, "rx": n, "tx": 0, "cyphal": n - errors, "foreign": 0, "errors": errors,
                "dropped": self.dropped}

    def capture(self, frames):
        """Frames the bus carried: into the ring, and queued for a subscribed dashboard."""
        self.ring += frames
        if self.queued is not None:
            self.queued += frames

    def flush(self):
        """What is queued, as one can_frame batch, as _capture_loop sends it."""
        if self.queued is None:  # the dashboard is not subscribed: nothing goes to it
            return
        frames, self.queued = self.queued, []
        self.ws.send(json.dumps({"type": "can_frame", "frames": frames, "stats": self._stats()}))

    async def handle(self, route):
        path = unquote(urlparse(route.request.url).path)
        self.requests.append((time.monotonic(), path))
        answer, status = self._answer(path)
        await route.fulfill(json=answer, status=status, headers={"Access-Control-Allow-Origin": "*"})

    def on_socket(self, ws):
        self.ws = ws
        ws.on_message(lambda message: self._on_message(ws, json.loads(message)))

    def _on_message(self, ws, msg):
        """A `capture` message, answered as _handle_capture_message does."""
        self.received.append(msg)
        if msg.get("type") != "capture":
            return
        if not msg.get("enabled"):
            self.queued = None
            ws.send(json.dumps({"type": "capture_status", "active": False, "capturing": self.capturing,
                                "forwarding": False}))
            return
        if not self.can:
            ws.send(json.dumps({"type": "capture_status", "active": False, "capturing": False,
                                "forwarding": False, "error": "CAN not connected"}))
            return
        earlier = self.ring[-500:] if self.queued is None else []
        if self.queued is None:
            self.queued = []
        self.capturing = True
        ws.send(json.dumps({"type": "capture_status", "active": True, "capturing": True, "forwarding": True,
                            "stats": self._stats(), "frames": earlier}))
        self.capture(self.right_after)

    def polls(self, since):
        """How often the tab asked for the diagnostics since `since` (monotonic time)."""
        return sum(1 for t, path in self.requests if path == "/api/can/transport" and t >= since)


# The page's /ws reaches the server of the Debug test that runs. Playwright
# cannot take a socket route back, so it is made once and follows this.
_debug_server = None
_debug_socket_routed = False


async def debug_open(page, server, restore=False, connected=True):
    """The Debug tab on a freshly loaded page, served by `server`. With restore
    the page loads on the tab with a connected dashboard saved, as after a
    reload; otherwise it loads on Nodes, connects (or not), and opens the tab."""
    global _debug_server, _debug_socket_routed
    _debug_server = server
    if not _debug_socket_routed:
        _debug_socket_routed = True
        await page.route_web_socket("**/ws**", lambda ws: _debug_server and _debug_server.on_socket(ws))
    await page.route("**/api/**", server.handle)
    await page.evaluate(f"""() => {{ state.dashboardConnected = {json.dumps(restore)};
        state.activeView = {json.dumps('debug' if restore else 'nodes')}; _writeSettingsNow(); }}""")
    await page.reload(wait_until="load")
    if not restore:
        if connected:
            await page.locator("#connectDashboardBtn").click()
            await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        await page.evaluate("switchView('debug')")
    if restore or connected:
        await page.wait_for_function("state.ws?.readyState === 1", timeout=5000)


async def debug_close(page, server):
    global _debug_server
    await page.evaluate("() => { switchView('nodes'); disconnectAll(); }")
    _debug_server = None
    await page.unroute("**/api/**", server.handle)


# The value a diagnostics card shows for `label`.
DEBUG_STAT = """(label) => [...document.querySelectorAll('#debugBody .debug-stat')]
    .find(row => row.querySelector('.debug-stat-label').textContent === label)
    ?.querySelector('.debug-stat-val').textContent"""


@test("Debug: a page reopened on the tab asks once a second, and shows the bus's own frame rate")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server, restore=True)
    try:
        await page.wait_for_selector("#debugBody .debug-card", state="attached", timeout=WAIT_MS)
        await page.wait_for_timeout(500)  # the connect that follows the reload has run
        since, read = time.monotonic(), []
        for _ in range(10):
            await page.wait_for_timeout(300)
            read.append(await page.evaluate(DEBUG_STAT, "Frames in"))
        polls = server.polls(since)
        rates = [int(m[1]) for text in read if (m := re.search(r"\(\+(\d+)/s\)", text or ""))]
        assert polls <= 4 and rates and all(90 <= rate <= 110 for rate in rates), \
            f"In 3 s the tab asked {polls} times; at 100 frames/s, Frames in read {sorted(set(read))}"
    finally:
        await debug_close(page, server)


@test("Debug: disconnected, the tab asks the backend nothing and says why, so a token can be typed in peace")
async def _(page):
    server = _DebugServer()
    for path in ("/api/status", "/api/nodes", "/api/can/transport"):  # a backend that wants a token
        server.answers[path] = ({"error": "Missing or invalid token"}, 401)
    await debug_open(page, server, connected=False)
    try:
        await page.wait_for_timeout(2500)
        asked, said = server.polls(0), await page.locator("#debugBody").inner_text()
        await page.evaluate("connectDashboard()")  # Connect, which the token prompt answers
        await page.locator("#authModalInput").fill("pasted-token")
        await page.wait_for_timeout(2000)  # reaching for "Save and retry"
        kept = await page.locator("#authModalInput").input_value()
        assert asked == 0 and "Not connected to backend" in said and kept == "pasted-token", \
            f"Disconnected, the tab asked for the diagnostics {asked} times and said {said!r}; " \
            f"2 s after pasting, the token field holds {kept!r}"
    finally:
        await page.evaluate("hideAuthModal()")
        await debug_close(page, server)


@test("Debug: the diagnostics cards show every row, and scroll when the window is short")
async def _(page):
    server = _DebugServer()
    await page.set_viewport_size({"width": 1024, "height": 800})
    await debug_open(page, server)
    try:
        await page.wait_for_selector("#debugBody .debug-card", state="attached", timeout=WAIT_MS)
        await page.locator("#debugDetails summary").click()  # folded beneath the health strip
        cut = await page.evaluate("""[...document.querySelectorAll('#debugBody .debug-card')]
            .map(card => card.scrollHeight - card.clientHeight)""")
        panel = await page.evaluate("[el('debugBody').scrollHeight, el('debugBody').clientHeight]")
        assert cut and max(cut) <= 1 and panel[0] > panel[1], \
            f"At 1024×800, rows cut off the bottom of each card (px): {cut}; the panel's scroll and visible height: {panel}"
    finally:
        await page.set_viewport_size({"width": 1280, "height": 800})
        await debug_close(page, server)


async def debug_start_capture(page):
    await page.locator("#fmToggle").click()
    await page.wait_for_function("el('fmToggle').classList.contains('active')", timeout=WAIT_MS)


def _can_frame(n, src=42):
    """Captured frame n, as serialize_capture makes it: a message from node src, n ms into the capture."""
    return {"t": 1000 + n / 1000, "ts": 1.76e9 + n / 1000, "dir": "rx", "id": f"0x{0x107D5500 + n % 256:08X}",
            "ext": True, "dlc": 8, "data": "01 02 03 04 05 06 07 E5", "cyphal": True, "priority": "NOMINAL",
            "src": src, "dst": None, "transfer_id": n % 32, "start": True, "end": True, "toggle": True, "kind": "msg",
            "port": 7509}


# The frames the table shows, newest first, known by their time (each is a different millisecond).
DEBUG_ROWS = "[...document.querySelectorAll('#fmRows tr')].map(row => row.querySelector('.fm-time').textContent)"


@test("Debug: a capture whose connection closes stops, and says so")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        await server.ws.close(code=1012, reason="backend restarting")
        await page.wait_for_timeout(500)
        button, said = await page.locator("#fmToggle").inner_text(), await page.locator("#fmStatus").inner_text()
        assert button == "Start capture" and "Capture stopped" in said, \
            f"Once the socket closed, the button reads {button!r} and the status says {said!r}"
    finally:
        await debug_close(page, server)


@test("Debug: Start capture shows the frames caught before it, then the live ones, each once")
async def _(page):
    server = _DebugServer()
    server.capture([_can_frame(n) for n in range(10)])
    server.right_after = [_can_frame(n) for n in range(10, 13)]
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        await page.wait_for_timeout(500)  # anything the tab asks for besides is answered
        server.flush()                    # the first live batch
        await page.wait_for_timeout(300)
        shown = await page.evaluate(DEBUG_ROWS)
        assert len(shown) == 13 and len(set(shown)) == 13, \
            f"13 frames came, the table has {len(shown)} rows, {len(shown) - len(set(shown))} of them repeats"
    finally:
        await debug_close(page, server)


async def debug_frames_come(page, server, numbers):
    server.capture([_can_frame(n) for n in numbers])
    server.flush()
    await page.wait_for_timeout(300)


@test("Debug: what comes while the frame table is paused shows on Resume")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        await debug_frames_come(page, server, range(5))
        await page.locator("#fmPause").click()
        await debug_frames_come(page, server, range(5, 10))
        held = len(await page.evaluate(DEBUG_ROWS))
        await page.locator("#fmPause").click()  # Resume
        await debug_frames_come(page, server, range(10, 15))
        shown = await page.evaluate(DEBUG_ROWS)
        assert held == 5 and len(shown) == 15 and shown == sorted(shown, reverse=True), \
            f"Paused, the table held {held} rows (5 came before Pause); after Resume it shows {len(shown)} of the 15"
    finally:
        await debug_close(page, server)


@test("Debug: the frame filter shows the same rows whenever it is typed, and says when none match")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        await page.locator("#fmFilter").fill("n42")
        for first in range(0, 6000, 250):  # one frame in a hundred from node 42, in the backend's batches
            server.capture([_can_frame(n, src=42 if n % 100 == 0 else 7) for n in range(first, first + 250)])
            server.flush()
        await page.wait_for_timeout(800)
        live = len(await page.evaluate(DEBUG_ROWS))
        await page.locator("#fmFilter").press("End")
        await page.locator("#fmFilter").press("Space")  # the same filter, typed again
        await page.wait_for_timeout(300)
        again = len(await page.evaluate(DEBUG_ROWS))
        await page.locator("#fmFilter").fill("no such frame")
        said = await page.locator("#fmEmpty").inner_text()
        assert live == again and said == "No frames match the filter.", \
            f"Filtered on n42, {live} rows as the frames came and {again} once typed again; " \
            f"a filter nothing matches says {said!r}"
    finally:
        await debug_close(page, server)


@test("Debug: screen readers are not read the frame counters on every batch")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        await page.evaluate("""() => { window.e2eSaid = [];  // what live regions would read out
            document.querySelectorAll('.frame-monitor [aria-live]:not([aria-live=off]), .frame-monitor [role=status]')
                .forEach((node) => new MutationObserver(() => e2eSaid.push(node.id))
                    .observe(node, {childList: true, characterData: true, subtree: true})); }""")
        for first in range(0, 25, 5):
            await debug_frames_come(page, server, range(first, first + 5))
        said = await page.evaluate("e2eSaid")
        assert not said, f"In 5 batches of frames, live regions changed {len(said)} times: {sorted(set(said))}"
    finally:
        await debug_close(page, server)


@test("Debug: Start capture is off, saying why, until there is a bus to capture from")
async def _(page):
    server = _DebugServer()
    server.can = False
    await debug_open(page, server)
    start, empty = page.locator("#fmToggle"), page.locator("#fmEmpty")
    try:
        can_idle = (await start.is_disabled(), await empty.inner_text())
        server.can = True
        await page.evaluate("state.canConnected = true; updateSemaphores();")
        can_up = await start.is_disabled()
        await page.evaluate("disconnectWs(); updateSemaphores();")  # as in the 5 s before the socket opens
        no_socket = (await start.is_disabled(), await start.get_attribute("title") or "")
        assert can_idle == (True, "Connect a CAN interface to capture frames.") and not can_up \
            and no_socket[0] and "connection" in no_socket[1], \
            f"Start disabled, and why: with CAN idle {can_idle}; with CAN up {can_up}; with no socket yet {no_socket}"
    finally:
        await debug_close(page, server)


@test("Debug: capture changes nothing on the bus: Stop ends it, and nothing says the bus stays in capture mode")
async def _(page):
    server = _DebugServer()
    server.capturing = True  # another dashboard captures, which leaves this one as it was
    await debug_open(page, server)
    try:
        await page.wait_for_timeout(1200)  # the diagnostics have answered
        idle = await page.locator("#fmEmpty").inner_text()
        await debug_start_capture(page)
        label = await page.locator("#fmToggle").inner_text()
        await page.locator("#fmToggle").click()  # Stop
        await page.wait_for_function("!el('fmToggle').classList.contains('active')", timeout=WAIT_MS)
        stopped = await page.locator("#fmToggle").inner_text()
        monitor = await page.locator(".frame-monitor").inner_text()
        assert idle.startswith("Capture is off") and label == "Stop" and stopped == "Start capture" \
            and "until CAN disconnect" not in monitor, \
            f"Before Start the table says {idle!r}; capturing, the button reads {label!r}, stopped {stopped!r}; " \
            f"the monitor says: {monitor[:200]!r}"
    finally:
        await debug_close(page, server)


@test("Debug: the frame table keeps its frames, filter and pause across tabs, and takes in what came meanwhile")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        await debug_frames_come(page, server, range(5))
        await page.locator("#fmFilter").fill("n42")
        await page.evaluate("switchView('nodes')")
        await debug_frames_come(page, server, range(5, 10))  # while another tab is shown
        await page.evaluate("switchView('debug')")
        back = (len(await page.evaluate(DEBUG_ROWS)), await page.locator("#fmFilter").input_value(),
                await page.locator("#fmToggle").inner_text())
        await page.locator("#fmPause").click()
        await page.evaluate("switchView('nodes'); switchView('debug')")
        still_paused = await page.locator("#fmPause").get_attribute("aria-pressed")
        assert back == (10, "n42", "Stop") and still_paused == "true", \
            f"Back on the tab, (rows, filter, button) are {back}; paused before leaving, aria-pressed={still_paused}"
    finally:
        await debug_close(page, server)


# Each diagnostics row, label: value.
DEBUG_STATS = """Object.fromEntries([...document.querySelectorAll('#debugBody .debug-stat')].map(
    row => [row.querySelector('.debug-stat-label').textContent, row.querySelector('.debug-stat-val').textContent]))"""


@test("Debug: the diagnostics show what the interface reports, in the sidebar's units, and nothing it does not")
async def _(page):
    server = _DebugServer()
    server.link = {"bitrate": 500000, "dbitrate": None, "adapter_frames_in": 123456, "adapter_frames_out": 789,
                   "adapter_send_failures": 0, "adapter_error_frames": 17}  # what the hub knows of an adapter
    await debug_open(page, server)
    try:
        await page.wait_for_selector("#debugBody .debug-card", state="attached", timeout=WAIT_MS)
        shown = await page.evaluate(DEBUG_STATS)
        blank = [label for label, value in shown.items() if value == "—"]
        assert not blank and shown.get("Arbitration bitrate") == "500 kbit/s" \
            and (shown.get("Adapter frames in") or "").startswith("123456"), \
            f"Rows with nothing to say: {blank}; bitrate {shown.get('Arbitration bitrate')!r}; " \
            f"the adapter's frames in {shown.get('Adapter frames in')!r}"
    finally:
        await debug_close(page, server)


@test("Debug: the diagnostics update in place, and a failed update keeps the last values, marked stale")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await page.wait_for_selector("#debugBody .debug-card", state="attached", timeout=WAIT_MS)
        await page.locator("#debugDetails summary").click()  # folded beneath the health strip
        await page.evaluate("""() => { const value = [...document.querySelectorAll('#debugBody .debug-stat')]
            .find(row => row.textContent.includes('Interface')).querySelector('.debug-stat-val');
            const range = document.createRange(); range.selectNodeContents(value);
            getSelection().removeAllRanges(); getSelection().addRange(range); }""")
        await page.wait_for_timeout(1300)  # a poll has answered
        kept = await page.evaluate("getSelection().toString()")
        server.answers["/api/can/transport"] = ({"error": "Internal Server Error"}, 500)
        await page.wait_for_timeout(1300)
        stale = await page.evaluate("""() => ({cards: document.querySelectorAll('#debugBody .debug-card').length,
            said: document.querySelector('#debugBody .debug-stale-note')?.textContent || ''})""")
        assert kept == "vcan0" and stale["cards"] == 3 and "failed" in stale["said"], \
            f"Selected 'vcan0', a poll later the selection is {kept!r}; after a failed poll: {stale}"
    finally:
        await debug_close(page, server)


@test("Debug: colour marks only the unusual, in the theme's own colours: own frames, lost frames, a bus in trouble")
async def _(page):
    server = _DebugServer()
    server.link = dict(VCAN_LINK, operstate="up", state="ERROR-ACTIVE")  # a controller doing fine
    server.dropped = 3
    await debug_open(page, server)
    try:
        await page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
        await debug_start_capture(page)
        server.capture([_can_frame(1), dict(_can_frame(2), dir="tx")])
        server.flush()
        await page.wait_for_timeout(1300)  # and the diagnostics have answered
        seen = await page.evaluate("""() => {
            const token = (name) => { const probe = document.createElement('span');
                probe.style.color = `var(${name})`; document.body.append(probe);
                const color = getComputedStyle(probe).color; probe.remove(); return color; };
            const style = (selector) => getComputedStyle(document.querySelector(selector));
            const state = [...document.querySelectorAll('#debugBody .debug-stat')]
                .find(row => row.textContent.includes('CAN state')).querySelector('.debug-stat-val');
            const dropped = document.querySelector('#fmCounters .fm-dropped');
            return {
                'own frames: the dark theme\\'s ok-muted': style('.fm-dir.fm-tx').backgroundColor === token('--ok-muted'),
                'frames received: plain': style('.fm-dir.fm-rx').color === token('--muted'),
                'ERROR-ACTIVE: plain': getComputedStyle(state).color === token('--text-bright'),
                'dropped: warning': !!dropped && getComputedStyle(dropped).color === token('--warn'),
            }; }""")
        assert all(seen.values()), f"Each colour as it should be: {seen}"
    finally:
        await debug_close(page, server)


# Each figure of the health strip, label: [value, colour].
DEBUG_HEALTH = """Object.fromEntries([...document.querySelectorAll('#debugHealth .debug-tile')].map(tile => [
    tile.querySelector('.debug-tile-label').textContent, [tile.querySelector('.debug-tile-val').textContent,
    tile.classList.contains('debug-tile-error') ? 'error' : tile.classList.contains('debug-tile-warn') ? 'warn' : '']]))"""


@test("Debug: the bus's health comes first, in one strip, with the details folded beneath it")
async def _(page):
    server = _DebugServer()
    since = time.monotonic()
    server.link = lambda: dict(VCAN_LINK, operstate="up", state="ERROR-PASSIVE", bitrate=1000000, berr_tx=136,
                               berr_rx=5, bus_off=1, bus_errors=1200 + int(30 * (time.monotonic() - since)))
    await debug_open(page, server)
    try:
        await page.wait_for_timeout(2300)  # two answers: the rates are known
        health = await page.evaluate(DEBUG_HEALTH)
        folded = await page.evaluate("el('debugDetails') ? !el('debugDetails').open : 'no fold'")
        table_at = await page.evaluate("el('fmRows').closest('.frame-monitor').getBoundingClientRect().top / innerHeight")
        frames_in = (health.get("Frames in") or [""])[0]
        assert health.get("Controller") == ["ERROR-PASSIVE", "warn"] and health.get("Error counters") == ["136 / 5", "error"] \
            and health.get("Bus-off") == ["1", "error"] and (health.get("Bus errors") or ["", ""])[1] == "warn" \
            and re.fullmatch(r"(9\d|10\d|110)/s", frames_in) and folded is True and table_at < 0.4, \
            f"The strip shows {health}; the details folded: {folded}; the frame table starts {table_at:.0%} down the window"
    finally:
        await debug_close(page, server)


# Each row of the frame table, newest first, as {column heading: cell text}; a
# cell across several columns goes under the first.
DEBUG_CELLS = """(() => { const heads = [...document.querySelectorAll('#fmTable thead th')].map(th => th.textContent);
    return [...document.querySelectorAll('#fmRows tr')].map((row) => { let column = 0;
        return Object.fromEntries([...row.cells].map((cell) => {
            const head = heads[column]; column += cell.colSpan; return [head, cell.textContent.trim()]; })); }); })()"""


@test("Debug: the frame table writes a standard ID in three digits, and gives a Cyphal frame's priority, transfer-ID and tail bits")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        first = dict(_can_frame(1), priority="FAST", transfer_id=5, end=False)                # a transfer's first frame
        last = dict(_can_frame(2), priority="FAST", transfer_id=5, start=False, toggle=False)  # and its last
        standard = {"t": 1000.003, "ts": 1.76e9 + 0.003, "dir": "rx", "id": "0x00000123", "ext": False, "dlc": 2,
                    "data": "AA BB", "cyphal": False}
        server.capture([first, last, standard])
        server.flush()
        await page.wait_for_timeout(300)
        shown = [(row.get("CAN ID"), row.get("Prio"), row.get("TID"), row.get("Flags"))
                 for row in await page.evaluate(DEBUG_CELLS)]
        await page.locator("#fmFilter").fill("0x123")
        found = [row.get("CAN ID") for row in await page.evaluate(DEBUG_CELLS)]
        assert shown == [("0x123", "", "", ""), ("0x107D5502", "fast", "5", "-E-"), ("0x107D5501", "fast", "5", "S-T")] \
            and found == ["0x123"], \
            f"(CAN ID, Prio, TID, Flags) of each row, newest first: {shown}; filtered on 0x123: {found}"
    finally:
        await debug_close(page, server)


@test("Debug: the frame filter takes node:, port:, dir: and the like, and a leading - leaves frames out")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        server.capture([_can_frame(1, src=42), _can_frame(2, src=4),  # heartbeats from nodes 42 and 4
                        dict(_can_frame(3, src=12), kind="req", dst=42, port=430),
                        dict(_can_frame(4, src=42), kind="resp", dst=12, port=430, dir="tx"),
                        {"t": 1000.005, "ts": 1.76e9 + 0.005, "dir": "rx", "id": "0x00000123", "ext": False, "dlc": 2,
                         "data": "AA BB", "cyphal": False}])
        server.flush()
        await page.wait_for_timeout(300)
        found = {}
        for typed in ("node:42", "port:7509 -node:42", "dir:tx", "-foreign kind:req", "node:", "02 03 04"):
            await page.locator("#fmFilter").fill(typed)
            found[typed] = sorted(row["CAN ID"][-2:] for row in await page.evaluate(DEBUG_CELLS))
        assert found == {"node:42": ["01", "03", "04"], "port:7509 -node:42": ["02"], "dir:tx": ["04"],
                         "-foreign kind:req": ["03"], "node:": ["01", "02", "03", "04", "23"],
                         "02 03 04": ["01", "02", "03", "04"]}, \
            f"The rows each filter shows, by the end of their CAN ID: {found}"
    finally:
        await debug_close(page, server)


# The data of the frame rows fully in view (under the table's heading), top to bottom.
DEBUG_IN_VIEW = """(() => { const wrap = document.querySelector('.fm-table-wrap').getBoundingClientRect();
    const top = document.querySelector('.fm-table thead').getBoundingClientRect().bottom;
    return [...document.querySelectorAll('#fmRows tr')].filter((row) => { const box = row.getBoundingClientRect();
        return box.top >= top - 0.5 && box.bottom <= wrap.bottom + 0.5; }).map((row) => row.querySelector('.fm-data').textContent); })()"""


@test("Debug: every frame kept can be scrolled to, the page drawing only the rows in view, which hold still as frames come")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    numbered = lambda n: dict(_can_frame(n), dlc=2, data=f"{n >> 8:02X} {n & 255:02X}")  # its number in its data
    try:
        await debug_start_capture(page)
        for first in range(0, 5000, 1000):  # as many as the table keeps
            server.capture([numbered(n) for n in range(first, first + 1000)])
            server.flush()
        await page.wait_for_timeout(500)
        drawn = await page.evaluate("document.querySelectorAll('#fmRows tr').length")
        await page.evaluate("document.querySelector('.fm-table-wrap').scrollTop = 1e9")
        await page.wait_for_timeout(300)
        oldest = (await page.evaluate(DEBUG_IN_VIEW))[-1:]
        await page.evaluate("document.querySelector('.fm-table-wrap').scrollTop = 30000")  # among older frames
        await page.wait_for_timeout(300)
        reading = await page.evaluate(DEBUG_IN_VIEW)
        server.capture([numbered(n) for n in range(5000, 5100)])
        server.flush()
        await page.wait_for_timeout(300)
        still = await page.evaluate(DEBUG_IN_VIEW)
        assert drawn < 100 and oldest == ["00 00"] and reading and still == reading, \
            f"With 5,000 frames kept, the page holds {drawn} rows; scrolled to the bottom, the last row is {oldest}; " \
            f"frames coming while older ones are read move them from {reading[:2]}… to {still[:2]}…"
    finally:
        await debug_close(page, server)


@test("Debug: each frame says how long after the frame shown before it it came, to the µs: with a filter, a subject's period")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        await debug_start_capture(page)
        # 1 ms apart (see _can_frame), node 42's every 10 ms.
        server.capture([dict(_can_frame(n, src=42 if n % 10 == 0 else 7), t=1000 + n / 1000 + 0.000003 * (n == 20))
                        for n in range(21)])
        server.flush()
        await page.wait_for_timeout(300)
        gaps = [row.get("Δ ms") for row in await page.evaluate(DEBUG_CELLS)]
        await page.locator("#fmFilter").fill("src:42")
        periods = [row.get("Δ ms") for row in await page.evaluate(DEBUG_CELLS)]
        assert gaps[:3] == ["1.003", "1.000", "1.000"] and gaps[-1] == "" and periods == ["10.003", "10.000", ""], \
            f"Δ ms, newest first: {gaps[:3]}…{gaps[-1:]}; with only node 42's frames: {periods}"
    finally:
        await debug_close(page, server)


# Each row of the By ID table as {column heading: cell text}, with whether its age is marked quiet.
DEBUG_ID_ROWS = """(() => { const heads = [...document.querySelectorAll('#fmIdTable thead th')].map(th => th.textContent);
    return [...document.querySelectorAll('#fmIdRows tr')].map(row => Object.assign(
        Object.fromEntries([...row.cells].map((cell, i) => [heads[i], cell.textContent.trim()])),
        {quiet: row.querySelector('.fm-quiet') !== null})); })()"""


@test("Debug: By ID gives each CAN ID a row, with its count, cycle and last data, and marks one gone quiet")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    heartbeat = lambda n, src: dict(_can_frame(n, src=src), id=f"0x107D55{src:02X}", t=1000 + n * 0.1,
                                    data=f"0{n % 10} 00 00 00 00 00 00 E5")  # every 100 ms
    try:
        await debug_start_capture(page)
        if not await page.locator('[data-fm-view="ids"]').count():
            raise AssertionError("The frame monitor has no By ID view")
        await page.locator('[data-fm-view="ids"]').click()
        server.capture([heartbeat(n, 42) for n in range(10)] + [heartbeat(n, 7) for n in range(3)])
        server.flush()
        await page.wait_for_timeout(700)  # By ID is redrawn twice a second at most
        rows = await page.evaluate(DEBUG_ID_ROWS)
        await page.wait_for_timeout(2200)
        server.capture([heartbeat(n, 42) for n in range(10, 12)])  # node 42 goes on; node 7 has stopped
        server.flush()
        await page.wait_for_timeout(700)
        later = {row["CAN ID"]: row["quiet"] for row in await page.evaluate(DEBUG_ID_ROWS)}
        await page.locator("#fmFilter").fill("src:7")
        filtered = [row["CAN ID"] for row in await page.evaluate(DEBUG_ID_ROWS)]
        shown = [(row["CAN ID"], row["Count"], row["Cycle ms"], row["Last data"]) for row in rows]
        assert shown == [("0x107D5507", "3", "100.0", "02 00 00 00 00 00 00 E5"),
                         ("0x107D552A", "10", "100.0", "09 00 00 00 00 00 00 E5")] \
            and later == {"0x107D5507": True, "0x107D552A": False} and filtered == ["0x107D5507"], \
            f"(CAN ID, Count, Cycle, Last data): {shown}; quiet, 3 s on: {later}; filtered on src:7: {filtered}"
    finally:
        await debug_close(page, server)


@test("Debug: error frames show in the trace, in red, saying what went wrong, and are counted")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    error = {"t": 1000.002, "ts": 1.76e9 + 0.002, "dir": "rx", "id": "0x000000A8", "ext": False, "dlc": 8,
             "data": "00 00 04 00 00 00 00 00", "cyphal": False,
             "error": ["protocol violation: stuff", "no ACK", "bus error"]}  # as serialize_message reports one
    try:
        await page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
        await debug_start_capture(page)
        server.capture([_can_frame(1), error])
        server.flush()
        await page.wait_for_timeout(300)
        seen = await page.evaluate("""() => { const red = (node) => { const probe = document.createElement('span');
                probe.style.color = 'var(--error)'; document.body.append(probe);
                const same = getComputedStyle(node).color === getComputedStyle(probe).color; probe.remove(); return same; };
            const row = document.querySelector('#fmRows tr.fm-error-frame');
            const counted = document.querySelector('#fmCounters .fm-errors');
            return {id: row?.querySelector('.fm-id').textContent, says: row?.querySelector('.fm-error-says')?.textContent,
                    red: !!row?.querySelector('.fm-error-says') && red(row.querySelector('.fm-error-says')),
                    counted: counted && counted.textContent, countedRed: !!counted && red(counted)}; }""")
        found = {}
        for typed in ("-error", "ack"):
            await page.locator("#fmFilter").fill(typed)
            found[typed] = [row.get("CAN ID") for row in await page.evaluate(DEBUG_CELLS)]
        assert seen == {"id": "error", "says": "protocol violation: stuff · no ACK · bus error", "red": True,
                        "counted": "errors 1", "countedRed": True} \
            and found == {"-error": ["0x107D5501"], "ack": ["error"]}, \
            f"The error frame's row and the count: {seen}; filtered: {found}"
    finally:
        await debug_close(page, server)


@test("Debug: Save to a raw log opens the Record tab at its raw log, which keeps every frame")
async def _(page):
    server = _DebugServer()
    await debug_open(page, server)
    try:
        link = page.locator("#fmRawLog")
        if not await link.count():
            raise AssertionError("The frame monitor has no way to a raw log")
        await link.click()
        await page.wait_for_timeout(300)
        seen = await page.evaluate("""() => { const box = el('rawLogPanel')?.getBoundingClientRect();
            return {view: state.activeView, inView: !!box && box.top >= 0 && box.top < innerHeight}; }""")
        assert seen == {"view": "record", "inView": True}, f"After the link: {seen}"
    finally:
        await debug_close(page, server)


# ── Test nodes tab ──

_VOLTAGE = "uavcan.si.unit.voltage.Scalar.1.0"
_VOLTAGE_SCHEMA = {"kind": "struct", "type": _VOLTAGE, "constants": [], "fields": [
    {"name": "volt", "schema": {"kind": "float", "type": "saturated float32", "min": -3.4e38, "max": 3.4e38}}]}
_TEST_NODE_OPTIONS = {
    "health": ["nominal", "advisory", "caution", "warning"],
    "modes": ["operational", "initialization", "maintenance", "software_update"],
    "waves": ["sine", "ramp", "square", "noise", "count"],
    "limits": {"test_nodes": 16, "publishers": 8, "node_id": [1, 125], "subject_id": [0, 8191],
               "rate_hz": [0.01, 100.0], "total_rate_hz": 1000.0},
}


def _test_node(test_id, name, node_id=60, running=False):
    return {"id": test_id, "unique_id": "ab" * 16, "name": name, "node_id": node_id, "health": "nominal",
            "mode": "operational", "vendor_status": 0, "software_version": {"major": 1, "minor": 0},
            "publishers": [{"subject_id": 1000, "type": _VOLTAGE, "rate_hz": 10.0, "port": "voltage_scalar",
                            "value": {"volt": 12}}],
            "assigned_node_id": None, "running": running, "error": None, "started_unix": None}


class _TestNodesServer:
    """The backend's test node routes, as server/websocket_server.py answers them."""

    def __init__(self, bus="sim:classic"):
        self.bus = bus        # the bus CAN is connected to (sim:... is simulated); None while it is not
        self.nodes = []       # as GET /api/testnodes lists them
        self.requests = []    # (method, path, body)
        self.refuse = None    # an error POST /api/testnodes answers with, 400

    def _answer(self, method, path, body):
        if path == "/api/dsdl/namespaces":
            return {"namespaces": {"uavcan": {"children": {}, "types": [
                {"kind": "message", "compiled": True, "full_name": _VOLTAGE}]}}}, 200
        if path == f"/api/dsdl/value-schema/{_VOLTAGE}":
            return _VOLTAGE_SCHEMA, 200
        if path == "/api/testnodes" and method == "GET":
            bus = self.bus and {"simulated": self.bus.startswith("sim:"), "interface": self.bus}
            return {"test_nodes": self.nodes, "bus": bus, **_TEST_NODE_OPTIONS}, 200
        if path == "/api/testnodes":
            if self.refuse:
                return {"error": self.refuse}, 400
            node = {**_test_node(f"t{len(self.nodes) + 1}", body["name"]), **body}
            self.nodes.append(node)
            return node, 201
        match = re.fullmatch(r"/api/testnodes/(\w+)(/start|/stop)?", path)
        node = next((n for n in self.nodes if match and n["id"] == match[1]), None)
        if node is None:
            return {"error": "There is no test node"}, 404
        if match[2] == "/start":
            if self.bus is None:
                return {"error": "Connect CAN first: test nodes run on the bus Cynitor is connected to"}, 409
            if not self.bus.startswith("sim:") and not (body or {}).get("confirm_real_bus"):
                return {"error": "This is a real bus. Confirm to start it", "confirm": "real_bus"}, 409
            if node["node_id"] is None:  # left to the server, which gives it a free one
                node["assigned_node_id"] = node["assigned_node_id"] or 100
            node["running"] = True
        elif match[2] == "/stop":
            node["running"] = False
        elif method == "PUT":
            node.update(body)
        elif method == "DELETE":
            self.nodes.remove(node)
            return {"deleted": node["id"]}, 200
        return node, 200

    async def handle(self, route):
        request = route.request
        path = unquote(urlparse(request.url).path)
        body = request.post_data_json if request.post_data else None
        self.requests.append((request.method, path, body))
        answer, status = self._answer(request.method, path, body)
        await route.fulfill(json=answer, status=status, headers={"Access-Control-Allow-Origin": "*"})

    def sent(self, method, path):
        """The bodies of the `method` requests to `path`, in order."""
        return [body for m, p, body in self.requests if (m, p) == (method, path)]


async def testnodes_open(page, server):
    """The Test nodes tab on a freshly loaded page, with a connected dashboard, served by `server`."""
    await page.evaluate("() => { state.dashboardConnected = false; _writeSettingsNow(); }")
    await page.reload(wait_until="load")
    await page.route("**/api/testnodes**", server.handle)
    await page.route("**/api/dsdl/**", server.handle)
    await page.evaluate("state.dashboardConnected = true; switchView('testnodes')")
    await page.wait_for_selector("#tnList .tn-card, #tnList .tn-empty", timeout=5000)


async def testnodes_close(page, server):
    await page.evaluate("""() => { switchView('nodes'); state.dashboardConnected = false; state.canConnected = false;
        state.latestNodesPayload = null; renderNodesTable(); TestNodesView.refreshIndicator(); }""")
    await page.unroute("**/api/testnodes**", server.handle)
    await page.unroute("**/api/dsdl/**", server.handle)


@test("Test nodes: one is made in the editor, its fields built from its type, a waveform sent as the API takes it")
async def _(page):
    server = _TestNodesServer()
    await testnodes_open(page, server)
    try:
        assert "nothing real can move" in await page.locator("#tnBus").inner_text()
        await page.locator("#tnNew").click()
        editor = page.locator("#tnEditor")
        await editor.get_by_label("Name", exact=True).fill("test.battery")
        await editor.get_by_role("button", name="Add publisher").click()
        type_field = editor.get_by_label("Message type")
        await type_field.fill(_VOLTAGE)
        await type_field.dispatch_event("change")
        await editor.get_by_label("value.volt, constant or waveform").select_option("sine")
        for what, value in (("from", "11"), ("to", "12.6"), ("period in seconds", "20")):
            await editor.get_by_label(f"value.volt, {what}").fill(value)
        await editor.get_by_role("button", name="Make it").click()
        await page.wait_for_selector("#tnList .tn-card", timeout=WAIT_MS)
        [sent] = server.sent("POST", "/api/testnodes")
        card = await page.locator("#tnList .tn-card").inner_text()
        assert (sent["name"], sent["node_id"], sent["health"]) == ("test.battery", None, "nominal") and \
            sent["publishers"] == [{"subject_id": 1000, "type": _VOLTAGE, "rate_hz": 1,
                                    "value": {"volt": {"$wave": "sine", "min": 11, "max": 12.6, "period_s": 20}}}], \
            f"Sent: {sent}"
        assert "test.battery" in card and "node-ID auto" in card and _VOLTAGE in card, f"The card: {card!r}"
    finally:
        await testnodes_close(page, server)


@test("Test nodes: the node-ID is left to Cynitor unless one is typed, and the card shows the one it was given")
async def _(page):
    server = _TestNodesServer()
    await testnodes_open(page, server)
    card_ids = page.locator("#tnList .tn-card .tn-card-id")
    try:
        await page.locator("#tnNew").click()
        editor = page.locator("#tnEditor")
        field = editor.get_by_label("Node-ID", exact=True)
        offered = [await field.input_value(), await field.get_attribute("placeholder"), await field.get_attribute("required")]
        await editor.get_by_label("Name", exact=True).fill("left.to.cynitor")
        await editor.get_by_role("button", name="Make it").click()
        await page.wait_for_selector("#tnList .tn-card", timeout=WAIT_MS)
        before_start = await card_ids.first.inner_text()
        await page.get_by_role("button", name="Start left.to.cynitor").click()
        await page.wait_for_selector(".tn-state-running", timeout=WAIT_MS)
        running = await card_ids.first.inner_text()
        await page.locator("#tnNew").click()
        await editor.get_by_label("Name", exact=True).fill("set.by.hand")
        await editor.get_by_label("Node-ID", exact=True).fill("42")
        await editor.get_by_role("button", name="Make it").click()
        await page.wait_for_function("document.querySelectorAll('#tnList .tn-card').length === 2", timeout=WAIT_MS)
        typed = await card_ids.nth(1).inner_text()
        sent = [body["node_id"] for body in server.sent("POST", "/api/testnodes")]
    finally:
        await testnodes_close(page, server)
    assert offered == ["", "auto", None], f"A new test node's Node-ID field (value, placeholder, required): {offered}"
    assert sent == [None, 42], f"Node-IDs sent for the two test nodes: {sent}"
    assert (before_start, running, typed) == ("node-ID auto", "node-ID 100 · auto", "node-ID 42"), \
        f"The cards' node-IDs: {before_start!r}, running {running!r}; the typed one {typed!r}"


@test("Test nodes: a definition the server refuses says why in the editor, which keeps what was typed")
async def _(page):
    server = _TestNodesServer()
    server.refuse = "publishers[0].type: There is no DSDL type demo.Nope.1.0"
    await testnodes_open(page, server)
    try:
        await page.locator("#tnNew").click()
        editor = page.locator("#tnEditor")
        await editor.get_by_label("Name", exact=True).fill("probe")
        await editor.get_by_role("button", name="Make it").click()
        await page.wait_for_function("el('tnEditor').querySelector('.tn-form-error')?.textContent", timeout=WAIT_MS)
        said = await editor.locator(".tn-form-error").inner_text()
        kept = await editor.get_by_label("Name", exact=True).input_value()
        assert "There is no DSDL type demo.Nope.1.0" in said and kept == "probe", f"Said {said!r}; the name: {kept!r}"
        await page.keyboard.press("Escape")
        assert await page.locator("#tnEditor").is_hidden(), "Escape did not close the editor"
    finally:
        await testnodes_close(page, server)


@test("Test nodes: on a real bus Start asks first, and only a confirmed start goes again")
async def _(page):
    server = _TestNodesServer(bus="pcan:PCAN_USBBUS1")
    server.nodes.append(_test_node("t1", "probe"))
    asked = []

    async def on_dialog(dialog):  # declined the first time, then accepted
        asked.append(dialog.message)
        await (dialog.dismiss() if len(asked) == 1 else dialog.accept())

    page.on("dialog", on_dialog)
    await testnodes_open(page, server)
    try:
        banner = await page.locator("#tnBus").inner_text()
        start = page.get_by_role("button", name="Start probe")
        await start.click()
        await page.wait_for_function("!document.querySelector('.tn-card button[data-action=\"start\"]')?.disabled",
                                     timeout=WAIT_MS)
        declined = server.sent("POST", "/api/testnodes/t1/start")
        await start.click()
        await page.wait_for_selector(".tn-state-running", timeout=WAIT_MS)
        assert "real bus" in banner and len(asked) == 2 and "every node on the bus" in asked[0], \
            f"The banner: {banner!r}; asked: {asked}"
        assert declined == [None] and server.sent("POST", "/api/testnodes/t1/start") == [
            None, None, {"confirm_real_bus": True}], f"Starts sent: {server.sent('POST', '/api/testnodes/t1/start')}"
        focused = await page.evaluate("document.activeElement?.getAttribute('aria-label')")
        assert focused == "Stop probe", f"After Start, the focus is on {focused!r}"
    finally:
        page.remove_listener("dialog", on_dialog)
        await testnodes_close(page, server)


@test("Test nodes: health and mode change from the card, sent with the rest of the node as it is")
async def _(page):
    server = _TestNodesServer()
    server.nodes.append(_test_node("t1", "probe", running=True))
    await testnodes_open(page, server)
    try:
        await page.get_by_label("Health of probe").select_option("warning")
        await page.wait_for_function("document.querySelector('.tn-card select[data-action=\"health\"]')?.disabled === false",
                                     timeout=WAIT_MS)
        [sent] = server.sent("PUT", "/api/testnodes/t1")
        expected = {key: value for key, value in _test_node("t1", "probe").items()
                    if key not in ("id", "unique_id", "assigned_node_id", "running", "error", "started_unix")}
        assert sent == {**expected, "health": "warning"}, f"Sent: {sent}"
    finally:
        await testnodes_close(page, server)


@test("Test nodes: without CAN, Start is off and the tab says the simulated bus needs no hardware")
async def _(page):
    server = _TestNodesServer(bus=None)
    server.nodes.append(_test_node("t1", "probe"))
    await testnodes_open(page, server)
    try:
        banner = await page.locator("#tnBus").inner_text()
        start = page.get_by_role("button", name="Start probe")
        assert "CAN is not connected" in banner and "simulated bus" in banner, f"The banner: {banner!r}"
        assert await start.is_disabled() and await start.get_attribute("title") == "Connect CAN first"
    finally:
        await testnodes_close(page, server)


@test("Test nodes: a running one is marked in the Nodes table and the Graph, and the tab has a dot meanwhile")
async def _(page):
    server = _TestNodesServer()
    await testnodes_open(page, server)
    nodes = {"60": {**_graph_node(60, "test.battery", [1000, 7509], [], uid_byte=0xAB), "test_node": "t1"},
             "61": _graph_node(61, "org.example.esc", [7509], [], uid_byte=2)}
    try:
        await page.evaluate(f"""() => {{ state.canConnected = true; state.latestNodesPayload =
            {{node_count: 2, nodes: {json.dumps(nodes)}}}; switchView('nodes'); renderNodesTable();
            TestNodesView.refreshIndicator(); }}""")
        await page.wait_for_function("nodesTabulator.getRows().length === 2", timeout=WAIT_MS)
        marked = await page.evaluate("""() => [...document.querySelectorAll('#nodesTable .tabulator-row')]
            .filter(row => row.querySelector('.test-node-badge')).map(row => row.innerText.split('\\n').join(' '))""")
        dot = await page.evaluate("el('viewTabTestNodes').classList.contains('testnodes-active')")
        await page.evaluate("switchView('graph')")
        await page.wait_for_function("""[...document.querySelectorAll('.graph-label-status')]
            .some(s => s.textContent === 'test node')""", timeout=WAIT_MS)
        await page.evaluate("""() => { state.latestNodesPayload.nodes['60'].has_disappeared = true;
            TestNodesView.refreshIndicator(); }""")
        gone = await page.evaluate("el('viewTabTestNodes').classList.contains('testnodes-active')")
        assert len(marked) == 1 and "test.battery" in marked[0] and dot and not gone, \
            f"Rows with the badge: {marked}; the dot while it ran: {dot}, once it stopped: {gone}"
    finally:
        await testnodes_close(page, server)


# ── Sidebar ──

@test("Sidebar: collapsed, none of its controls takes the focus, and its toggle says it is collapsed")
async def _(page):
    toggle = page.locator("#sidebarCollapseBtn")
    await toggle.click()
    try:
        await page.wait_for_timeout(400)  # the collapse plays for 0.2 s
        focusable = await page.evaluate("""() => [...document.querySelectorAll(
            'nav.sidebar button, nav.sidebar input, nav.sidebar select, nav.sidebar a[href]')]
            .filter((e) => { e.focus(); return document.activeElement === e; })
            .map((e) => e.id || e.textContent.trim())""")
        collapsed = await toggle.get_attribute("aria-expanded")
    finally:
        await toggle.click()
        await page.locator("nav.sidebar:not(.collapsed)").wait_for(state="attached", timeout=WAIT_MS)
    expanded = await toggle.get_attribute("aria-expanded")
    assert not focusable and (collapsed, expanded) == ("false", "true"), \
        f"Collapsed, these sidebar controls still take the focus: {focusable}; " \
        f"the toggle's aria-expanded collapsed/open: {collapsed}/{expanded}"


SIDEBAR_VCAN = {"interface": "vcan0", "label": "vcan0", "needs_bitrate": False, "supports_fd": False}
SIDEBAR_PEAK = {"interface": "pcan:PCAN_USBBUS1", "label": "PEAK PCAN-USB (PCAN_USBBUS1)",
                "needs_bitrate": True, "supports_fd": True}


class _SidebarServer:
    """The backend the sidebar talks to: status, CAN connect and disconnect, the nodes on the bus."""

    def __init__(self):
        self.can = None     # the interface CAN is connected to, or None
        self.down = False   # every request refused, as by a backend that is not running
        self.ws = None      # the server end of the dashboard's socket
        self.adapters = [SIDEBAR_VCAN, SIDEBAR_PEAK]  # what discovery lists
        self.connect_error = None  # what a CAN connect fails with, if it does
        self.last_error = None     # why the backend last ended a CAN session itself
        self.token = None          # the CYNITOR_AUTH_TOKEN it wants, if any
        self.connect_delay = 0     # seconds a CAN connect takes
        self.bus_errors = None     # what it says of errors on the bus, while connected
        self.update = None         # a newer release its update check found
        self.history_error = None  # why its database cannot be written, while connected
        self.listen_only = False   # whether the session it runs sends nothing
        self.connect_body = None   # what the last CAN connect asked for
        self.allocator = None      # what it says of its node-ID allocator, while it runs one
        self.decode_on_demand = False  # whether it decodes only what is watched or recorded
        self.decoding_error = None     # what switching that fails with, if it does
        self.watched = []          # every list of watched subjects the dashboard sent, in turn

    async def handle(self, route):
        if self.down:
            await route.abort("connectionrefused")
            return
        request = route.request
        path = unquote(urlparse(request.url).path)
        if self.token and request.headers.get("authorization") != f"Bearer {self.token}":
            await route.fulfill(json={"error": "Missing or invalid token"}, status=401,
                                headers={"Access-Control-Allow-Origin": "*"})
            return
        if path == "/api/status":
            answer = {"status": "running" if self.can else "idle", "can_interface": self.can,
                      "can_bitrate": None, "can_data_bitrate": None, "can_fd": False,
                      "available_interfaces": ["vcan0"], "available_adapters": self.adapters,
                      "bus_utilization": 30.0 if self.can else None, "dropped": None,
                      "last_error": self.last_error, "cyphal_v11": None,
                      "bus_errors": self.bus_errors if self.can else None,
                      "allocator": self.allocator if self.can else None,
                      "decode_on_demand": self.decode_on_demand, "update": self.update,
                      "history_error": self.history_error if self.can else None,
                      "listen_only": bool(self.can) and self.listen_only}
        elif path == "/api/decoding" and self.decoding_error:
            await route.fulfill(json={"error": self.decoding_error}, status=500,
                                headers={"Access-Control-Allow-Origin": "*"})
            return
        elif path == "/api/decoding":
            self.decode_on_demand = request.post_data_json["on_demand"]
            answer = {"decode_on_demand": self.decode_on_demand}
        elif path == "/api/can/connect" and self.connect_error:
            await route.fulfill(json={"error": self.connect_error}, status=500,
                                headers={"Access-Control-Allow-Origin": "*"})
            return
        elif path == "/api/can/connect":
            await asyncio.sleep(self.connect_delay)
            self.connect_body = request.post_data_json
            self.can = self.connect_body["interface"]
            # As the backend: its allocator runs only in a session asked to run it.
            self.allocator = {"other_allocator": None, "allocated": 0} if self.connect_body.get("allocator") else None
            self.listen_only = self.connect_body.get("listen_only") is True
            answer = {"status": "running", "can_interface": self.can, "can_fd": False, "allocator": self.allocator,
                      "listen_only": self.listen_only}
        elif path == "/api/can/disconnect":
            self.can, self.allocator, answer = None, None, {"status": "idle"}
        elif path == "/api/nodes" and self.can:
            answer = {"node_count": 2, "nodes": {"10": _graph_node(10, "org.example.imu", [100], [], uid_byte=1),
                                                 "11": _graph_node(11, "org.example.esc", [101], [], uid_byte=2)}}
        else:
            answer = {"/api/nodes": {"node_count": 0, "nodes": {}}, "/api/replay/status": {"active": False},
                      "/api/recordings": {"recordings": []}, "/api/rawlogs": {"active": None, "logs": []},
                      "/api/logs": {"logs": []}}.get(path, {})
        try:
            await route.fulfill(json=answer, headers={"Access-Control-Allow-Origin": "*"})
        except Exception:  # the page gave up on the request meanwhile
            pass

    def on_socket(self, ws):
        self.ws = ws
        ws.on_message(self._on_message)

    def _on_message(self, text):
        message = json.loads(text)
        if message.get("type") == "watch":
            self.watched.append(message["subject_ids"])


async def sidebar_open(page, server, saved_connected=False):
    """A freshly loaded page on the Nodes tab whose backend, socket too, is
    `server`, with a connected dashboard saved or not. The socket route is the
    Debug tests' (a page takes one), pointed at `server`."""
    global _debug_server, _debug_socket_routed
    _debug_server = server
    if not _debug_socket_routed:
        _debug_socket_routed = True
        await page.route_web_socket("**/ws**", lambda ws: _debug_server and _debug_server.on_socket(ws))
    await page.route("**/api/**", server.handle)
    await page.evaluate(f"""() => {{ state.dashboardConnected = {json.dumps(saved_connected)};
        state.activeView = 'nodes'; _writeSettingsNow(); }}""")
    await page.reload(wait_until="load")


async def sidebar_close(page, server):
    global _debug_server
    server.down = False
    await page.evaluate("state.preferredCanInterface = ''; disconnectAll()")  # no CAN pick left for the next test
    _debug_server = None
    await page.unroute("**/api/**", server.handle)


async def wait_until(page, expression, seconds):
    """Whether `expression` became true in the page within `seconds`."""
    for _ in range(int(seconds * 5)):
        if await page.evaluate(expression):
            return True
        await page.wait_for_timeout(200)
    return False


@test("Sidebar: CAN Disconnect takes the bus's nodes and load curve with it, rather than leave them looking live")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.ws?.readyState === 1", timeout=5000)
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'connected'", timeout=WAIT_MS)
        for load in (20, 35, 30):  # the backend's metrics, once a second
            server.ws.send(json.dumps({"type": "metrics", "bus_utilization": load}))
        await page.evaluate("getAllNodes()")
        await page.wait_for_function("nodesTabulator.getRows().length === 2", timeout=WAIT_MS)
        shown = await page.evaluate("!el('busLoadSparkline').classList.contains('hidden')")
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'idle'", timeout=WAIT_MS)
        await page.wait_for_timeout(300)
        after = await page.evaluate("""() => ({rows: nodesTabulator.getRows().length,
            curve: !el('busLoadSparkline').classList.contains('hidden'),
            says: document.querySelector('#nodesTable .tabulator-placeholder')?.innerText || ''})""")
        assert shown and after["rows"] == 0 and not after["curve"] and "CAN bus not connected" in after["says"], \
            f"Connected, the curve was shown: {shown}; after CAN Disconnect: {after}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: errors on the bus show under the CAN status, its dot amber, until they stop")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'connected'", timeout=WAIT_MS)
        notice, dot = page.locator("#canBusNotice"), page.locator("#canSemaphore")
        before = (await notice.is_hidden(), await dot.get_attribute("class"))
        # A bus without its terminator, as the backend reports it.
        server.bus_errors = {"state": "ERROR-PASSIVE", "tx_errors": 0, "rx_errors": 128, "since_unix": time.time()}
        await page.evaluate("pollStatus()")  # the 5 s poll, without waiting for it
        await notice.wait_for(state="visible", timeout=WAIT_MS)
        text = " ".join((await notice.inner_text()).split())
        title, during = await notice.get_attribute("title"), await dot.get_attribute("class")
        server.bus_errors = None
        await page.evaluate("pollStatus()")
        await notice.wait_for(state="hidden", timeout=WAIT_MS)
        after = await dot.get_attribute("class")
        assert before == (True, "semaphore ok"), f"With no errors, notice hidden and the dot: {before}"
        assert text == "Bus errors · ERROR-PASSIVE Check 120 Ω termination and bitrate", f"Notice: {text}"
        assert "120 Ω at each end" in title and "TX 0, RX 128" in title, f"Its title: {title}"
        assert (during, after) == ("semaphore warn", "semaphore ok"), f"The dot with errors, then without: {during}, {after}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: a newer release the server found is a link by the version, to its release page")
async def _(page):
    server = _SidebarServer()
    page_url = "https://github.com/OpenCyphal-Garage/cynitor/releases/tag/v9.1.0"
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        none = await page.locator("#versionTag .version-update").count()
        server.update = {"version": "9.1.0", "url": page_url}
        await page.evaluate("pollStatus()")
        link = page.locator("#versionTag .version-update")
        await link.wait_for(state="visible", timeout=WAIT_MS)
        found = (await link.inner_text(), await link.get_attribute("href"), await link.get_attribute("target"))
        if os.environ.get("CYNITOR_E2E_SHOTS"):  # a folder to see the footer in
            await page.locator(".sidebar-footer").screenshot(path=os.path.join(os.environ["CYNITOR_E2E_SHOTS"], "version-update.png"))
        server.update = {"version": "9.2.0", "url": "https://example.com/x"}  # only the project's own release pages
        await page.evaluate("pollStatus()")
        foreign = await link.count()
        assert (none, found, foreign) == (0, ("v9.1.0 available", page_url, "_blank"), 0), \
            f"Before: {none}; found: {found}; a link off the project: {foreign}"
    finally:
        server.update = None
        await page.evaluate("state.update = null; renderVersionTag()")
        await sidebar_close(page, server)


@test("Sidebar: transmit errors alone say no other node acknowledges, not to check termination")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'connected'", timeout=WAIT_MS)
        # The node unplugged: Cynitor alone, its frames unacknowledged (audit E8-18).
        server.bus_errors = {"state": "ERROR-PASSIVE", "tx_errors": 128, "rx_errors": 0, "since_unix": time.time()}
        await page.evaluate("pollStatus()")
        hint = page.locator("#canBusNotice .can-bus-notice-hint")
        await hint.wait_for(state="visible", timeout=WAIT_MS)
        text = await hint.inner_text()
        assert text == "No other node acknowledges: check the bus is connected and a node is powered", f"Hint: {text}"
    finally:
        server.bus_errors = None
        await sidebar_close(page, server)


@test("Sidebar: a problem the backend reports while connected shows under the CAN form, and goes with it")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'connected'", timeout=WAIT_MS)
        server.last_error = "Another node uses Cynitor's own node-ID 127"
        await page.evaluate("pollStatus()")
        shown = await page.evaluate(SIDEBAR_CAN_ERROR)
        server.last_error = None
        await page.evaluate("pollStatus()")
        gone = await page.evaluate(SIDEBAR_CAN_ERROR)
        server.history_error = "disk I/O error"  # a full disk
        await page.evaluate("pollStatus()")
        unsaved = await page.evaluate(SIDEBAR_CAN_ERROR)
        server.history_error = None
        assert (shown, gone, unsaved) == ("Another node uses Cynitor's own node-ID 127", None,
                                          "History and recordings are not being saved: disk I/O error"), \
            f"Shown: {shown!r}; then: {gone!r}; with the disk full: {unsaved!r}"
    finally:
        server.last_error = server.history_error = None
        await sidebar_close(page, server)


@test("Sidebar: a backend that stops answering is reconnected once it is back, its session kept meanwhile")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        server.down = True
        await page.evaluate("pollStatus()")  # the poll that finds it gone, without waiting the 5 s for it
        await page.wait_for_timeout(600)
        during = await page.evaluate("""() => ({button: el('connectDashboardBtn').textContent,
            saved: JSON.parse(localStorage.getItem(STORAGE_KEY)).dashboardConnected})""")
        server.down = False
        back = await wait_until(page, "state.dashboardConnected", 6)
        assert back and during == {"button": "Disconnect", "saved": True}, \
            f"Back up, the dashboard reconnected within 6 s: {back}; while it was down: {during}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: a page opened while the backend is down connects once it answers")
async def _(page):
    server = _SidebarServer()
    server.down = True
    await sidebar_open(page, server, saved_connected=True)
    try:
        await page.wait_for_timeout(800)
        server.down = False
        back = await wait_until(page, "state.dashboardConnected", 6)
        toasts = await page.locator("#toastContainer .toast").all_inner_texts()
        assert back, f"The backend answered again; 6 s later the dashboard is still not connected. Toasts: {toasts}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: when the backend serving the page is gone a while, the page comes back connected after its reload")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)

    async def page_server_down(route):  # the page's heartbeat asks its own server, with HEAD
        if route.request.method == "HEAD":
            await route.abort("connectionrefused")
        else:
            await route.fallback()
    routed = False
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        server.down = True
        await page.route(f"{BASE_URL}/**", page_server_down)
        routed = True
        shown = await wait_until(page, "!el('serverDownOverlay').classList.contains('hidden')", 26)
        server.down = False
        async with page.expect_event("load", timeout=8000):  # the heartbeat reloads it once its server answers
            await page.unroute(f"{BASE_URL}/**", page_server_down)
            routed = False
        back = await wait_until(page, "state.dashboardConnected", 6)
        assert shown and back, f"The outage overlay showed: {shown}; after the reload, connected again: {back}"
    finally:
        if routed:
            await page.unroute(f"{BASE_URL}/**", page_server_down)
        await sidebar_close(page, server)


@test("Sidebar: the CAN interface picked last is picked again, its bitrate too, though settings were saved before connecting")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        # Saved from an earlier session: PEAK at 500 kbit/s.
        await page.evaluate("""() => { clearTimeout(_saveSettingsPending); _saveSettingsPending = null;
            const saved = JSON.parse(localStorage.getItem(STORAGE_KEY));
            saved.canInterface = 'pcan:PCAN_USBBUS1'; saved.canBitrates = {'pcan:PCAN_USBBUS1': 500000};
            localStorage.setItem(STORAGE_KEY, JSON.stringify(saved)); }""")
        await page.reload(wait_until="load")
        for _ in range(2):  # a setting changed before connecting saves them all
            await page.locator("#themeToggle").click()
        await page.wait_for_timeout(400)
        await page.reload(wait_until="load")
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("el('interfacesSelect').options.length > 1", timeout=WAIT_MS)
        picked = await page.evaluate("[el('interfacesSelect').value, el('canBitrateSelect').value]")
        assert picked == ["pcan:PCAN_USBBUS1", "500000"], f"Connected, the CAN list and bitrate read {picked}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: an adapter missing from the list for a moment is picked again once it is back")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("el('interfacesSelect').options.length > 1", timeout=WAIT_MS)
        await page.select_option("#interfacesSelect", "pcan:PCAN_USBBUS1")
        server.adapters = [SIDEBAR_VCAN]  # the USB adapter enumerating again
        await page.evaluate("loadInterfaces()")  # the list's 3-second refresh
        meanwhile = await page.evaluate("el('interfacesSelect').value")
        server.adapters = [SIDEBAR_VCAN, SIDEBAR_PEAK]
        await page.evaluate("loadInterfaces()")
        back = await page.evaluate("selectedCanTarget().interface")
        assert back == "pcan:PCAN_USBBUS1", \
            f"PEAK picked, gone for a refresh ({meanwhile} shown), then back: Connect would open {back}"
    finally:
        await sidebar_close(page, server)


SIDEBAR_CANABLE_SLCAN = {"interface": "slcan:COM5", "label": "CANable (slcan) COM5", "needs_bitrate": True,
                         "supports_fd": True, "data_bitrates": [2000000, 5000000]}


@test("Sidebar: an adapter with CAN FD data bitrate presets is offered only those, and loses a pick it cannot run")
async def _(page):
    server = _SidebarServer()
    server.adapters = [SIDEBAR_VCAN, SIDEBAR_PEAK, SIDEBAR_CANABLE_SLCAN]
    await sidebar_open(page, server)
    offered = "[...el('canDataBitrateSelect').options].filter((o) => !o.disabled).map((o) => o.value)"
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("!el('connectCanBtn').disabled", timeout=WAIT_MS)
        await page.select_option("#interfacesSelect", "pcan:PCAN_USBBUS1")
        peak = await page.evaluate(offered)
        await page.select_option("#interfacesSelect", "slcan:COM5")
        canable = await page.evaluate(offered)
        # Named under Other…, slcan is known by its interface.
        await page.select_option("#interfacesSelect", "__other__")
        await page.fill("#canSpecInput", "pcan:PCAN_USBBUS2")
        await page.select_option("#canDataBitrateSelect", "4000000")
        await page.fill("#canSpecInput", "slcan:COM7@115200")
        typed = await page.evaluate(offered)
        kept = await page.evaluate("el('canDataBitrateSelect').value")
    finally:
        await sidebar_close(page, server)
    assert peak == ["", "1000000", "2000000", "4000000", "5000000", "8000000"], f"PEAK is offered {peak}"
    assert canable == typed == ["", "2000000", "5000000"], \
        f"A CANable with slcan firmware is offered {canable}; one named slcan:COM7, {typed}"
    assert kept == "", f"4 Mbit/s, picked for a PEAK, is still picked for slcan: {kept!r}"


SIDEBAR_SIM = {"interface": "sim:classic", "label": "Simulated bus (no hardware)", "needs_bitrate": False,
               "supports_fd": False, "data_bitrates": None}


@test("Sidebar: the simulated bus connects without a bitrate, picked from the list or typed under Other…")
async def _(page):
    server = _SidebarServer()
    server.adapters = [SIDEBAR_VCAN, SIDEBAR_SIM]
    await sidebar_open(page, server)
    form = """() => ({bitrate: !el('canBitrateRow').classList.contains('hidden'),
        connect: !el('connectCanBtn').disabled})"""
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("!el('connectCanBtn').disabled", timeout=WAIT_MS)
        await page.select_option("#interfacesSelect", "sim:classic")
        listed = await page.evaluate(form)
        await page.select_option("#interfacesSelect", "__other__")
        await page.fill("#canSpecInput", "sim:fd")
        typed = await page.evaluate(form)
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'connected'", timeout=WAIT_MS)
        connected = server.can
    finally:
        await sidebar_close(page, server)
    assert listed == typed == {"bitrate": False, "connect": True}, \
        f"The listed simulated bus: {listed}; one typed under Other…: {typed}"
    assert connected == "sim:fd", f"Connected to {connected!r}"


# What the CAN section says of its last failure, if anything (#canError shown).
SIDEBAR_CAN_ERROR = "el('canError') && !el('canError').classList.contains('hidden') ? el('canError').textContent : null"


@test("Sidebar: why a CAN connect failed, or the session ended, stays under the CAN form until the next try")
async def _(page):
    server = _SidebarServer()
    server.connect_error = "Failed to open can0: [Errno 19] No such device"
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("!el('connectCanBtn').disabled", timeout=WAIT_MS)
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'idle'", timeout=WAIT_MS)
        failed = await page.evaluate(SIDEBAR_CAN_ERROR)
        server.connect_error = None
        await page.locator("#connectCanBtn").click()
        await page.wait_for_function("state.canState === 'connected'", timeout=WAIT_MS)
        cleared = await page.evaluate(SIDEBAR_CAN_ERROR)
        server.can, server.last_error = None, "Interface vcan0 no longer exists"  # ended by the watchdog
        await page.evaluate("pollStatus()")
        lost = await page.evaluate(SIDEBAR_CAN_ERROR)
        live = await page.get_attribute("#toastContainer", "aria-live")
        assert (failed, cleared, lost, live) == ("Connect failed: Failed to open can0: [Errno 19] No such device", None,
                                                 "Disconnected: Interface vcan0 no longer exists", "polite"), \
            f"Refused: {failed!r}; connected: {cleared!r}; ended by the backend: {lost!r}; toasts' aria-live: {live!r}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: screen readers hear what its fields are, and which Connect is the server's and which is CAN's")
async def _(page):
    connect = re.compile("connect", re.IGNORECASE)
    found = {"url": await page.get_by_role("textbox", name="Backend URL", exact=True).count(),
             "list": await page.get_by_role("combobox", name="CAN interface", exact=True).count(),
             "server": await page.get_by_role("group", name="Server").get_by_role("button", name=connect).count(),
             "can": await page.get_by_role("group", name="CAN").get_by_role("button", name=connect).count()}
    assert found == {"url": 1, "list": 1, "server": 1, "can": 1}, f"Found by their names: {found}"


@test("Sidebar: a backend that wants a token says so without calling itself unreachable; the prompt can be left, and keeps what is typed")
async def _(page):
    server = _SidebarServer()
    server.token = "s3cret"
    await sidebar_open(page, server)
    prompt = page.locator("#authModal")
    try:
        await page.locator("#connectDashboardBtn").click()
        await prompt.locator("#authModalInput").wait_for(state="visible", timeout=WAIT_MS)
        toasts = await page.locator("#toastContainer .toast").all_inner_texts()
        await page.keyboard.press("Escape")
        left = await prompt.is_hidden() and await page.evaluate("document.activeElement.id")
        await page.locator("#connectDashboardBtn").click(timeout=WAIT_MS)  # under a prompt still open, it cannot be
        await prompt.locator("#authModalInput").wait_for(state="visible", timeout=WAIT_MS)
        await page.wait_for_timeout(100)  # the field takes the focus
        stops = []
        for _ in range(4):
            await page.keyboard.press("Tab")
            stops.append(await page.evaluate("el('authModal').contains(document.activeElement)"))
        await prompt.locator("#authModalInput").fill("s3c")
        await page.evaluate("showAuthModal('Missing or invalid token')")  # what each refused request does
        kept = await prompt.locator("#authModalInput").input_value()
        await prompt.locator("#authModalInput").fill("s3cret")
        await prompt.locator("#authModalInput").press("Enter")
        connected = await wait_until(page, "state.dashboardConnected", 3)
        assert not toasts and left == "connectDashboardBtn" and all(stops) and kept == "s3c" and connected, \
            f"Toasts: {toasts}; Escape left it (focus back on): {left}; Tab stayed in it: {stops}; " \
            f"typed 's3c', then asked again: {kept!r}; the token saved connects: {connected}"
    finally:
        await page.evaluate("setAuthToken(''); hideAuthModal()")
        await sidebar_close(page, server)


@test("Sidebar: a CAN connect slower than other requests is waited for, its button saying Connecting…")
async def _(page):
    server = _SidebarServer()
    server.connect_delay = 16  # a first connect compiles the DSDL; other requests give up after 15 s
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("!el('connectCanBtn').disabled", timeout=WAIT_MS)
        await page.locator("#connectCanBtn").click()
        await page.wait_for_timeout(500)
        said = await page.locator("#connectCanBtn").text_content()
        connected = await wait_until(page, "state.canState === 'connected'", 18)
        toasts = await page.locator("#toastContainer .toast").all_inner_texts()
        assert said == "Connecting…" and connected and not toasts, \
            f"While connecting the button said {said!r}; connected after 16 s: {connected}; toasts: {toasts}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: with the server not connected, the CAN form is off, saying to connect the server first")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    can_form = """() => ({list: el('interfacesSelect').disabled, button: el('connectCanBtn').disabled,
        title: el('connectCanBtn').title})"""
    try:
        fresh = await page.evaluate(can_form)
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("el('interfacesSelect').options.length > 1", timeout=WAIT_MS)
        connected = await page.evaluate(can_form)
        await page.locator("#connectDashboardBtn").click()  # Disconnect: the list stays, from the last session
        await page.wait_for_function("!state.dashboardConnected", timeout=WAIT_MS)
        left = await page.evaluate(can_form)
        off = {"list": True, "button": True, "title": "Connect to the server first"}
        assert fresh == off and left == off and connected == {"list": False, "button": False, "title": ""}, \
            f"The CAN form on a fresh page: {fresh}; connected: {connected}; after the server's Disconnect: {left}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: the outage overlay names the server that serves the page, and how to start that one")
async def _(page):
    origin = "http://cynitor.test:8080"  # the backend serving the page, as README's setup has it

    async def serve(route):
        file = WEBSITE_DIR / (urlparse(route.request.url).path.lstrip("/") or "index.html")
        await route.fulfill(path=file) if file.is_file() else await route.fulfill(status=404)
    await page.route(f"{origin}/**", serve)
    said = {}
    try:
        for url in (f"{origin}/", f"{BASE_URL}/"):
            await page.goto(url, wait_until="load")
            said[url] = " ".join(await page.evaluate("""[...el('serverDownOverlay').querySelectorAll('p')]
                .filter((p) => !p.classList.contains('hidden')).map((p) => p.textContent)"""))
    finally:
        await page.unroute(f"{origin}/**", serve)
        if not page.url.startswith(BASE_URL):
            await page.goto(BASE_URL, wait_until="load")
    backend, dev = said[f"{origin}/"], said[f"{BASE_URL}/"]
    dev_host = urlparse(BASE_URL).netloc
    assert "cynitor.test:8080" in backend and "http.server" not in backend and dev_host in dev, \
        f"Served by the backend, the overlay says: {backend!r}; served on {dev_host}: {dev!r}"


@test("Sidebar: it needs no scrollbar of its own, its version reads, and its logos show in both themes")
async def _(page):
    # A footer logo hung below it: on Linux and Windows a scrollbar showed at
    # every window size, wrapping the brand row, and below 80rem a second one.
    seen = await page.evaluate("""() => { const sb = document.querySelector('nav.sidebar');
        const html = document.documentElement, theme = html.getAttribute('data-theme');
        html.setAttribute('data-theme', 'dark');
        const hammer = getComputedStyle(document.querySelector('.cordicor-logo')).filter;
        if (theme) html.setAttribute('data-theme', theme); else html.removeAttribute('data-theme');
        const version = el('versionTag'), was = version.textContent;
        version.textContent = 'v0.12.10';  // as the backend serving the page has it
        const over = [sb.scrollHeight - sb.clientHeight, sb.scrollWidth - sb.clientWidth];
        version.textContent = was;
        return {over, darkHammer: hammer, version: parseFloat(getComputedStyle(version).fontSize)}; }""")
    assert seen["over"] == [0, 0] and "invert" in seen["darkHammer"] and seen["version"] >= 9, \
        f"Sidebar overflow (px, down and across): {seen['over']}; the hammer's filter in dark: " \
        f"{seen['darkHammer']!r}; the version's font size: {seen['version']} px"


@test("Sidebar: in a narrow window, collapsed, it leaves no blank behind, and its toggle stays put under the pointer")
async def _(page):
    size = page.viewport_size
    toggle = page.locator("#sidebarCollapseBtn")
    box = """() => { const rect = (e) => e.getBoundingClientRect();
        return {sidebar: Math.round(rect(document.querySelector('nav.sidebar')).height),
                toggleTop: Math.round(rect(el('sidebarCollapseBtn')).top)}; }"""
    await page.set_viewport_size({"width": 900, "height": 700})
    try:
        await page.wait_for_timeout(300)
        await toggle.click()  # collapsed; the pointer stays on the toggle
        await page.wait_for_timeout(400)
        hovered = await page.evaluate(box)
        await page.mouse.move(450, 600)
        await page.wait_for_timeout(200)
        left = await page.evaluate(box)
    finally:
        await page.evaluate("scrollTo(0, 0)")
        if await page.evaluate("state.sidebarCollapsed"):
            await toggle.click()
        await page.locator("nav.sidebar:not(.collapsed)").wait_for(state="attached", timeout=WAIT_MS)
        await page.set_viewport_size(size)
    assert left == {"sidebar": 0, "toggleTop": 0}, f"Collapsed in a 900 px window, the sidebar's height and its toggle's top: {left}"
    assert hovered == left, f"Under the pointer the toggle moved: {hovered}, then {left} with the pointer away"


async def sidebar_connect_can(page):
    await page.locator("#connectCanBtn").click()
    await page.wait_for_function("state.canState === 'connected'", timeout=WAIT_MS)


async def sidebar_disconnect_can(page):
    await page.locator("#connectCanBtn").click()
    await page.wait_for_function("state.canState === 'idle'", timeout=WAIT_MS)


@test("Sidebar: Cynitor allocates node-IDs only once its box is ticked; Connect says so, and the choice is kept")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    row, box = page.locator("#canAllocatorRow"), page.locator("#canAllocatorToggle")
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        offered = (await row.is_visible(), await box.is_checked(), await box.is_enabled())
        await sidebar_connect_can(page)
        unticked = (server.connect_body.get("allocator"), await row.is_hidden())
        await sidebar_disconnect_can(page)

        await box.check()
        await sidebar_connect_can(page)
        ticked = server.connect_body.get("allocator")
        running = (await row.is_visible(), await box.is_checked(), await box.is_disabled())
        server.allocator["allocated"] = 2
        await page.evaluate("pollStatus()")  # the 5 s poll, without waiting for it
        await page.wait_for_function("el('canAllocatorRow').title.includes('2 given')", timeout=WAIT_MS)
        await sidebar_disconnect_can(page)
        again = (await row.is_visible(), await box.is_checked(), await box.is_enabled())

        await page.evaluate("_writeSettingsNow()")
        await page.reload(wait_until="load")
        kept = (await page.evaluate("state.canAllocator"), await box.is_checked())
        assert offered == (True, False, True), f"Before connecting, the box shown, ticked, enabled: {offered}"
        assert unticked == (False, True), f"Unticked, Connect asked for the allocator, and connected the box is hidden: {unticked}"
        assert ticked is True, f"Ticked, Connect asked for the allocator: {ticked}"
        assert running == (True, True, True), f"Connected with it, the box shown, ticked, locked: {running}"
        assert again == (True, True, True), f"Disconnected again, the box shown, ticked, enabled: {again}"
        assert kept == (True, True), f"After a reload, the choice and the box: {kept}"
    finally:
        await page.evaluate("state.canAllocator = false; _writeSettingsNow()")  # off again for the tests after
        await sidebar_close(page, server)


@test("Sidebar: Listen only connects sending nothing; while it runs the box says so, and the choice is kept")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    row, box = page.locator("#canListenOnlyRow"), page.locator("#canListenOnlyToggle")
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        offered = (await row.is_visible(), await box.is_checked(), await box.is_enabled())
        await sidebar_connect_can(page)
        unticked = (server.connect_body.get("listen_only"), await row.is_hidden())
        await sidebar_disconnect_can(page)
        await box.check()
        await sidebar_connect_can(page)
        running = (server.connect_body.get("listen_only"), await row.is_visible(), await box.is_checked(),
                   await box.is_disabled(), "sends nothing" in (await row.get_attribute("title")))
        await sidebar_disconnect_can(page)
        await page.evaluate("_writeSettingsNow()")
        await page.reload(wait_until="load")
        kept = (await page.evaluate("state.canListenOnly"), await box.is_checked())
        assert offered == (True, False, True), f"Before connecting, the box shown, ticked, enabled: {offered}"
        assert unticked == (False, True), f"Unticked, Connect sent listen_only false and the box hides: {unticked}"
        assert running == (True, True, True, True, True), f"Ticked, connected listening only: {running}"
        assert kept == (True, True), f"After a reload, the choice and the box: {kept}"
    finally:
        await page.evaluate("state.canListenOnly = false; _writeSettingsNow()")
        await sidebar_close(page, server)


@test("Sidebar: an allocator that another on the bus has silenced names the node that allocates there, until CAN disconnects")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    notice = page.locator("#canAllocatorNotice")
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        await page.locator("#canAllocatorToggle").check()
        await sidebar_connect_can(page)
        answering = await notice.is_hidden()
        server.allocator = {"other_allocator": 42, "allocated": 1}  # node 42 answered a request too
        await page.evaluate("pollStatus()")
        await notice.wait_for(state="visible", timeout=WAIT_MS)
        text, title = await notice.inner_text(), await notice.get_attribute("title")
        await sidebar_disconnect_can(page)
        gone = await notice.is_hidden()
        assert answering, "While Cynitor's allocator answers, its notice shows"
        assert text == "Allocator silent · node 42 allocates here", f"Notice: {text}"
        assert "A bus has one allocator" in title and "Connect without the allocator" in title, f"Its title: {title}"
        assert gone, "After CAN Disconnect the notice still shows"
    finally:
        await page.evaluate("state.canAllocator = false; _writeSettingsNow()")
        await sidebar_close(page, server)


@test("Sidebar: decoding on demand is switched in the Server section, and the switch follows what the server says")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    box = page.locator("#decodeOnDemandToggle")
    try:
        off = (await box.is_disabled(), await box.is_checked())
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.dashboardConnected", timeout=WAIT_MS)
        ready = (await box.is_enabled(), await box.is_checked())
        await box.check()
        await page.wait_for_function("state.decodeOnDemand === true", timeout=WAIT_MS)
        on = (server.decode_on_demand, await box.is_checked())
        server.decode_on_demand = False   # another dashboard switched it back
        await page.evaluate("pollStatus()")
        await page.wait_for_function("!el('decodeOnDemandToggle').checked", timeout=WAIT_MS)
        server.decoding_error = "CAN is busy"
        await box.click()
        toast = page.locator("#toastContainer .toast", has_text="Decoding not switched")
        await toast.wait_for(state="visible", timeout=WAIT_MS)
        refused = (server.decode_on_demand, await box.is_checked())
        title = await page.locator("#decodeOnDemandRow").get_attribute("title")
        assert off == (True, False), f"With no server, the switch is disabled, ticked: {off}"
        assert ready == (True, False), f"Connected to a server that decodes everything, enabled, ticked: {ready}"
        assert on == (True, True), f"Ticked, the server decodes on demand, and the box is ticked: {on}"
        assert refused == (False, False), f"Refused by the server, the server's mode and the box: {refused}"
        assert "rates still show" in title and "hold only what was decoded" in title, f"Its title: {title}"
    finally:
        await sidebar_close(page, server)


@test("Sidebar: the server is told which subjects' values are on screen, once, and again when they change")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.ws?.readyState === 1", timeout=5000)
        await sidebar_connect_can(page)
        await page.evaluate("getAllNodes()")
        await page.wait_for_function("nodesTabulator.getRows().length === 2", timeout=WAIT_MS)

        async def told(expected):
            for _ in range(15):
                if server.watched and server.watched[-1] == expected:
                    return True
                await page.wait_for_timeout(100)
            return False

        nothing = await told([])                       # a new socket is told, though nothing is on screen
        await page.evaluate("setSelectedNode(10)")     # node 10 publishes subject 100
        node = await told([100])
        await page.evaluate("state.logSubjectIds.add(7)")   # a text subject in the log panel
        log = await told([7, 100])
        sent = len(server.watched)
        await page.wait_for_timeout(700)               # nothing changes: nothing more is said
        quiet = len(server.watched) == sent
        await page.evaluate("clearSelectedNode()")
        left = await told([7])
        assert nothing and node and log and left, \
            f"Told of nothing, node 10's subject, the log's too, then the log's alone: {server.watched}"
        assert quiet, f"With nothing changed, more was sent: {server.watched}"
    finally:
        await page.evaluate("state.logSubjectIds.delete(7); clearSelectedNode()")
        await sidebar_close(page, server)


@test("Sidebar: the server's CPU use shows under its data rate, amber when high and red when it is full")
async def _(page):
    server = _SidebarServer()
    await sidebar_open(page, server)
    reading = page.locator("#serverCpu")
    try:
        await page.locator("#connectDashboardBtn").click()
        await page.wait_for_function("state.ws?.readyState === 1", timeout=5000)
        before = await reading.is_hidden()
        seen = {}
        for cpu in (12.4, 75, 96.2):  # the backend's metrics, once a second
            server.ws.send(json.dumps({"type": "metrics", "bus_utilization": None, "server_cpu": cpu}))
            await page.wait_for_function(f"state.serverCpu === {cpu}", timeout=WAIT_MS)
            await page.evaluate("updateSemaphores()")  # the one-second timer, without waiting for it
            seen[cpu] = (await reading.inner_text(), sorted(set((await reading.get_attribute("class")).split())
                                                           & {"load-warn", "load-err"}))
        title = await reading.get_attribute("title")
        one_line = await page.evaluate("""() => { const r = el('serverCpu').getBoundingClientRect();
            return r.height < 1.6 * parseFloat(getComputedStyle(el('serverCpu')).fontSize); }""")
        await page.evaluate("disconnectAll()")
        gone = await reading.is_hidden()
        assert before, "Before the server said what it uses, the reading shows"
        assert seen[12.4] == ("CPU 12%", []), f"At 12.4 %: {seen[12.4]}"
        assert seen[75] == ("CPU 75%", ["load-warn"]), f"At 75 %: {seen[75]}"
        assert seen[96.2] == ("CPU 96%", ["load-err"]), f"At 96.2 %: {seen[96.2]}"
        assert "96.2% of one core" in title and "cannot keep up" in title, f"Its title: {title}"
        assert one_line, "The reading wraps"
        assert gone, "Disconnected from the server, its reading still shows"
    finally:
        await sidebar_close(page, server)


def _contrast(colour, behind):
    """The WCAG contrast of two opaque colours given as the browser writes them, rgb(r, g, b)."""
    def luminance(css):
        channels = [int(n) / 255 for n in re.findall(r"\d+", css)[:3]]
        r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b
    light, dark = sorted((luminance(colour), luminance(behind)), reverse=True)
    return round((light + 0.05) / (dark + 0.05), 2)


@test("Themes: muted text reads against the page and its panels, in the dark theme as in the light one")
async def _(page):
    seen = {}
    for _ in range(2):  # the theme in use, then the other, and back to the first
        theme = await page.evaluate("document.documentElement.getAttribute('data-theme') || 'light'")
        muted = await page.evaluate(TOKEN_COLOUR, "muted")
        seen[theme] = {surface: _contrast(muted, await page.evaluate(TOKEN_COLOUR, surface))
                       for surface in ("bg", "surface", "surface-2")}
        await page.locator("#themeToggle").click()
    faint = {f"{theme}, on --{surface}": ratio for theme, ratios in seen.items()
             for surface, ratio in ratios.items() if ratio < 4.5}
    assert set(seen) == {"light", "dark"} and not faint, f"Muted text with less than 4.5:1 behind it: {faint}; all: {seen}"


# ── Log panel ──

class _LogServer(_SidebarServer):
    """A backend with a log of its own, which GET /api/logs serves as server/log_store.py does."""

    def __init__(self):
        super().__init__()
        self.records = []       # {timestamp, logger, level, message}, oldest first
        self.asked = []         # the query of every GET /api/logs
        self.logs_error = None  # what GET /api/logs fails with, if it does
        self._clock = time.time()

    def log(self, message, level="INFO"):
        self._clock += 0.001
        stamp = datetime.fromtimestamp(self._clock, tz=timezone.utc).isoformat(timespec="microseconds")
        self.records.append({"timestamp": stamp, "logger": "main", "level": level, "message": message})

    async def handle(self, route):
        url = urlparse(route.request.url)
        if url.path != "/api/logs" or self.down:
            await super().handle(route)
            return
        query = {key: values[0] for key, values in parse_qs(url.query).items()}
        self.asked.append(query)
        headers = {"Access-Control-Allow-Origin": "*"}
        if self.logs_error:
            await route.fulfill(json={"error": self.logs_error}, status=503, headers=headers)
            return
        logs = [r for r in self.records if r["timestamp"] >= query.get("since", "")][-int(query.get("limit", 200)):]
        await route.fulfill(json={"count": len(logs), "logs": logs}, headers=headers)


async def log_open(page, server):
    """A freshly loaded page connected to `server`, its log panel open and empty with the
    Server source on. The panel's own polling is off: a test asks with LOG_FETCH."""
    await page.evaluate("state.logShowServer = true; state.logPanelCollapsed = false")
    await sidebar_open(page, server)
    await page.evaluate("stopServerLogPoll()")
    await page.locator("#connectDashboardBtn").click()
    await page.wait_for_function("state.dashboardConnected", timeout=5000)


async def log_close(page, server):
    await page.evaluate("state.logShowServer = false; state.logPanelCollapsed = true")
    await sidebar_close(page, server)


LOG_FETCH = "_fetchServerLogsOnce()"
LOG_ROWS = "[...el('logList').children].map((row) => row.innerText.replace(/\\s+/g, ' ').trim())"


@test("Log panel: the server's log is asked only for what came since the last record shown")
async def _(page):
    server = _LogServer()
    server.log("boot")
    await log_open(page, server)
    try:
        await page.evaluate(LOG_FETCH)
        server.log("one more")
        await page.evaluate(LOG_FETCH)
        rows = await page.evaluate(LOG_ROWS)
    finally:
        await log_close(page, server)
    first, second = server.asked[0], server.asked[1]
    assert "since" not in first and second.get("since") == server.records[0]["timestamp"], \
        f"The two requests asked for: {first}, then {second}; the first record's time: {server.records[0]['timestamp']}"
    assert [row.split(" main ")[-1] for row in rows] == ["boot", "one more"], f"Rows: {rows}"


@test("Log panel: server records it could not get are marked as missing, where they are missing")
async def _(page):
    server = _LogServer()
    server.log("boot")
    await log_open(page, server)
    try:
        await page.evaluate(LOG_FETCH)
        for i in range(250):  # more than one request takes; the first 50 are the warnings
            server.log(f"burst {i}", "WARNING" if i < 50 else "INFO")
        await page.evaluate(LOG_FETCH)
        rows = await page.evaluate(LOG_ROWS)
    finally:
        await log_close(page, server)
    assert len(rows) == 202 and "boot" in rows[0] and rows[2].endswith("burst 50"), \
        f"{len(rows)} rows, starting: {rows[:3]}"
    assert "missing" in rows[1] and "WARN" in rows[1], f"Between 'boot' and 'burst 50': {rows[1]}"


@test("Log panel: missing server records are marked also when it had none to show before, but a first backlog is not")
async def _(page):
    quiet = _LogServer()  # a log with nothing in it when first asked
    await log_open(page, quiet)
    try:
        await page.evaluate(LOG_FETCH)
        for i in range(250):
            quiet.log(f"burst {i}")
        await page.evaluate(LOG_FETCH)
        after_quiet = await page.evaluate(LOG_ROWS)
    finally:
        await log_close(page, quiet)
    busy = _LogServer()  # a log longer than one request takes, when first asked
    for i in range(250):
        busy.log(f"earlier {i}")
    await log_open(page, busy)
    try:
        await page.evaluate(LOG_FETCH)
        backlog = await page.evaluate(LOG_ROWS)
    finally:
        await log_close(page, busy)
    assert len(after_quiet) == 201 and "missing" in after_quiet[0] and after_quiet[1].endswith("burst 50"), \
        f"{len(after_quiet)} rows after a quiet start, starting: {after_quiet[:2]}"
    assert len(backlog) == 200 and not any("missing" in row for row in backlog), \
        f"{len(backlog)} rows of a first backlog, starting: {backlog[:2]}"


@test("Log panel: a server log that cannot be read marks its pill, saying why, until it can")
async def _(page):
    server = _LogServer()
    server.logs_error = "Log store is not configured"
    await log_open(page, server)
    pill = "({marked: el('logSrcServer').classList.contains('disconnected'), title: el('logSrcServer').title})"
    try:
        await page.evaluate(LOG_FETCH)
        failing = await page.evaluate(pill)
        server.logs_error = None
        await page.evaluate(LOG_FETCH)
        answering = await page.evaluate(pill)
    finally:
        await log_close(page, server)
    assert failing["marked"] and "Log store is not configured" in failing["title"], f"While it fails: {failing}"
    assert not answering["marked"] and "could not be read" not in answering["title"], f"Once it answers: {answering}"


@test("Log panel: every message it counts is in its list, so the filters find the oldest too")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        seen = await page.evaluate("""() => {
            for (let i = 0; i < 1200; i++) ingestLogEvent({subject_id: 8184, message_type: 'Record_1_1',
                publisher_node_id: 50, timestamp_unix: Date.now() / 1000 - 100 + i * 0.01,
                attributes: [{attribute: 'severity', value: 2}, {attribute: 'text', value: `kept ${i}.`}]});
            const filter = (text) => { el('logFilterInput').value = text; el('logFilterInput').dispatchEvent(new Event('input')); };
            const oldest = state.logBuffer[0].text;
            filter(oldest);
            const found = el('logList').querySelectorAll('.log-row:not(.hidden)').length;
            filter('');
            return {kept: state.logBuffer.length, listed: el('logList').childElementCount, oldest, found,
                    counted: Number(el('logSrcCyphal').querySelector('.log-source-count').textContent)}; }""")
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert seen["kept"] == seen["listed"] == seen["counted"], \
        f"After 1200 messages it keeps {seen['kept']}, lists {seen['listed']} and counts {seen['counted']}"
    assert seen["found"] == 1, f"The filter finds {seen['found']} rows for the oldest message kept, {seen['oldest']!r}"


@test("Log panel: a replayed message is listed where it arrives, marked, with the time it was recorded")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        await page.evaluate(INGEST_DIAGNOSTIC, [-30, 2, "live, before it"])  # by a server clock 30 s ahead
        # As server/replay.py sends it: the recording's time, two days old, and replay: true.
        await page.evaluate("""() => ingestLogEvent({subject_id: 8184, message_type: 'Record_1_1', publisher_node_id: 50,
            replay: true, timestamp_unix: new Date(2026, 9, 10, 9, 8, 7).getTime() / 1000,
            attributes: [{attribute: 'severity', value: 5}, {attribute: 'text', value: 'replayed fault'}]})""")
        rows = await page.evaluate(LOG_ROWS)
        label, title = await page.evaluate("""() => { const tag = el('logList').lastElementChild.querySelector('.log-row-tag');
            return [tag?.textContent, tag?.title]; }""")
        await page.locator("#logFilterInput").fill("replay")
        filtered = await page.locator("#logList .log-row:not(.hidden)").count()
    finally:
        await page.locator("#logFilterInput").fill("")
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert len(rows) == 2 and "replayed fault" in rows[1], f"The replayed message should come last: {rows}"
    assert label == "replay" and "2026" in (title or "") and "9:08:07" in title, f"Its tag: {label!r}, saying {title!r}"
    assert filtered == 1, f"'replay' in the filter lists {filtered} rows"


@test("Log panel: shut, none of its controls takes the focus, and its toggle says whether it is open")
async def _(page):
    toggle = page.locator("#logPanelCollapseBtn")
    says = "[el('logPanelCollapseBtn').getAttribute('aria-expanded'), el('logPanelCollapseBtn').title]"
    was_open = await set_log_panel(page, False)
    try:
        await page.wait_for_timeout(400)  # the collapse plays for 0.2 s
        focusable = await page.evaluate("""() => [...document.querySelectorAll(
            '#logPanel button, #logPanel input, #logPanel select')]
            .filter((e) => { e.focus(); return document.activeElement === e; })
            .map((e) => e.id)""")
        shut = await page.evaluate(says)
        await set_log_panel(page, True)
        opened = await page.evaluate(says)
        await page.locator("#logFilterInput").focus()
        usable = await page.evaluate("document.activeElement.id")
    finally:
        await page.evaluate("document.activeElement.blur()")
        await set_log_panel(page, was_open)
    assert not focusable, f"Shut, these log panel controls still take the focus: {focusable}"
    assert shut[0] == "false" and "show" in shut[1].lower(), f"Shut, the toggle says: {shut}"
    assert opened[0] == "true" and "hide" in opened[1].lower(), f"Open, the toggle says: {opened}"
    assert usable == "logFilterInput", f"Open again, the filter box takes the focus: {usable}"
    assert await toggle.get_attribute("aria-controls") == "logPanel", "The toggle does not name the panel it controls"
    assert await toggle.locator("svg").get_attribute("aria-hidden") == "true", \
        "The toggle's arrow is read out as an image with no name"


@test("Log panel: a row keeps its node's name once the bus is gone and the list is drawn again")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    await page.evaluate("""() => { window.e2eNodesBefore = state.latestNodesPayload;
        state.latestNodesPayload = {node_count: 1, nodes: {50: {node_id: 50, name: 'demo.sensor', unique_id: []}}}; }""")
    try:
        await page.evaluate(INGEST_DIAGNOSTIC, [1, 4, "supply voltage low"])
        named = (await page.evaluate(LOG_ROWS))[0]
        await page.evaluate("state.latestNodesPayload = {node_count: 0, nodes: {}}")  # as a CAN disconnect leaves it
        await set_log_panel(page, False)
        await set_log_panel(page, True)  # opening the panel draws its list again
        redrawn = (await page.evaluate(LOG_ROWS))[0]
    finally:
        await page.evaluate("state.latestNodesPayload = window.e2eNodesBefore; delete window.e2eNodesBefore")
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert "50 demo.sensor supply voltage low" in named, f"The row as it arrived: {named}"
    assert redrawn == named, f"Drawn again after the disconnect: {redrawn!r}, was {named!r}"


@test("Log panel: a type of your own called Record is not taken for a diagnostic record")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        # myproject.Record.1.0 on subject 1234: its class is named as uavcan.diagnostic.Record's is.
        await page.evaluate("""() => ingestLogEvent({subject_id: 1234, message_type: 'Record_1_0', publisher_node_id: 51,
            timestamp_unix: Date.now() / 1000,
            attributes: [{attribute: 'index', value: 7}, {attribute: 'celsius', value: 21.5}]})""")
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 3, "a real diagnostic"])
        rows = await page.evaluate(LOG_ROWS)
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert len(rows) == 1 and "NOTICE" in rows[0] and "a real diagnostic" in rows[0], f"Rows: {rows}"


@test("Log panel: markup in a message's node-ID or text is shown as text, never run")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        seen = await page.evaluate("""() => {
            const markup = '<img src="x" class="e2e-log-markup">';
            for (const [nodeId, text] of [[markup, 'its node-ID is markup'], [50, markup]]) {
                ingestLogEvent({subject_id: 8184, message_type: 'Record_1_1', publisher_node_id: nodeId,
                    timestamp_unix: Date.now() / 1000,
                    attributes: [{attribute: 'severity', value: 5}, {attribute: 'text', value: text}]});
            }
            return {elements: el('logList').querySelectorAll('.e2e-log-markup').length,
                    rows: [...el('logList').children].map((row) => row.innerText.replace(/\\s+/g, ' ').trim())}; }""")
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert seen["elements"] == 0, f"Markup from a message became {seen['elements']} element(s) in the log"
    assert len(seen["rows"]) == 2 and "its node-ID is markup" in seen["rows"][0] and "<img" in seen["rows"][1], \
        f"Rows: {seen['rows']}"


@test("Log panel: a text subject whose first string is empty is logged by the next one that says something")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        rows = await page.evaluate("""() => {
            state.logSubjectIds.add(1900);
            ingestLogEvent({subject_id: 1900, message_type: 'Status_1_0', publisher_node_id: 51,
                timestamp_unix: Date.now() / 1000,
                attributes: [{attribute: 'label', value: ''}, {attribute: 'detail', value: 'valve stuck'}]});
            state.logSubjectIds.delete(1900);
            return [...el('logList').children].map((row) => row.innerText.replace(/\\s+/g, ' ').trim()); }""")
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert len(rows) == 1 and rows[0].endswith("s1900 valve stuck"), f"Rows: {rows}"


@test("Log panel: a width saved on a wide screen leaves the main area room in a smaller window, and is never too narrow to read")
async def _(page):
    size = page.viewport_size
    was_open = await set_log_panel(page, True)
    saved = await page.evaluate("state.logPanelWidth")
    measure = """async (width) => { state.logPanelWidth = width; applyLogPanelWidth(el('logPanel'));
        await new Promise((done) => setTimeout(done, 400));  // the width's transition
        const box = (e) => e.getBoundingClientRect(); const toggle = box(el('logPanelCollapseBtn'));
        const cut = [...document.querySelectorAll('.log-panel-header button, .log-panel-header input, .log-panel-header select')]
            .filter((e) => box(e).right > box(el('logPanel')).right).map((e) => e.id);
        return {panel: box(el('logPanel')).width, main: box(document.querySelector('.main-area')).width, cut,
                toggleOff: Math.abs(toggle.left + toggle.width / 2 - box(el('logPanel')).left)}; }"""
    try:
        await page.set_viewport_size({"width": 1100, "height": 700})
        wide = await page.evaluate(measure, 50)  # in rem: dragged to its widest on a large screen
        narrow = await page.evaluate(measure, 7.5)
    finally:
        await page.evaluate("(width) => { state.logPanelWidth = width; applyLogPanelWidth(el('logPanel')); }", saved)
        await page.set_viewport_size(size)
        await set_log_panel(page, was_open)
    assert wide["main"] >= 380 and wide["panel"] >= 380, \
        f"A panel saved 50rem wide, in a 1100 px window: panel {wide['panel']}, main area {wide['main']} px"
    assert narrow["panel"] >= 300 and not narrow["cut"], \
        f"A panel saved 7.5rem wide is {narrow['panel']} px wide; controls cut off at its edge: {narrow['cut']}"
    assert wide["toggleOff"] <= 8 and narrow["toggleOff"] <= 8, \
        f"The toggle is {wide['toggleOff']} and {narrow['toggleOff']} px from the panel's edge"


@test("Log panel: in a narrow window, where it sits under everything else, the page scrolls down to it")
async def _(page):
    size = page.viewport_size
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    box = """() => { const panel = el('logPanel').getBoundingClientRect();
        return {top: Math.round(panel.top), bottom: Math.round(panel.bottom), height: Math.round(panel.height),
                toggleTop: Math.round(el('logPanelCollapseBtn').getBoundingClientRect().top),
                toggleBottom: Math.round(el('logPanelCollapseBtn').getBoundingClientRect().bottom), window: innerHeight}; }"""
    try:
        for i in range(40):
            await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, f"narrow window {i}"])
        await page.set_viewport_size({"width": 900, "height": 700})
        await page.wait_for_timeout(400)
        await page.mouse.move(450, 300)
        await page.mouse.wheel(0, 5000)
        await page.wait_for_timeout(300)
        opened = await page.evaluate(box)
        await set_log_panel(page, False)
        await page.wait_for_timeout(300)
        await page.mouse.wheel(0, 5000)
        await page.wait_for_timeout(300)
        shut = await page.evaluate(box)
    finally:
        await page.evaluate("scrollTo(0, 0); el('logClearBtn').click()")  # shut, its button takes no real click
        await page.set_viewport_size(size)
        await set_log_panel(page, was_open)
    assert 0 <= opened["top"] and opened["bottom"] <= opened["window"] and opened["height"] >= 100, \
        f"Scrolled to the end of a 900x700 window, the open panel is at {opened}"
    assert shut["height"] == 0 and shut["toggleBottom"] <= shut["window"], \
        f"Shut, the panel should take no room and leave its toggle in reach: {shut}"
    # The pointer is still on the toggle it clicked: it must not move from under it.
    assert shut["toggleTop"] == shut["bottom"], f"Under the pointer the toggle left its row: {shut}"


LOG_PILLS = """() => Object.fromEntries([['cyphal', 'logSrcCyphal'], ['server', 'logSrcServer']].map(([source, id]) =>
    [source, {marked: el(id).classList.contains('disconnected'), title: el(id).title}]))"""

# What the log panel is told of the connection, set as the dashboard sets it and
# shown through the call every change of connection makes (updateSemaphores).
LOG_CONNECTION = """(now) => { Object.assign(state, {dashboardConnected: false, canConnected: false, replayActive: false,
    logShowCyphal: true, logShowServer: false}, now); updateSemaphores(); }"""


@test("Log panel: a source's pill says what it is, and, turned on with nothing to come from, why")
async def _(page):
    try:
        await page.evaluate(LOG_CONNECTION, {"logShowServer": True})
        no_backend = await page.evaluate(LOG_PILLS)
        await page.evaluate(LOG_CONNECTION, {"logShowServer": True, "dashboardConnected": True})
        no_bus = await page.evaluate(LOG_PILLS)
        await page.evaluate(LOG_CONNECTION, {"logShowServer": True, "dashboardConnected": True, "canConnected": True})
        all_on = await page.evaluate(LOG_PILLS)
        await page.evaluate(LOG_CONNECTION, {"dashboardConnected": True, "replayActive": True})
        replaying = await page.evaluate(LOG_PILLS)
        await page.evaluate(LOG_CONNECTION, {"logShowCyphal": False})
        turned_off = await page.evaluate(LOG_PILLS)
    finally:
        await page.evaluate(LOG_CONNECTION, {})
    assert no_backend["cyphal"]["marked"] and no_backend["server"]["marked"] \
        and "Not connected to backend" in no_backend["cyphal"]["title"] \
        and "Not connected to backend" in no_backend["server"]["title"], f"With no backend: {no_backend}"
    assert no_bus["cyphal"]["marked"] and "CAN bus not connected" in no_bus["cyphal"]["title"] \
        and not no_bus["server"]["marked"], f"With a backend and no bus: {no_bus}"
    assert not all_on["cyphal"]["marked"] and not all_on["server"]["marked"] \
        and "uavcan.diagnostic.Record" in all_on["cyphal"]["title"] and "log" in all_on["server"]["title"], \
        f"With both connected, each should say what it is: {all_on}"
    assert not replaying["cyphal"]["marked"], f"A replay sends the nodes' messages with no bus: {replaying}"
    assert not turned_off["cyphal"]["marked"], f"A source turned off is not marked: {turned_off}"


@test("Log panel: empty, it says why: no backend, no bus, or what hides the messages it has")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    hint = "el('logEmpty').classList.contains('hidden') ? null : el('logEmpty').textContent"
    said = {}
    try:
        for state, now in (("no backend", {}), ("no bus", {"dashboardConnected": True}),
                           ("no bus, server on", {"dashboardConnected": True, "logShowServer": True}),
                           ("connected", {"dashboardConnected": True, "canConnected": True})):
            await page.evaluate(LOG_CONNECTION, now)
            said[state] = await page.evaluate(hint)
        await page.evaluate(INGEST_DIAGNOSTIC, [1, 2, "an info"])
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 4, "a warning"])
        said["with messages"] = await page.evaluate(hint)
        await page.locator("#logSeveritySelect").select_option("5")
        said["under Min"] = await page.evaluate(hint)
        await page.locator("#logFilterInput").fill("Zzz")
        said["under Min and a filter"] = await page.evaluate(hint)
    finally:
        await page.locator("#logSeveritySelect").select_option("0")
        await page.locator("#logFilterInput").fill("")
        await page.locator("#logClearBtn").click()
        await page.evaluate(LOG_CONNECTION, {})
        await set_log_panel(page, was_open)
    assert "Not connected to backend" in said["no backend"], f"With no backend: {said['no backend']!r}"
    assert said["no bus"].startswith("CAN bus not connected"), f"With a backend and no bus: {said['no bus']!r}"
    assert said["no bus, server on"].startswith("Waiting for log messages") \
        and "CAN bus not connected" in said["no bus, server on"], \
        f"With no bus, the server's log turned on: {said['no bus, server on']!r}"
    assert said["connected"] == "Waiting for log messages…", f"Connected: {said['connected']!r}"
    assert said["with messages"] is None, f"With messages shown it should say nothing: {said['with messages']!r}"
    assert said["under Min"] == "None of the 2 messages kept match Min ERROR.", f"Under Min ERROR: {said['under Min']!r}"
    assert said["under Min and a filter"] == "None of the 2 messages kept match Min ERROR and the filter “Zzz”.", \
        f"Under Min ERROR and a filter: {said['under Min and a filter']!r}"


@test("Log panel: shut, its toggle counts the warnings and errors that came since, in the colour of the worst")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    badge = """() => { const badge = el('logUnread'); const box = badge?.getBoundingClientRect();
        return {shown: Boolean(badge) && !badge.classList.contains('hidden'), text: badge?.textContent,
                error: Boolean(badge?.classList.contains('log-unread--error')), title: el('logPanelCollapseBtn').title,
                inWindow: Boolean(box) && box.width > 0 && box.left >= 0 && box.right <= innerWidth}; }"""
    try:
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 5, "an error seen while open"])
        while_open = await page.evaluate(badge)
        await set_log_panel(page, False)
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, "an info"])
        after_info = await page.evaluate(badge)
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 4, "a warning"])
        after_warning = await page.evaluate(badge)
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 6, "a critical"])
        after_critical = await page.evaluate(badge)
        await page.evaluate("state.logSeverityFloor = 7")  # Min ALERT: an error is not listed, so not counted
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 5, "an error under Min ALERT"])
        await page.evaluate("state.logSeverityFloor = 0")
        after_hidden = await page.evaluate(badge)
        await set_log_panel(page, True)
        reopened = await page.evaluate(badge)
    finally:
        await set_log_panel(page, True)
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert not while_open["shown"] and not after_info["shown"], \
        f"Nothing to count while open, or for an INFO: {while_open}, {after_info}"
    assert after_warning["shown"] and after_warning["text"] == "1" and not after_warning["error"] \
        and after_warning["inWindow"] and "1 warning or error" in after_warning["title"], f"After a warning: {after_warning}"
    assert after_critical["text"] == "2" and after_critical["error"], f"After a critical too: {after_critical}"
    assert after_hidden["text"] == "2", f"A message its Min hides should not count: {after_hidden}"
    assert not reopened["shown"] and "warning" not in reopened["title"], f"Opened, the count is gone: {reopened}"


@test("Log panel: scrolled up, it counts the messages that come below, and a click goes to them")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    cue = """() => { const cue = el('logNewBtn');
        return {shown: Boolean(cue) && !cue.classList.contains('hidden') && cue.getBoundingClientRect().height > 0,
                text: cue?.textContent, error: Boolean(cue?.classList.contains('log-new-btn--error')),
                atEnd: isAtBottom(el('logPanelBody'))}; }"""
    try:
        for i in range(80):  # more than the panel shows at once
            await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, f"line {i}"])
        following = await page.evaluate(cue)
        await page.evaluate("el('logPanelBody').scrollTop = 0")
        await page.wait_for_timeout(100)
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, "a new info"])
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 5, "a new error"])
        reading = await page.evaluate(cue)
        kept_place = await page.evaluate("el('logPanelBody').scrollTop")
        if reading["shown"]:
            await page.locator("#logNewBtn").click(timeout=2000)
            await page.wait_for_timeout(200)
        clicked = await page.evaluate(cue)
        await page.evaluate("el('logPanelBody').scrollTop = 0")
        await page.wait_for_timeout(100)
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, "one more"])
        await page.evaluate("el('logPanelBody').scrollTop = el('logPanelBody').scrollHeight")
        await page.wait_for_timeout(200)
        scrolled_back = await page.evaluate(cue)
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert not following["shown"] and following["atEnd"], f"Following the end, there is nothing to count: {following}"
    assert reading["shown"] and reading["text"].startswith("2 new") and reading["error"] and kept_place == 0, \
        f"Reading the top while an info and an error came: {reading}, scrolled to {kept_place}"
    assert not clicked["shown"] and clicked["atEnd"], f"After a click on the count: {clicked}"
    assert not scrolled_back["shown"], f"After scrolling to the end oneself: {scrolled_back}"


@test("Log panel: Escape shuts the subject picker, and its button says whether it is open")
async def _(page):
    was_open = await set_log_panel(page, True)
    add = page.locator("#logAddSubjectBtn")
    seen = """() => ({shut: el('logSubjectPickerPopover').classList.contains('hidden'),
        expanded: el('logAddSubjectBtn').getAttribute('aria-expanded'), focus: document.activeElement.id})"""
    try:
        before = await page.evaluate(seen)
        await add.click()
        opened = await page.evaluate(seen)
        await page.keyboard.press("Escape")
        escaped = await page.evaluate(seen)
        await add.click()
        await page.mouse.click(5, 5)  # a click elsewhere shuts it too
        clicked_away = await page.evaluate(seen)
    finally:
        await page.evaluate("el('logSubjectPickerPopover').classList.add('hidden'); document.activeElement.blur()")
        await set_log_panel(page, was_open)
    assert before["shut"] and before["expanded"] == "false", f"Before it is opened: {before}"
    assert not opened["shut"] and opened["expanded"] == "true", f"Opened: {opened}"
    assert escaped == {"shut": True, "expanded": "false", "focus": "logAddSubjectBtn"}, f"After Escape: {escaped}"
    assert clicked_away["shut"] and clicked_away["expanded"] == "false", f"After a click elsewhere: {clicked_away}"


@test("Log panel: a message's time is to the millisecond, as the Debug tab's frames, with its date in the tooltip")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        seen = await page.evaluate("""() => {
            const second = new Date(2026, 9, 12, 11, 13, 40).getTime() / 1000;
            for (const [after, text] of [[0.25, 'a quarter past the second'], [0.5, 'half past it']]) {
                ingestLogEvent({subject_id: 8184, message_type: 'Record_1_1', publisher_node_id: 50,
                    timestamp_unix: second + after,
                    attributes: [{attribute: 'severity', value: 2}, {attribute: 'text', value: text}]});
            }
            return [...el('logList').querySelectorAll('.log-row-time')].map((cell) =>
                ({time: cell.textContent, title: cell.title, cut: cell.scrollWidth > cell.clientWidth})); }""")
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert [cell["time"] for cell in seen] == ["11:13:40.250", "11:13:40.500"], f"Times shown: {seen}"
    assert all("2026" in cell["title"] and not cell["cut"] for cell in seen), f"Tooltips, and whether a time is cut: {seen}"


# The WCAG contrast of an element's text against what is painted behind it, opacity counted.
CONTRAST_OF = """(selector) => {
    const parse = (colour) => { const n = colour.match(/[\\d.]+/g).map(Number); return {rgb: n.slice(0, 3), alpha: n.length > 3 ? n[3] : 1}; };
    const over = (top, alpha, under) => top.map((v, i) => v * alpha + under[i] * (1 - alpha));
    const luminance = (rgb) => { const [r, g, b] = rgb.map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
        return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
    const element = document.querySelector(selector);
    const layers = [];
    let opacity = 1;
    for (let node = element; node; node = node.parentElement) {
        const style = getComputedStyle(node);
        opacity *= Number(style.opacity);
        const fill = parse(style.backgroundColor);
        if (fill.alpha > 0 && !layers.some((layer) => layer.alpha === 1)) layers.push(fill);
    }
    let behind = [255, 255, 255];
    for (const layer of layers.reverse()) behind = over(layer.rgb, layer.alpha, behind);
    const text = parse(getComputedStyle(element).color);
    const [light, dark] = [luminance(over(text.rgb, text.alpha * opacity, behind)), luminance(behind)].sort((a, b) => b - a);
    return Math.round((light + 0.05) / (dark + 0.05) * 100) / 100; }"""


@test("Log panel: every severity's label reads against its row in both themes, and DEBUG takes no colour")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    levels = ["trace", "debug", "info", "notice", "warning", "error", "critical", "alert"]
    seen = {}
    try:
        for severity in range(8):
            await page.evaluate(INGEST_DIAGNOSTIC, [8 - severity, severity, f"severity {severity}"])
        for _ in range(2):  # the theme in use, then the other, and back to the first
            theme = await page.evaluate("document.documentElement.getAttribute('data-theme') || 'light'")
            await page.mouse.move(400, 400)
            await page.wait_for_timeout(300)
            seen[theme] = {level: await page.evaluate(CONTRAST_OF, f".log-row-sev-{level} .log-row-sev") for level in levels}
            seen[theme]["debug is coloured"] = await page.evaluate("""() => getComputedStyle(
                document.querySelector('.log-row-sev-debug .log-row-sev')).color
                !== getComputedStyle(document.querySelector('.log-row-sev-debug .log-row-text')).color""")
            await page.locator("#themeToggle").click()
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert set(seen) == {"light", "dark"}, f"Themes checked: {list(seen)}"
    for theme, contrasts in seen.items():
        faint = {level: contrasts[level] for level in levels if contrasts[level] < 4.5}
        assert not faint, f"In the {theme} theme these labels have less than 4.5:1 against their row: {faint}"
        assert not contrasts["debug is coloured"], f"In the {theme} theme DEBUG's label has a colour of its own"


@test("Log panel: a message said again within seconds adds to its row's count, not a row")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        seen = await page.evaluate("""() => {
            const now = Date.now() / 1000;
            const say = (after, text, node = 50, severity = 4) => ingestLogEvent({subject_id: 8184,
                message_type: 'Record_1_1', publisher_node_id: node, timestamp_unix: now + after,
                attributes: [{attribute: 'severity', value: severity}, {attribute: 'text', value: text}]});
            for (let i = 0; i < 5; i++) say(i * 0.02, 'supply voltage low');  // at 50 Hz
            say(0.2, 'supply voltage low', 51);  // another node saying it: a row of its own
            say(0.3, 'supply voltage low');      // no longer in a row with the first five
            say(60, 'supply voltage low');       // a minute later: not a repeat
            const rows = [...el('logList').children];
            return {rows: rows.map((row) => row.innerText.replace(/\\s+/g, ' ').trim().replace(/^\\S+ /, '')),
                    tag: rows[0].querySelector('.log-row-tag')?.title, kept: state.logBuffer.length,
                    counted: el('logSrcCyphal').querySelector('.log-source-count').textContent}; }""")
        await set_log_panel(page, False)
        unread = await page.evaluate("""() => { const later = Date.now() / 1000 + 120;
            for (let i = 0; i < 3; i++) ingestLogEvent({subject_id: 8184, message_type: 'Record_1_1', publisher_node_id: 50,
                timestamp_unix: later + i * 0.1,
                attributes: [{attribute: 'severity', value: 5}, {attribute: 'text', value: 'I2C timeout'}]});
            return {unread: state.logUnreadCount, kept: state.logBuffer.length}; }""")
    finally:
        await set_log_panel(page, True)
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert seen["rows"] == ["WARN 50 ×5 supply voltage low", "WARN 51 supply voltage low",
                            "WARN 50 supply voltage low", "WARN 50 supply voltage low"], f"Rows: {seen['rows']}"
    assert "5 times" in (seen["tag"] or "") and seen["kept"] == 4 and seen["counted"] == "4", \
        f"The count's tooltip: {seen['tag']!r}; kept {seen['kept']}, counted {seen['counted']}"
    assert unread == {"unread": 3, "kept": 5}, f"Shut, an error said three times in a row: {unread}"


@test("Log panel: Save writes the messages listed, filters applied, to a file, and is off with none listed")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        off_when_empty = await page.evaluate("el('logSaveBtn')?.disabled")
        server_time = await page.evaluate("""() => {
            const earlier = new Date(Date.now() - 5000).toISOString();
            ingestServerEntry({timestamp: earlier, level: 'WARNING', logger: 'scanner_node', message: 'two types on subject 1620'});
            return earlier; }""")
        await page.evaluate(INGEST_DIAGNOSTIC, [3, 2, "temperature 21.4 C"])
        await page.evaluate(INGEST_DIAGNOSTIC, [2, 5, "I2C timeout on bus 1"])
        await page.evaluate(INGEST_DIAGNOSTIC, [1.5, 5, "I2C timeout on bus 1"])
        await page.locator("#logSrcServer").click()  # its rows are listed too
        await page.locator("#logSeveritySelect").select_option("4")  # Min WARNING leaves the INFO out
        assert off_when_empty is True and not await page.locator("#logSaveBtn").is_disabled(), \
            f"Save should be off with nothing listed ({off_when_empty}) and on with messages listed"
        async with page.expect_download() as download:
            await page.locator("#logSaveBtn").click()
        saved = await download.value
        name, text = saved.suggested_filename, Path(await saved.path()).read_text()
    finally:
        await page.locator("#logSeveritySelect").select_option("0")
        if await page.evaluate("state.logShowServer"):
            await page.locator("#logSrcServer").click()
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    lines = text.splitlines()
    assert re.fullmatch(r"cynitor-log-[\dTZ-]+\.log", name), f"The file's name: {name}"
    assert lines[0].startswith("#") and "UTC" in lines[0], f"Its first line should say what the columns are: {lines[0]!r}"
    rows = [line.split("\t") for line in lines[1:]]
    assert len(rows) == 2 and rows[0] == [server_time, "WARNING", "server scanner_node", "two types on subject 1620"], \
        f"The file's lines: {lines[1:]}"
    assert rows[1][1:] == ["ERROR", "node 50 ×2", "I2C timeout on bus 1"] and rows[1][0].endswith("Z"), \
        f"The folded error's line: {rows[1]}"


@test("Log panel: its width is kept across reloads, saved in rem, a px one from earlier versions too")
async def _(page):
    was_open = await set_log_panel(page, True)
    width = "Math.round(el('logPanel').getBoundingClientRect().width)"
    stored = "JSON.parse(localStorage.getItem(STORAGE_KEY))"
    try:
        start = await page.evaluate(width)
        handle = await page.locator("#logPanelResizeHandle").bounding_box()
        await page.mouse.move(handle["x"] + 2, 300)
        await page.mouse.down()
        await page.mouse.move(handle["x"] + 2 - 96, 300, steps=4)  # 6rem wider
        await page.mouse.up()
        await page.wait_for_timeout(300)
        dragged = await page.evaluate(width)
        await page.evaluate("_writeSettingsNow()")
        saved = await page.evaluate(f"({stored}).logPanelWidthRem")
        await page.reload(wait_until="load")
        await page.wait_for_timeout(500)  # it opens over 0.2 s
        reloaded = await page.evaluate(width)
        # As an earlier version saved it: in px, under the old name.
        await page.evaluate(f"""() => {{ const settings = {stored}; delete settings.logPanelWidthRem;
            settings.logPanelWidth = 480; localStorage.setItem(STORAGE_KEY, JSON.stringify(settings)); }}""")
        await page.reload(wait_until="load")
        await page.wait_for_timeout(500)
        from_px = [await page.evaluate(width), await page.evaluate("state.logPanelWidth")]
    finally:
        await page.evaluate("state.logPanelWidth = null; applyLogPanelWidth(el('logPanel')); _writeSettingsNow()")
        await set_log_panel(page, was_open)
    assert dragged == start + 96 and saved == dragged / 16, f"Dragged from {start} to {dragged} px; saved as {saved} rem"
    assert reloaded == dragged, f"After a reload it is {reloaded} px wide, was {dragged}"
    assert from_px == [480, 30], f"A width saved as 480 px by an earlier version gives: {from_px} (px shown, rem kept)"


LOG_VISIBLE = """[...el('logList').querySelectorAll('.log-row:not(.hidden)')].map((row) =>
    row.innerText.replace(/\\s+/g, ' ').trim().replace(/^\\S+ /, ''))"""  # the rows shown, without their times


@test("Log panel: the Server source lists the server's records with their level and logger, and asks only while it is on")
async def _(page):
    server = _LogServer()
    server.log("WebSocket server started")
    server.log("two types on subject 1620", "WARNING")
    server.log("CAN connect failed", "ERROR")
    await log_open(page, server)
    pill = page.locator("#logSrcServer")
    try:
        await page.evaluate(LOG_FETCH)
        listed = await page.evaluate(LOG_VISIBLE)
        await page.locator("#logSeveritySelect").select_option("4")
        from_warning = await page.evaluate(LOG_VISIBLE)
        await page.locator("#logSeveritySelect").select_option("0")
        await pill.click()  # off: its rows go
        turned_off = await page.evaluate(LOG_VISIBLE)
        await pill.click()  # on: it asks at once, then every 2 s
        asked_before = len(server.asked)
        await page.wait_for_timeout(2400)
        asked_while_on = len(server.asked) - asked_before
        await pill.click()  # off again: it stops asking
        asked_before = len(server.asked)
        await page.wait_for_timeout(2400)
        asked_while_off = len(server.asked) - asked_before
    finally:
        await page.locator("#logSeveritySelect").select_option("0")
        await page.evaluate("stopServerLogPoll()")
        await log_close(page, server)
    assert listed == ["INFO main WebSocket server started", "WARN main two types on subject 1620",
                      "ERROR main CAN connect failed"], f"Rows: {listed}"
    assert from_warning == listed[1:], f"Under Min WARNING: {from_warning}"
    assert turned_off == [], f"With the source turned off these rows still show: {turned_off}"
    assert asked_while_on >= 1 and asked_while_off == 0, \
        f"Requests for the log in 2.4 s: {asked_while_on} while on, {asked_while_off} while off"


@test("Log panel: the subject picker offers the subjects that carry text, and one is logged while it is ticked")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    say = """(text) => ingestLogEvent({subject_id: 1800, message_type: 'String_1_0', publisher_node_id: 50,
        timestamp_unix: Date.now() / 1000, attributes: [{attribute: 'value', value: text}]})"""
    box = page.locator("#logSubjectPickerPopover .log-subject-checkbox")
    try:
        # A text subject, a subject of numbers, and a heartbeat, whose strings are enum names.
        await page.evaluate("""() => { for (const [subject_id, message_type, attribute, value] of [
                [1800, 'String_1_0', 'value', 'state: idle'], [1620, 'Scalar_1_0', 'kelvin', 294.5],
                [7509, 'Heartbeat_1_0', 'health', 'NOMINAL']]) {
            cacheEvent({subject_id, message_type, publisher_node_id: 50, attributes: [{attribute, value}]}); } }""")
        await page.locator("#logAddSubjectBtn").click()
        offered = await box.evaluate_all("(boxes) => boxes.map((box) => Number(box.dataset.subjectId))")
        await page.evaluate(say, "said before it is ticked")
        await box.first.check()
        await page.evaluate(say, "state: heating")
        picked = await page.evaluate("[...state.logSubjectIds]")
        await box.first.uncheck()
        await page.evaluate(say, "said after it is unticked")
        rows = await page.evaluate(LOG_VISIBLE)
    finally:
        await page.evaluate("""() => { closeLogPicker(); state.logSubjectIds.delete(1800); state.latestByNode.delete(50);
            for (const id of [1800, 1620, 7509]) state.latestBySubject.delete(id);
            state.subjectHistory.delete('1620:kelvin'); _writeSettingsNow(); }""")
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert offered == [1800], f"Subjects offered: {offered}"
    assert picked == [1800] and rows == ["— 50 s1800 state: heating"], f"Picked: {picked}; rows: {rows}"


@test("Log panel: a source turned off hides its rows and keeps their count; Clear empties the list and the counts")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    seen = """() => ({rows: """ + LOG_VISIBLE + """, kept: state.logBuffer.length,
        counts: [...document.querySelectorAll('.log-source-pill')].map((pill) => pill.innerText.replace(/\\s+/g, ' ').trim()),
        pressed: el('logSrcCyphal').getAttribute('aria-pressed')})"""
    try:
        await page.evaluate(INGEST_DIAGNOSTIC, [2, 2, "from a node"])
        await page.evaluate(INGEST_DIAGNOSTIC, [1, 4, "from a node, later"])
        await page.evaluate("ingestServerEntry({timestamp: new Date().toISOString(), level: 'INFO', logger: 'main', message: 'from the server'})")
        await page.locator("#logSrcServer").click()
        both = await page.evaluate(seen)
        await page.locator("#logSrcCyphal").click()
        cyphal_off = await page.evaluate(seen)
        await page.locator("#logSrcCyphal").click()
        back_on = await page.evaluate(seen)
        await page.locator("#logClearBtn").click()
        cleared = await page.evaluate(seen)
    finally:
        if await page.evaluate("state.logShowServer"):
            await page.locator("#logSrcServer").click()
        if not await page.evaluate("state.logShowCyphal"):
            await page.locator("#logSrcCyphal").click()
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert len(both["rows"]) == 3 and both["counts"] == ["CYPHAL 2", "SERVER 1"], f"With both sources on: {both}"
    assert cyphal_off["rows"] == ["INFO main from the server"] and cyphal_off["counts"] == ["CYPHAL 2", "SERVER 1"] \
        and cyphal_off["pressed"] == "false", f"With Cyphal turned off: {cyphal_off}"
    assert back_on["rows"] == both["rows"] and back_on["pressed"] == "true", f"Turned on again: {back_on}"
    assert cleared["rows"] == [] and cleared["kept"] == 0 and cleared["counts"] == ["CYPHAL", "SERVER"], \
        f"After Clear: {cleared}"


@test("Log panel: Min, autoscroll, the sources, the subjects picked and whether it is open survive a reload; the filter does not")
async def _(page):
    was_open = await set_log_panel(page, True)
    try:
        await page.locator("#logSeveritySelect").select_option("5")
        await page.locator("#logAutoscrollBtn").click()  # off
        await page.locator("#logSrcServer").click()  # on
        await page.locator("#logSrcCyphal").click()  # off
        await page.locator("#logFilterInput").fill("i2c")
        await page.evaluate("state.logSubjectIds.add(1800); _writeSettingsNow()")
        await page.reload(wait_until="load")
        await page.wait_for_timeout(500)
        kept = await page.evaluate("""() => ({min: el('logSeveritySelect').value,
            autoscroll: el('logAutoscrollBtn').getAttribute('aria-pressed'),
            server: el('logSrcServer').getAttribute('aria-pressed'), cyphal: el('logSrcCyphal').getAttribute('aria-pressed'),
            subjects: [...state.logSubjectIds], open: !el('logPanel').classList.contains('collapsed'),
            filter: el('logFilterInput').value + state.logTextFilter})""")
    finally:
        await page.evaluate("""() => { stopServerLogPoll();
            Object.assign(state, {logSeverityFloor: 0, logAutoscroll: true, logShowServer: false, logShowCyphal: true});
            state.logSubjectIds.delete(1800); _writeSettingsNow(); }""")
        await page.reload(wait_until="load")
        await page.wait_for_timeout(500)
        await set_log_panel(page, was_open)
    assert kept == {"min": "5", "autoscroll": "false", "server": "true", "cyphal": "false", "subjects": [1800],
                    "open": True, "filter": ""}, f"After a reload: {kept}"


@test("Log panel: with autoscroll off a new message leaves the list where it is; turned on again, the list follows")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    place = "({top: Math.round(el('logPanelBody').scrollTop), atEnd: isAtBottom(el('logPanelBody'))})"
    try:
        for i in range(80):
            await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, f"line {i}"])
        following = await page.evaluate(place)
        await page.locator("#logAutoscrollBtn").click()  # off
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, "came with autoscroll off"])
        held = await page.evaluate(place)
        await page.locator("#logAutoscrollBtn").click()  # on: it goes to the end
        on_again = await page.evaluate(place)
        await page.evaluate(INGEST_DIAGNOSTIC, [0, 2, "came with autoscroll on"])
        followed = await page.evaluate(place)
    finally:
        if not await page.evaluate("state.logAutoscroll"):
            await page.locator("#logAutoscrollBtn").click()
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert following["atEnd"] and following["top"] > 0, f"80 messages in, the list should be at its end: {following}"
    assert held == {"top": following["top"], "atEnd": False}, f"With autoscroll off, after one more message: {held}"
    assert on_again["atEnd"] and followed["atEnd"] and followed["top"] > on_again["top"], \
        f"Turned on again: {on_again}; after one more message: {followed}"


# Notes, from before the page's own scripts run, every transition the log panel or
# its toggle starts. Stays registered for later loads, where it does nothing
# unless asked to through sessionStorage.
LOG_SLIDE_WATCH = """if (sessionStorage.getItem('e2eWatchLogSlide')) {
    window.e2eLogSlide = [];
    document.addEventListener('transitionrun', (event) => {
        if (['logPanel', 'logPanelCollapseBtn'].includes(event.target.id)) {
            window.e2eLogSlide.push(`${event.target.id} ${event.propertyName}`);
        }
    }, true);
}"""
_log_slide_watched = False


@test("Log panel: saved open, it is in place as the page loads rather than sliding in; its toggle still slides it")
async def _(page):
    global _log_slide_watched
    if not _log_slide_watched:
        _log_slide_watched = True
        await page.add_init_script(LOG_SLIDE_WATCH)
    was_open = await set_log_panel(page, True)
    try:
        await page.evaluate("_writeSettingsNow(); sessionStorage.setItem('e2eWatchLogSlide', '1')")
        await page.reload(wait_until="load")
        await page.wait_for_timeout(500)
        at_load = await page.evaluate("window.e2eLogSlide.splice(0)")
        open_after_load = await page.evaluate("!el('logPanel').classList.contains('collapsed')")
        await page.locator("#logPanelCollapseBtn").click()
        await page.wait_for_timeout(400)
        on_toggle = await page.evaluate("window.e2eLogSlide.splice(0)")
        await page.locator("#logPanelCollapseBtn").click()
        await page.wait_for_timeout(400)
    finally:
        await page.evaluate("sessionStorage.removeItem('e2eWatchLogSlide')")
        await set_log_panel(page, was_open)
    assert open_after_load and at_load == [], f"Transitions started while the page loaded: {at_load}"
    assert "logPanel width" in on_toggle, f"Its toggle should still slide it: {on_toggle}"


@test("Log panel: a text subject that keeps saying new things does not push out what the nodes reported")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    try:
        seen = await page.evaluate("""() => {
            const now = Date.now() / 1000 - 100;
            ingestLogEvent({subject_id: 8184, message_type: 'Record_1_1', publisher_node_id: 50, timestamp_unix: now,
                attributes: [{attribute: 'severity', value: 5}, {attribute: 'text', value: 'the one error'}]});
            state.logSubjectIds.add(1800);
            for (let i = 0; i < 1500; i++) {  // a text of its own every time: nothing to fold
                ingestLogEvent({subject_id: 1800, message_type: 'String_1_0', publisher_node_id: 50,
                    timestamp_unix: now + 1 + i * 0.02, attributes: [{attribute: 'value', value: `reading ${i}`}]});
            }
            state.logSubjectIds.delete(1800);
            const rows = [...el('logList').children];
            return {kept: state.logBuffer.length, listed: rows.length,
                    error: state.logBuffer.some((entry) => entry.text === 'the one error'),
                    first: rows[0].innerText.replace(/\\s+/g, ' ').trim().replace(/^\\S+ /, ''),
                    last: rows[rows.length - 1].innerText.replace(/\\s+/g, ' ').trim().replace(/^\\S+ /, ''),
                    inStep: state.logBuffer.map((entry) => String(entry.id)).join() === rows.map((row) => row.dataset.id).join()}; }""")
    finally:
        await page.evaluate("state.logSubjectIds.delete(1800)")
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert seen["error"] and seen["first"] == "ERROR 50 the one error", \
        f"After 1500 texts from a subject the node's error should still lead the list: {seen}"
    assert seen["kept"] == seen["listed"] == 1000 and seen["last"].endswith("reading 1499") and seen["inStep"], \
        f"The list should hold its 1000 newest in step with what is kept: {seen}"


@test("Log panel: shut, a screen reader is told of the first warning, and of the first error, not of every message")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    told = "el('logAnnounce')?.textContent"
    try:
        await set_log_panel(page, False)
        region = await page.evaluate("""() => { const region = el('logAnnounce'); if (!region) return null;
            window.e2eLogAnnounced = 0;
            new MutationObserver(() => { window.e2eLogAnnounced += 1; }).observe(region, {childList: true, characterData: true, subtree: true});
            const box = region.getBoundingClientRect();
            return {role: region.getAttribute('role'), drawn: box.width > 2 || box.height > 2, text: region.textContent}; }""")
        said = []
        for severity, text in ((2, "an info"), (4, "a warning"), (4, "another warning"), (5, "an error"), (6, "a critical")):
            await page.evaluate(INGEST_DIAGNOSTIC, [0, severity, text])
            said.append(await page.evaluate(told))
        times = await page.evaluate("window.e2eLogAnnounced")
        await set_log_panel(page, True)
        opened = await page.evaluate(told)
    finally:
        await set_log_panel(page, True)
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    assert region == {"role": "status", "drawn": False, "text": ""}, f"The region a screen reader hears: {region}"
    assert said[0] == "" and "warning" in said[1] and said[2] == said[1] and "error" in said[3] and said[4] == said[3], \
        f"What it held after an info, two warnings, an error and a critical: {said}"
    assert times == 2 and opened == "", f"It was told {times} times; with the panel open it holds {opened!r}"


@test("Log panel: the subject picker takes the focus when it opens, so its boxes are ticked by keyboard")
async def _(page):
    was_open = await set_log_panel(page, True)
    try:
        await page.evaluate("""() => cacheEvent({subject_id: 1800, message_type: 'String_1_0', publisher_node_id: 50,
            attributes: [{attribute: 'value', value: 'state: idle'}]})""")
        await page.locator("#logAddSubjectBtn").focus()
        await page.keyboard.press("Enter")
        focused = await page.evaluate("document.activeElement.className + ' ' + (document.activeElement.dataset.subjectId || '')")
        await page.keyboard.press("Space")
        ticked = await page.evaluate("[...state.logSubjectIds]")
        await page.keyboard.press("Escape")
        after = await page.evaluate("[document.activeElement.id, el('logSubjectPickerPopover').classList.contains('hidden')]")
    finally:
        await page.evaluate("""() => { closeLogPicker(); state.logSubjectIds.delete(1800); state.latestBySubject.delete(1800);
            state.latestByNode.delete(50); _writeSettingsNow(); document.activeElement.blur(); }""")
        await set_log_panel(page, was_open)
    assert focused == "log-subject-checkbox 1800", f"Opened with Enter, the focus is on: {focused!r}"
    assert ticked == [1800] and after == ["logAddSubjectBtn", True], f"Space ticked {ticked}; after Escape: {after}"


@test("Log panel: reading back in a full list, what is in view stays put as old messages go and late ones come in above")
async def _(page):
    was_open = await set_log_panel(page, True)
    await page.locator("#logClearBtn").click()
    in_view = """() => { const top = el('logPanelBody').getBoundingClientRect().top;
        for (const row of el('logList').children) { const box = row.getBoundingClientRect();
            if (box.bottom > top + 1) return {row: row.innerText.replace(/\\s+/g, ' ').trim().replace(/^\\S+ /, ''),
                                              at: Math.round(box.top - top)}; } }"""
    ingest = """([count, text, ago]) => { const start = Date.now() / 1000 - ago;
        for (let i = 0; i < count; i++) ingestLogEvent({subject_id: 8184, message_type: 'Record_1_1', publisher_node_id: 50,
            timestamp_unix: start + i * 0.001, attributes: [{attribute: 'severity', value: 2}, {attribute: 'text', value: `${text} ${i}`}]}); }"""
    try:
        await page.evaluate(ingest, [1000, "line", 100])  # the list is full
        await page.evaluate("el('logPanelBody').scrollTop = el('logPanelBody').scrollHeight / 2")
        await page.wait_for_timeout(100)
        reading = await page.evaluate(in_view)
        await page.evaluate(ingest, [50, "newer", 10])  # each pushes the oldest row out, above what is in view
        after_newer = await page.evaluate(in_view)
        await page.evaluate(ingest, [3, "late", 99.7])  # sorts in near the top of the list, above what is in view
        after_late = await page.evaluate(in_view)
        anchoring = await page.evaluate("getComputedStyle(el('logPanelBody')).overflowAnchor")
    finally:
        await page.locator("#logClearBtn").click()
        await set_log_panel(page, was_open)
    def same_place(now):  # the same row, within the pixel a scroll position is rounded to
        return now["row"] == reading["row"] and abs(now["at"] - reading["at"]) <= 1

    assert reading["row"].startswith("INFO 50 line 5"), f"Scrolled to the middle of 1000 rows, the first in view: {reading}"
    assert same_place(after_newer), f"After 50 new messages the first row in view is {after_newer}, was {reading}"
    assert same_place(after_late), f"After 3 late messages sorted in above, the first row in view is {after_late}, was {reading}"
    assert anchoring == "none", f"The list keeps the place itself, so the browser must not as well: overflow-anchor is {anchoring}"


# Keep last: it reloads the page with every other host unreachable.
@test("Dashboard works with no internet access")
async def _(page):
    own_host = urlparse(BASE_URL).hostname
    external = []  # scripts, stylesheets and fonts asked of other hosts

    async def offline(route):
        request = route.request
        if urlparse(request.url).hostname == own_host:
            await route.continue_()
            return
        if request.resource_type in ("script", "stylesheet", "font"):
            external.append(request.url)
        await route.abort("internetdisconnected")

    await page.route("**/*", offline)
    try:
        await page.reload(wait_until="load")
        await page.wait_for_selector(".tabulator-col-title", timeout=5000)
        loaded = await page.evaluate("typeof d3 === 'object' && typeof Tabulator === 'function'")
        assert loaded, "D3 or Tabulator did not load without internet access"
        assert not external, f"Requested from other hosts: {external}"
    finally:
        await page.unroute("**/*", offline)


# ── Runner ──

async def run_tests():
    from playwright.async_api import async_playwright

    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 800})
        page = await context.new_page()
        for name, fn in TESTS:
            try:
                await fn(page)
                results.append((name, "PASS", ""))
            except Exception as e:
                results.append((name, "FAIL", str(e)))
        await browser.close()
    return results


def write_results(results):
    passed = sum(1 for _, status, _ in results if status == "PASS")
    failed = len(results) - passed
    lines = [
        "# Landing Page Test Results\n",
        f"**Passed:** {passed} | **Failed:** {failed} | **Total:** {len(results)}\n",
        "",
        "| Test | Status | Details |",
        "|------|--------|---------|",
    ]
    for name, status, detail in results:
        detail = detail.replace("|", "\\|")[:100]
        lines.append(f"| {name} | {status} | {detail} |")
    RESULT_FILE.write_text("\n".join(lines) + "\n")
    print(f"\nResults written to {RESULT_FILE}")
    return passed, failed


def main():
    try:
        with frontend_server():
            results = asyncio.run(run_tests())
    except Exception as e:
        print(f"Fatal error: {e}")
        traceback.print_exc()
        results = [("Test setup", "FAIL", str(e))]

    passed, failed = write_results(results)
    print(f"\n{'=' * 40}")
    print(f"  PASSED: {passed}  |  FAILED: {failed}")
    print(f"{'=' * 40}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
