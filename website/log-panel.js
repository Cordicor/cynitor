// Right-side log panel: shell (collapse/resize/persistence) + live log feed
// from two independently-toggled sources:
//   - cyphal: uavcan.diagnostic.Record + user-added text subjects (WS push)
//   - server: GET /api/logs poller (Python `logging` records)
//
// Source of truth: state.logBuffer (in-memory ring buffer, NOT persisted).
// Persisted UI: state.logPanelCollapsed, state.logPanelWidth,
//               state.logSeverityFloor, state.logAutoscroll,
//               state.logSubjectIds, state.logShowCyphal, state.logShowServer.

const LOG_PANEL_MIN_WIDTH_PX = 320;   // 20rem, as --log-w-fit in styles.css: what the controls and a readable message need
const LOG_PANEL_MAX_WIDTH_PX = 800;   // ~50rem
// Entries kept, every one of them drawn: the counts, the filters and the list
// then speak of the same rows. Drawing a new one costs about 1 ms at 1000 rows
// and twice that at 2000.
const LOG_CAP = 1000;
const AUTOSCROLL_STICKY_PX = 16;
const SERVER_LOG_POLL_MS = 2000;
const SERVER_LOG_FETCH_LIMIT = 200;

const SEVERITY_LABELS = ['TRACE', 'DEBUG', 'INFO', 'NOTICE', 'WARNING', 'ERROR', 'CRITICAL', 'ALERT'];
const SEVERITY_CSS = ['trace', 'debug', 'info', 'notice', 'warning', 'error', 'critical', 'alert'];
// What the severity column shows: short enough for one narrow column.
const SEVERITY_SHORT = ['TRACE', 'DEBUG', 'INFO', 'NOTICE', 'WARN', 'ERROR', 'CRIT', 'ALERT'];

const SERVER_LEVEL_TO_SEVERITY = {
  DEBUG: 1, INFO: 2, WARNING: 4, ERROR: 5, CRITICAL: 6,
};

const DIAGNOSTIC_SUBJECT_ID = 8184;

// From this severity on a message is one to notice: the shut panel's toggle counts it.
const SEVERITY_WARNING = 4;
const SEVERITY_ERROR = 5;

// Per-source entry counters mirror state.logBuffer composition.
// Kept in sync via _pushEntry, _onClearLog, _recomputeSrcCounts.
const _srcCounts = { cyphal: 0, server: 0 };

const _recomputeSrcCounts = () => {
  _srcCounts.cyphal = 0;
  _srcCounts.server = 0;
  for (const e of state.logBuffer) {
    if (e.source in _srcCounts) _srcCounts[e.source]++;
  }
};

// What each source is, for its pill's tooltip.
const LOG_SOURCE_ABOUT = {
  cyphal: 'What the nodes say: uavcan.diagnostic.Record, and the text subjects added with +',
  server: "Cynitor's own log, from its server",
};

// Why nothing can come from a source now, as connectionProblem's parts
// (message, helper), or null when its messages can come.
const logSourceProblem = (src) => {
  // Without the backend neither source has anything to come from.
  if (!state.dashboardConnected) return connectionProblem('see log messages');
  if (src === 'server') {
    return _serverLogError ? { message: "The server's log could not be read:", helper: _serverLogError } : null;
  }
  // A replay sends the nodes' messages with no bus connected.
  return state.replayActive ? null : connectionProblem('see what the nodes say');
};

const _problemText = (problem) =>
  `${problem.message}${/[.…:]$/.test(problem.message) ? '' : '.'} ${problem.helper}`;

const _refreshSourcePillBadges = () => {
  for (const pill of document.querySelectorAll('.log-source-pill')) {
    const src = pill.dataset.source;
    const countEl = pill.querySelector('.log-source-count');
    if (countEl) {
      const n = _srcCounts[src] || 0;
      countEl.textContent = n > 0 ? String(n) : '';
      countEl.classList.toggle('hidden', n === 0);
    }
    // Amber, with a dot: turned on, with nothing to come from. Its tooltip says why.
    const problem = _isSourceOn(src) ? logSourceProblem(src) : null;
    const title = problem ? _problemText(problem) : LOG_SOURCE_ABOUT[src];
    pill.classList.toggle('disconnected', Boolean(problem));
    if (pill.title !== title) pill.title = title;
  }
};

// The connection changed (connection.js says so): the panel shows what can
// reach it now.
const renderLogConnection = () => {
  _refreshSourcePillBadges();
  updateEmptyHint();
};

// ── The toggle at the window's edge ─────────────────────────────────

// A bare arrow: it says what it opens, and, while the panel is shut, how many
// warnings and errors came since, in the colour of the worst of them.
const renderLogToggle = () => {
  const toggle = el('logPanelCollapseBtn');
  const badge = el('logUnread');
  if (!toggle || !badge) return;
  const n = state.logUnreadCount;
  const since = n ? `: ${n} ${n === 1 ? 'warning or error' : 'warnings or errors'} since it was shut` : '';
  toggle.setAttribute('aria-expanded', String(!state.logPanelCollapsed));
  toggle.setAttribute('aria-label', `Toggle log panel${since}`);
  toggle.title = state.logPanelCollapsed ? `Show the log panel${since}` : 'Hide the log panel';
  toggle.classList.toggle('has-unread', n > 0);
  badge.textContent = n > 99 ? '99+' : String(n);
  badge.classList.toggle('hidden', n === 0);
  badge.classList.toggle('log-unread--error', state.logUnreadWorst >= SEVERITY_ERROR);
};

// ── New messages below ──────────────────────────────────────────────

// Over the list's foot while its end is out of view: how many messages came
// meanwhile, in the colour of the worst when that is a warning or an error.
// A click goes to them.
const renderLogNewBelow = () => {
  const cue = el('logNewBtn');
  if (!cue) return;
  const n = state.logNewBelow;
  cue.classList.toggle('hidden', n === 0);
  cue.classList.toggle('log-new-btn--warn', state.logNewBelowWorst === SEVERITY_WARNING);
  cue.classList.toggle('log-new-btn--error', state.logNewBelowWorst >= SEVERITY_ERROR);
  cue.textContent = `${n} new ↓`;
  cue.setAttribute('aria-label', `${n} new ${n === 1 ? 'message' : 'messages'}: go to the newest`);
};

const clearLogNewBelow = () => {
  if (!state.logNewBelow) return;
  state.logNewBelow = 0;
  state.logNewBelowWorst = 0;
  renderLogNewBelow();
};

// ── Width plumbing ──────────────────────────────────────────────────

const applyLogPanelWidth = (panel) => {
  const shell = panel.closest('.app-shell');
  if (!shell) return;
  if (state.logPanelWidth != null) {
    shell.style.setProperty('--log-w', state.logPanelWidth + 'px');
  } else {
    shell.style.removeProperty('--log-w');
  }
};

// ── Cyphal source helpers ───────────────────────────────────────────

// By its fixed subject-ID, not its class name: a type of another namespace
// may be called Record too.
const isDiagnosticRecord = (event) => event?.subject_id === DIAGNOSTIC_SUBJECT_ID;

const isUserLoggedSubject = (event) => {
  return Number.isInteger(event?.subject_id) && state.logSubjectIds.has(event.subject_id);
};

// The text of a message that is no diagnostic record: its first string that
// says something, or ''.
const firstStringValue = (event) => {
  for (const a of event?.attributes || []) {
    if (typeof a.value === 'string' && a.value.length > 0) return a.value;
  }
  return '';
};

const hasStringAttribute = (event) => firstStringValue(event) !== '';

const extractRecordEntry = (event) => {
  let severity = null;
  let text = '';
  for (const a of event.attributes || []) {
    if (a.attribute === 'severity' && Number.isInteger(a.value)) severity = a.value;
    else if (a.attribute === 'text' && typeof a.value === 'string') text = a.value;
  }
  if (severity == null || severity < 0 || severity > 7) severity = null;
  return { severity, text };
};

// ── Entry builders ──────────────────────────────────────────────────

// The time of a message that brings no usable one: now, and not before the
// newest entry, whose time is by the server's clock, which may be ahead.
const _arrivalTime = () =>
  Math.max(Date.now() / 1000, state.logBuffer[state.logBuffer.length - 1]?.t || 0);

const buildCyphalEntry = (event, kind) => {
  let severity = null;
  let text = '';
  if (kind === 'diagnostic') {
    const r = extractRecordEntry(event);
    severity = r.severity;
    text = r.text;
  } else {
    text = firstStringValue(event);
  }
  // A replayed message carries the time it was recorded at, which may be days
  // ago: it is listed where it arrives, its row saying when it was recorded.
  const replayed = event.replay === true;
  return {
    id: ++state._logSeq,
    t: (!replayed && event.timestamp_unix) || _arrivalTime(),
    recordedT: replayed ? event.timestamp_unix ?? null : null,
    source: 'cyphal',
    kind,
    severity,
    nodeId: Number.isInteger(event.publisher_node_id) ? event.publisher_node_id : null,
    subjectId: event.subject_id ?? null,
    subjectName: typeof event.message_type === 'string' ? event.message_type : '',
    text,
  };
};

const buildServerEntry = (raw) => {
  const level = String(raw.level || '').toUpperCase();
  const severity = SERVER_LEVEL_TO_SEVERITY[level] ?? null;
  const tsMs = Date.parse(raw.timestamp || '');
  return {
    id: ++state._logSeq,
    t: Number.isFinite(tsMs) ? tsMs / 1000 : Date.now() / 1000,
    tsRaw: raw.timestamp || '',
    source: 'server',
    severity,
    level,
    loggerName: typeof raw.logger === 'string' ? raw.logger : '',
    text: typeof raw.message === 'string' ? raw.message : '',
  };
};

// ── Render ──────────────────────────────────────────────────────────

// "11:13:40.250": to the millisecond, as the Debug tab times its frames, so a
// message can be set beside the frames around it.
const formatLogTime = (unix) =>
  `${formatPlotTime(unix)}.${String(new Date(unix * 1000).getMilliseconds()).padStart(3, '0')}`;

const isAtBottom = (container) => {
  return container.scrollHeight - container.scrollTop - container.clientHeight <= AUTOSCROLL_STICKY_PX;
};

// The name of the node an entry comes from, kept from when it is first known:
// a list drawn again later finds the node gone with the bus, or its ID taken
// by another device.
const entryNodeName = (entry) => {
  if (!entry.nodeName) entry.nodeName = nodeDisplayName(entry.nodeId);
  return entry.nodeName;
};

// Who a row comes from: [html, plain text for the filter, tooltip].
const logRowSource = (entry) => {
  if (entry.source === 'server') {
    const logger = entry.loggerName || 'server';
    return [escapeHtml(logger), logger, `backend logger: ${logger}`];
  }
  if (entry.nodeId == null) return ['', '', ''];
  const name = entryNodeName(entry);
  const html = `<span class="log-row-nid">${escapeHtml(entry.nodeId)}</span>${name ? ` ${escapeHtml(name)}` : ''}`;
  return [html, `${entry.nodeId} ${name}`, `node ${entry.nodeId}${name ? ` (${name})` : ''}`];
};

// A small label before a row's message, its tooltip saying more.
const logRowTag = (label, title) =>
  `<span class="log-row-tag" title="${escapeHtml(title)}">${escapeHtml(label)}</span> `;

// time · severity · message, the source leading the message: one layout for
// both sources, so the columns line up and the message keeps the width.
const renderLogRow = (entry) => {
  const row = document.createElement('div');
  const sev = entry.severity;
  row.className = `log-row log-row-sev-${sev != null ? SEVERITY_CSS[sev] : 'none'} log-row-src-${entry.source}`;
  row.dataset.severity = String(sev ?? -1);
  row.dataset.source = entry.source;
  row.dataset.t = String(entry.t);
  const [srcHtml, srcText, srcTitle] = logRowSource(entry);
  const subject = entry.kind === 'subject' ? `s${entry.subjectId}` : '';
  const replay = entry.recordedT != null ? 'replay' : '';
  row.dataset.search = `${srcText} ${replay} ${subject} ${entry.text}`.toLowerCase();
  row.innerHTML = `
    <span class="log-row-time" title="${escapeHtml(new Date(entry.t * 1000).toLocaleString())}">${
      escapeHtml(formatLogTime(entry.t))}</span>
    <span class="log-row-sev" title="${escapeHtml(sev != null ? SEVERITY_LABELS[sev] : 'no severity')}">${
      sev != null ? SEVERITY_SHORT[sev] : '—'}</span>
    <span class="log-row-text">${srcHtml ? `<span class="log-row-src" title="${escapeHtml(srcTitle)}">${srcHtml}</span> ` : ''}${
      replay ? logRowTag(replay, `Replayed from a recording: recorded ${new Date(entry.recordedT * 1000).toLocaleString()}`) : ''}${
      subject ? logRowTag(subject, entry.subjectName) : ''}${
      escapeHtml(entry.text)}</span>`;
  return row;
};

// ── Filters ─────────────────────────────────────────────────────────

const _sourceIsShown = (src) => {
  if (src === 'cyphal') return state.logShowCyphal;
  if (src === 'server') return state.logShowServer;
  return true;
};

const applyAllFiltersToRow = (row) => {
  const sev = Number(row.dataset.severity);
  const floor = state.logSeverityFloor;
  const sevPasses = sev < 0 ? true : sev >= floor;
  const srcPasses = _sourceIsShown(row.dataset.source);
  const textPasses = !state.logTextFilter || row.dataset.search.includes(state.logTextFilter);
  row.classList.toggle('hidden', !(sevPasses && srcPasses && textPasses));
};

const reapplyFiltersToAllRows = () => {
  const list = el('logList');
  if (!list) return;
  for (const row of list.children) applyAllFiltersToRow(row);
  updateEmptyHint();
  clearLogNewBelow();  // other rows are in view now
};

// "a", "a and b", "a, b and c".
const _listInWords = (items) =>
  (items.length > 1 ? `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}` : items[0] || '');

// Every message kept is hidden: which settings hide some of them.
const _hiddenMessagesText = () => {
  const by = [];
  const floor = state.logSeverityFloor;
  if (state.logBuffer.some((e) => e.severity != null && e.severity < floor)) by.push(`Min ${SEVERITY_LABELS[floor]}`);
  if (state.logTextFilter) by.push(`the filter “${el('logFilterInput').value.trim()}”`);
  if (['cyphal', 'server'].some((src) => !_isSourceOn(src) && _srcCounts[src] > 0)) by.push('the sources turned on');
  const n = state.logBuffer.length;
  return n === 1 ? `The one message kept does not match ${_listInWords(by)}.`
    : `None of the ${n} messages kept match ${_listInWords(by)}.`;
};

// What the list says while it shows no message: why, and what would change
// it. '' when it shows some.
const _emptyHintText = () => {
  const on = ['cyphal', 'server'].filter(_isSourceOn);
  if (!on.length) return 'All log sources are off — enable one above.';
  if (state.logBuffer.length > 0) {
    return el('logList')?.querySelector('.log-row:not(.hidden)') ? '' : _hiddenMessagesText();
  }
  // No backend is one problem, though it stops both sources.
  const problems = on.map(logSourceProblem).filter(Boolean).map(_problemText);
  const stopped = [...new Set(problems)].join(' ');
  // With a source that can still send, it waits, and says what the other lacks.
  return problems.length === on.length ? stopped : `Waiting for log messages… ${stopped}`.trim();
};

const updateEmptyHint = () => {
  const empty = el('logEmpty');
  if (!empty) return;
  const text = _emptyHintText();
  if (text && empty.textContent !== text) empty.textContent = text;
  empty.classList.toggle('hidden', !text);
};

// ── Ingest (push entry into buffer + DOM) ───────────────────────────

// Where an entry with time ``t`` goes in the time-ordered buffer. Entries
// mostly arrive in order; the server's backlog arrives late, in one batch.
const _bufferIndexFor = (t) => {
  let i = state.logBuffer.length;
  while (i > 0 && state.logBuffer[i - 1].t > t) i--;
  return i;
};

// The row to insert a row with time ``t`` before: null to append, undefined
// if it is older than every row shown while the list is full.
const _rowAfter = (list, t) => {
  let after = null;
  for (let r = list.lastElementChild; r && Number(r.dataset.t) > t; r = r.previousElementSibling) after = r;
  if (after === list.firstElementChild && after && list.childElementCount >= LOG_CAP) return undefined;
  return after;
};

const _pushEntry = (entry) => {
  state.logBuffer.splice(_bufferIndexFor(entry.t), 0, entry);
  if (entry.source in _srcCounts) _srcCounts[entry.source]++;
  if (state.logBuffer.length > LOG_CAP) {
    const excess = state.logBuffer.length - LOG_CAP;
    for (let i = 0; i < excess; i++) {
      const s = state.logBuffer[i].source;
      if (s in _srcCounts) _srcCounts[s]--;
    }
    state.logBuffer.splice(0, excess);
  }
  _refreshSourcePillBadges();
  const list = el('logList');
  if (!list) return;
  const wasAtBottom = isAtBottom(list.parentElement);
  const after = _rowAfter(list, entry.t);
  let listed = false;  // drawn, and not hidden by a filter
  if (after !== undefined) {
    const row = renderLogRow(entry);
    applyAllFiltersToRow(row);
    list.insertBefore(row, after);
    listed = !row.classList.contains('hidden');
  }
  if (listed && state.logPanelCollapsed && entry.severity >= SEVERITY_WARNING) {
    state.logUnreadCount++;
    state.logUnreadWorst = Math.max(state.logUnreadWorst, entry.severity);
    renderLogToggle();
  }
  while (list.childElementCount > LOG_CAP) {
    list.removeChild(list.firstElementChild);
  }
  updateEmptyHint();
  if (state.logAutoscroll && wasAtBottom) {
    list.parentElement.scrollTop = list.parentElement.scrollHeight;
  } else if (listed && after === null && !state.logPanelCollapsed && !isAtBottom(list.parentElement)) {
    // Added under what is in view, which keeps its place: the cue counts it.
    state.logNewBelow++;
    state.logNewBelowWorst = Math.max(state.logNewBelowWorst, entry.severity ?? 0);
    renderLogNewBelow();
  }
};

// Cyphal WS hook (called unconditionally from connection.js).
const ingestLogEvent = (event) => {
  let kind;
  if (isDiagnosticRecord(event)) kind = 'diagnostic';
  else if (isUserLoggedSubject(event)) kind = 'subject';
  else return;
  const entry = buildCyphalEntry(event, kind);
  if (kind === 'subject' && !entry.text) return;
  _pushEntry(entry);
};

const ingestServerEntry = (raw) => {
  _pushEntry(buildServerEntry(raw));
};

// ── Rebuild (used after major UI state changes) ─────────────────────

const rebuildLogList = () => {
  const list = el('logList');
  if (!list) return;
  list.replaceChildren();
  for (const entry of state.logBuffer) {
    const row = renderLogRow(entry);
    applyAllFiltersToRow(row);
    list.appendChild(row);
  }
  updateEmptyHint();
  clearLogNewBelow();
  if (state.logAutoscroll) {
    list.parentElement.scrollTop = list.parentElement.scrollHeight;
  }
};

// ── Subject picker popover ──────────────────────────────────────────

const getCandidateSubjects = () => {
  const out = [];
  for (const [sid, event] of state.latestBySubject) {
    // Fixed subject-IDs are the standard messages (heartbeat, port list,
    // diagnostics): their strings are enum names, not text to log.
    if (sid >= FIRST_FIXED_SUBJECT_ID) continue;
    if (!hasStringAttribute(event)) continue;
    out.push({
      subjectId: sid,
      messageType: typeof event.message_type === 'string' ? event.message_type : '',
    });
  }
  for (const sid of state.logSubjectIds) {
    if (sid === DIAGNOSTIC_SUBJECT_ID) continue;
    if (!out.some(s => s.subjectId === sid)) {
      out.push({ subjectId: sid, messageType: '' });
    }
  }
  out.sort((a, b) => a.subjectId - b.subjectId);
  return out;
};

const ensureLogPickerPopover = () => {
  let popover = el('logSubjectPickerPopover');
  if (popover) return popover;
  popover = document.createElement('div');
  popover.id = 'logSubjectPickerPopover';
  popover.className = 'hidden-popover log-subject-popover hidden';
  document.body.appendChild(popover);
  return popover;
};

const renderLogPickerPopover = () => {
  const popover = ensureLogPickerPopover();
  const candidates = getCandidateSubjects();

  let listHtml = '';
  if (candidates.length === 0) {
    listHtml = '<div class="log-subject-empty">No subject with a text field seen yet.</div>';
  } else {
    listHtml = '<div class="hidden-popover-list">';
    for (const c of candidates) {
      const checked = state.logSubjectIds.has(c.subjectId);
      const title = c.messageType ? `subject ${c.subjectId} — ${c.messageType}` : `subject ${c.subjectId}`;
      listHtml += `
        <label class="log-subject-row" title="${escapeHtml(title)}">
          <span class="hidden-popover-id">${escapeHtml(String(c.subjectId))}</span>
          <span class="log-subject-type">${escapeHtml(c.messageType || '—')}</span>
          <input type="checkbox" class="log-subject-checkbox" data-subject-id="${c.subjectId}" ${checked ? 'checked' : ''}>
        </label>
      `;
    }
    listHtml += '</div>';
  }

  popover.innerHTML = `
    <div class="hidden-popover-header">
      <span>Log subjects with text</span>
    </div>
    ${listHtml}
  `;

  for (const cb of popover.querySelectorAll('.log-subject-checkbox')) {
    cb.addEventListener('change', () => {
      const sid = parseInt(cb.dataset.subjectId, 10);
      if (!Number.isInteger(sid)) return;
      if (cb.checked) state.logSubjectIds.add(sid);
      else state.logSubjectIds.delete(sid);
      saveSettings();
    });
  }
};

// Shuts the picker; its button says so. Escape and a click elsewhere (app.js) do it too.
const closeLogPicker = () => {
  el('logSubjectPickerPopover')?.classList.add('hidden');
  el('logAddSubjectBtn')?.setAttribute('aria-expanded', 'false');
};

const toggleLogPickerPopover = () => {
  const popover = ensureLogPickerPopover();
  const btn = el('logAddSubjectBtn');
  if (!btn) return;
  if (!popover.classList.contains('hidden')) {
    closeLogPicker();
    return;
  }
  renderLogPickerPopover();
  positionPopover(popover, btn);
  popover.classList.remove('hidden');
  btn.setAttribute('aria-expanded', 'true');
};

// ── Server log poller ───────────────────────────────────────────────

let _serverPollTimer = null;
let _serverLastSeenTs = '';
let _serverKeysAtLastTs = new Set();  // entries already taken at that timestamp

let _serverLogError = '';              // why the last request for the log failed, or ''
let _serverLogRead = false;            // whether a request for the log has been answered yet

const _serverEntryKey = (e) => `${e.timestamp}|${e.logger}|${e.message}`;

// Stands where server records are missing: more came between two requests
// than one request takes, and the oldest of them were left out.
const _serverGapEntry = (next) => buildServerEntry({
  timestamp: next.timestamp,
  level: 'WARNING',
  logger: 'log panel',
  message: `Server records are missing here: more than ${SERVER_LOG_FETCH_LIMIT} came between two requests.`,
});

const _fetchServerLogsOnce = async () => {
  _refreshSourcePillBadges();
  if (!state.dashboardConnected) return;
  try {
    // Only what came since the newest record taken, which comes back first.
    const since = _serverLastSeenTs ? `&since=${encodeURIComponent(_serverLastSeenTs)}` : '';
    const data = await requestJson(`/api/logs?limit=${SERVER_LOG_FETCH_LIMIT}${since}`);
    const logs = Array.isArray(data.logs) ? data.logs : [];
    _serverLogError = '';
    // As many as one request takes, the first of them newer than the newest
    // taken: some in between were left out. Not so for the first answer,
    // which is the backlog.
    const full = logs.length >= SERVER_LOG_FETCH_LIMIT;
    if (_serverLogRead && full && (!_serverLastSeenTs || logs[0].timestamp > _serverLastSeenTs)) {
      _pushEntry(_serverGapEntry(logs[0]));
    }
    _serverLogRead = true;
    // Oldest first. Several entries can share a timestamp, so one equal to the
    // newest seen is new unless that very entry was taken.
    for (const e of logs) {
      const ts = e?.timestamp || '';
      if (!ts || ts < _serverLastSeenTs) continue;
      const key = _serverEntryKey(e);
      if (ts === _serverLastSeenTs && _serverKeysAtLastTs.has(key)) continue;
      ingestServerEntry(e);
      if (ts > _serverLastSeenTs) {
        _serverLastSeenTs = ts;
        _serverKeysAtLastTs = new Set();
      }
      _serverKeysAtLastTs.add(key);
    }
  } catch (error) {
    // Tried again on the next tick; the pill says why meanwhile.
    _serverLogError = error.message;
  }
  _refreshSourcePillBadges();
};

const startServerLogPoll = () => {
  if (_serverPollTimer) return;
  _fetchServerLogsOnce();
  _serverPollTimer = setInterval(_fetchServerLogsOnce, SERVER_LOG_POLL_MS);
};

const stopServerLogPoll = () => {
  if (_serverPollTimer) {
    clearInterval(_serverPollTimer);
    _serverPollTimer = null;
  }
};

// ── Toolbar bits ────────────────────────────────────────────────────

const updateAutoscrollBtn = () => {
  const btn = el('logAutoscrollBtn');
  if (!btn) return;
  btn.classList.toggle('active', state.logAutoscroll);
  btn.setAttribute('aria-pressed', String(state.logAutoscroll));
};

const _isSourceOn = (src) =>
  src === 'cyphal' ? state.logShowCyphal
  : src === 'server' ? state.logShowServer
  : false;

const _setSourceOn = (src, on) => {
  if (src === 'cyphal') state.logShowCyphal = on;
  else if (src === 'server') state.logShowServer = on;
};

const _refreshSourcePill = (pill) => {
  const on = _isSourceOn(pill.dataset.source);
  pill.setAttribute('aria-pressed', String(on));
};

const _onSourceToggle = (pill) => {
  const src = pill.dataset.source;
  const next = !_isSourceOn(src);
  _setSourceOn(src, next);
  _refreshSourcePill(pill);
  if (src === 'server') {
    if (next) startServerLogPoll();
    else stopServerLogPoll();
  }
  reapplyFiltersToAllRows();
  _refreshSourcePillBadges();
  updateEmptyHint();
  saveSettings();
};

// ── Init ────────────────────────────────────────────────────────────

const initLogPanel = () => {
  const panel = el('logPanel');
  const collapseBtn = el('logPanelCollapseBtn');
  const resizeHandle = el('logPanelResizeHandle');
  if (!panel || !collapseBtn || !resizeHandle) return;

  panel.style.width = '';
  panel.style.minWidth = '';

  if (state.logPanelCollapsed) {
    panel.classList.add('collapsed');
  } else {
    panel.classList.remove('collapsed');
    applyLogPanelWidth(panel);
  }

  renderLogToggle();

  collapseBtn.addEventListener('click', () => {
    state.logPanelCollapsed = !state.logPanelCollapsed;
    panel.classList.toggle('collapsed', state.logPanelCollapsed);
    state.logUnreadCount = 0;  // opened, they are read; shut, the count starts again
    state.logUnreadWorst = 0;
    renderLogToggle();
    if (!state.logPanelCollapsed) {
      applyLogPanelWidth(panel);
      rebuildLogList();
    }
    saveSettings();
  });

  let startX = 0;
  let startW = 0;
  const shell = panel.closest('.app-shell');

  const onMove = (e) => {
    const dx = startX - e.clientX;
    const newW = Math.max(LOG_PANEL_MIN_WIDTH_PX, Math.min(LOG_PANEL_MAX_WIDTH_PX, startW + dx));
    if (shell) shell.style.setProperty('--log-w', newW + 'px');
  };

  const onUp = () => {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    panel.classList.remove('no-transition');
    resizeHandle.classList.remove('dragging');
    state.logPanelWidth = panel.getBoundingClientRect().width;
    saveSettings();
  };

  resizeHandle.addEventListener('mousedown', (e) => {
    if (state.logPanelCollapsed) return;
    e.preventDefault();
    startX = e.clientX;
    startW = panel.getBoundingClientRect().width;
    panel.classList.add('no-transition');
    resizeHandle.classList.add('dragging');
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  resizeHandle.addEventListener('dblclick', () => {
    state.logPanelWidth = null;
    applyLogPanelWidth(panel);
    saveSettings();
  });

  const sevSelect = el('logSeveritySelect');
  if (sevSelect) {
    sevSelect.value = String(state.logSeverityFloor);
    sevSelect.addEventListener('change', () => {
      const v = parseInt(sevSelect.value, 10);
      state.logSeverityFloor = Number.isInteger(v) ? Math.max(0, Math.min(7, v)) : 0;
      reapplyFiltersToAllRows();
      saveSettings();
    });
  }

  const filterInput = el('logFilterInput');
  if (filterInput) {
    filterInput.addEventListener('input', () => {
      state.logTextFilter = filterInput.value.trim().toLowerCase();
      reapplyFiltersToAllRows();
    });
  }

  const autoBtn = el('logAutoscrollBtn');
  if (autoBtn) {
    autoBtn.addEventListener('click', () => {
      state.logAutoscroll = !state.logAutoscroll;
      updateAutoscrollBtn();
      if (state.logAutoscroll) {
        const list = el('logList');
        if (list) list.parentElement.scrollTop = list.parentElement.scrollHeight;
      }
      saveSettings();
    });
    updateAutoscrollBtn();
  }

  const clearBtn = el('logClearBtn');
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      state.logBuffer.length = 0;
      _srcCounts.cyphal = 0;
      _srcCounts.server = 0;
      const list = el('logList');
      if (list) list.replaceChildren();
      _refreshSourcePillBadges();
      updateEmptyHint();
      clearLogNewBelow();
    });
  }

  // The count of messages below goes once they are in view: by its own click,
  // or by scrolling there.
  const body = el('logPanelBody');
  const newBtn = el('logNewBtn');
  if (body && newBtn) {
    newBtn.addEventListener('click', () => {
      body.scrollTop = body.scrollHeight;
      clearLogNewBelow();
    });
    body.addEventListener('scroll', () => {
      if (state.logNewBelow && isAtBottom(body)) clearLogNewBelow();
    }, { passive: true });
  }

  const addBtn = el('logAddSubjectBtn');
  if (addBtn) {
    ensureLogPickerPopover();
    addBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleLogPickerPopover();
    });
    // Escape shuts the picker and gives the focus back to its button.
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape' || el('logSubjectPickerPopover').classList.contains('hidden')) return;
      closeLogPicker();
      addBtn.focus();
    });
  }

  for (const pill of document.querySelectorAll('.log-source-pill')) {
    _refreshSourcePill(pill);
    pill.addEventListener('click', () => _onSourceToggle(pill));
  }

  if (state.logShowServer) startServerLogPoll();

  _recomputeSrcCounts();
  _refreshSourcePillBadges();
  updateEmptyHint();
};
