# Landing Page Test Results

**Passed:** 279 | **Failed:** 0 | **Total:** 279


| Test | Status | Details |
|------|--------|---------|
| Page loads with correct title | PASS |  |
| Sidebar is visible on load | PASS |  |
| Server semaphore starts disconnected | PASS |  |
| CAN semaphore starts disconnected | PASS |  |
| Nodes table has correct headers | PASS |  |
| Empty table shows 'Not connected' | PASS |  |
| Filter row has 13 inputs | PASS |  |
| Uptime reads at a glance; narrow tables drop the least telling columns | PASS |  |
| API URL input has default value | PASS |  |
| Connect button shows 'Connect' | PASS |  |
| Theme toggle has aria-label | PASS |  |
| Sidebar collapse button has aria-label | PASS |  |
| Theme toggle switches theme | PASS |  |
| Sidebar collapse toggles both ways | PASS |  |
| Sortable columns exist | PASS |  |
| Cards: a diagnostic's severity by name, as the log panel names it, and a zero timestamp as unknown | PASS |  |
| History: Cynitor's register writes and firmware updates are rows of the node's history | PASS |  |
| Log panel shows diagnostics' severity, in time order | PASS |  |
| Log filter hides other rows and says when none match | PASS |  |
| Cyphal v1.1 traffic is noticed under the CAN status | PASS |  |
| Graph: a subject that stops publishing goes silent | PASS |  |
| Graph: an edge carries its own publisher's rate, an offline one none | PASS |  |
| Graph: devices that lost their node-ID keep their own identity | PASS |  |
| Graph: only what is unusual is coloured, and nothing pulses | PASS |  |
| Graph: subjects are labelled by their ID first | PASS |  |
| Graph: the layered layout puts devices and subjects in bands of their own | PASS |  |
| Graph: hovering an edge says what it carries | PASS |  |
| Graph: a click selects a node without moving or pinning it | PASS |  |
| Graph: the open inspector keeps its buttons while it refreshes, and draws rate sparklines | PASS |  |
| Graph: the inspector docks beside the canvas, follows its rows, and Esc closes it | PASS |  |
| Graph: Enter in the filter goes to the match; a focused node selects on Enter | PASS |  |
| Graph: the drawing exports as an SVG file | PASS |  |
| Graph: a node heard only in Cyphal v1.1 is drawn, marked not decoded | PASS |  |
| Graph: the legend opens from its button | PASS |  |
| Graph: the status strip counts what needs a look, and picks it out | PASS |  |
| Graph: 'Open in Nodes' selects the device in the Nodes tab | PASS |  |
| Graph: 'Nodes only' honours Hide system and shows direction | PASS |  |
| Graph: traffic animates only on request, and never under reduced motion | PASS |  |
| Graph: Fit brings every node into view | PASS |  |
| Graph: service calls are drawn from client to server | PASS |  |
| Graph: recent restarts and node-ID conflicts are said on the node | PASS |  |
| Graph: resizing the window keeps the drawing at screen scale | PASS |  |
| Tables: a subject that stops publishing loses its rate, in the table and on its card | PASS |  |
| Tables: a card's rate follows the live rate; a subscriber's card shows the subject's | PASS |  |
| Registers: a node that does not answer says so, not that the backend is unreachable | PASS |  |
| Tables: offline nodes that lost their node-ID stay last; health sorts by severity | PASS |  |
| Tables: a sorted table sorts again as values change, not under the pointer | PASS |  |
| Tables: ID filters match whole IDs; the selected row shows no grey through | PASS |  |
| Tables: VSSC shows each heartbeat's vendor status code, 0 muted | PASS |  |
| Tables: rows are reached and selected by keyboard | PASS |  |
| Tables: a port list shows its first IDs and how many more; the tooltip has it whole | PASS |  |
| Tables: the Nodes strip counts what needs a look, and a count picks those rows out | PASS |  |
| Tables: Hide offline takes every offline node out of the Nodes table at once, says how many, and is remembered | PASS |  |
| Tables: Compact makes the rows of both tables shorter, and is remembered | PASS |  |
| Tables: a node that lost its node-ID keeps its alias, and is forgotten only when confirmed | PASS |  |
| Tables: the Clients tab says the bus is not connected | PASS |  |
| Plots: a node's card plots its own messages, a vector one line per element | PASS |  |
| Plots: the Nodes tab's plot has the Subjects tab's controls, shared | PASS |  |
| Plots: Subjects plots each publisher of a subject apart | PASS |  |
| Plots: Fill Rate keeps up once a field's history is full | PASS |  |
| Compare: a subject several nodes publish is compared one publisher at a time | PASS |  |
| Plots: Compare opens what a node's plot shows in a new Compare graph | PASS |  |
| Plots: a graph made from a plot before Compare has opened gets an id of its own | PASS |  |
| Tables: Subjects say how long ago they were heard, and how many bytes a second | PASS |  |
| Tables: the Subjects strip lists one kind, and picks out the silent | PASS |  |
| Tables: Subjects says silent, and refreshes with nothing arriving | PASS |  |
| Tables: a table scrolled down is still there after a tab switch | PASS |  |
| Compare: graphs that do not fit keep their height, and the list scrolls | PASS |  |
| Compare: a graph resumed after Pause All moves on, and Pause All says what it does | PASS |  |
| Compare: a graph paused by a click on its plot says so on its own pause button, to screen readers too | PASS |  |
| Compare: a click on a plot pauses it only when the graph's Click pauses is on, and it is kept | PASS |  |
| Compare: a paused graph keeps what it showed, in a replay too | PASS |  |
| Compare: a series keeps to the node it was added from | PASS |  |
| Compare: a series with nothing to plot stays in the legend, and colours stay put | PASS |  |
| Compare: a graph says why it shows nothing | PASS |  |
| Compare: saved graphs and clones keep the whole graph, and untitled ones keep apart | PASS |  |
| Compare: Import checks the file first, and asks before it replaces the graphs | PASS |  |
| Compare: plots take the colours of the theme in use, loaded in it too | PASS |  |
| Compare: the wheel scrolls past the graphs, Ctrl+wheel zooms one | PASS |  |
| Compare: the y-axis fits what is in view, thresholds included | PASS |  |
| Compare: the tooltip keeps a value's digits, and has none for a series gone quiet | PASS |  |
| Compare: subject 0 is compared like any other | PASS |  |
| Compare: a threshold's pill hides its line, and its style stays a line | PASS |  |
| Compare: with Fill Rate on, a cleared history leaves no old line | PASS |  |
| Compare: series are picked, shown, removed and saved graphs opened by keyboard | PASS |  |
| Compare: the marker form saves on Enter and closes on Escape | PASS |  |
| Compare: graphs made, dragged and removed leave no listeners behind | PASS |  |
| Compare: a line draws what is in view, at most two points a pixel, and no spike is lost | PASS |  |
| Compare: a redraw writes lines in whole tenths of a pixel, and filters a publisher's points once | PASS |  |
| Compare: synced graphs pause, take a time window and zoom together, and a graph that joins takes their view | PASS |  |
| Compare: synced graphs show each other's markers, and a graph that leaves no longer does | PASS |  |
| Compare: a synced graph above another synced graph leaves its time labels to it, and draws taller | PASS |  |
| Compare: Collapse All collapses every graph, Expand All expands them, and the button says which it does | PASS |  |
| Compare: a collapsed graph is its plot, a line of names and, at its side, its pause, window and Sync | PASS |  |
| Compare: Min/Max follows the lowest and highest of the last samples, and an Add says what it is missing | PASS |  |
| Compare: a graph scrolled out of view is not drawn, and catches up in view | PASS |  |
| Compare: removing a graph with markers or drawings asks first | PASS |  |
| Compare: a graph's editing rows fold away under Edit, and a new graph starts on them | PASS |  |
| Compare: series are found by any part of their name, and ticked on and off in one list | PASS |  |
| Compare: with no graph, the tab says how to start, and starts one | PASS |  |
| Compare: its everyday buttons are plain, as in the Graph tab: colour is for the unusual | PASS |  |
| Compare: a strip counts the graphs, and picks out quiet series and paused graphs | PASS |  |
| Compare: a window longer than the history keeps says so | PASS |  |
| Compare: a plot carries its graph's name, not the word Compare | PASS |  |
| Compare: the legend lists each series' last, lowest and highest value in view, lined up | PASS |  |
| Compare: a graph's parts show and hide by class, not inline style, and a dragged plot shows a grabbing hand | PASS |  |
| Compare: thresholds, drawings and markers left at their default colour take the theme's | PASS |  |
| Compare: each graph clips its lines to its own plot, not to the first graph's | PASS |  |
| Compare: drawings and markers scrolled past the y-axis are cut there, not drawn over it | PASS |  |
| Compare: a series' and a derived series' colours are picked from the legend | PASS |  |
| Compare: a graph lists its markers and drawings, shows a marker gone off screen, and removes them | PASS |  |
| DSDL: a subject's bus dot goes on its own type, not on another with its class name | PASS |  |
| DSDL: a type just created saves again, as an edit | PASS |  |
| DSDL: opened before connecting, the tab stops saying 'Not connected.' once connected | PASS |  |
| DSDL: a compiled custom type can be deleted, and the tab says how to change it | PASS |  |
| DSDL: compile and editor errors are drawn in the error colour | PASS |  |
| DSDL: the editor's example is a type that compiles | PASS |  |
| DSDL: a fixed port ID the compiler would refuse is not saved, and the editor says which it takes | PASS |  |
| DSDL: a compile keeps its button busy until it ends, and is not asked for twice | PASS |  |
| DSDL: the namespace Create and delete-confirm buttons keep their colours under the pointer | PASS |  |
| DSDL: long field types wrap, and never cut off or run into the field names | PASS |  |
| DSDL: when the server goes away the tab says so, and comes back with it | PASS |  |
| DSDL: the tree says it is loading until the types arrive | PASS |  |
| DSDL: the time since the last compile follows the clock | PASS |  |
| DSDL: a refused namespace or delete is said where it was asked for | PASS |  |
| DSDL: a public recompile leaves the custom types' compile error alone | PASS |  |
| DSDL: the editor asks before unsaved changes are dropped | PASS |  |
| DSDL: a type busy on the bus shows a few of its publishers, and how many more | PASS |  |
| DSDL: the type tree works by keyboard, one Tab stop and the arrows | PASS |  |
| DSDL: a custom namespace's buttons and the 'Depends on' links work by keyboard | PASS |  |
| DSDL: screen readers hear the editor's fields and the results, and the bus dots hold still on request | PASS |  |
| DSDL: a type's pane says whether it is a union, deprecated, and how many bytes it takes | PASS |  |
| DSDL: a type's pane gives its description, and the comments on its fields and constants | PASS |  |
| DSDL: a custom type that does not compile says why, with its line marked in the source | PASS |  |
| DSDL: a first type is written from a 'New type' button, its namespace typed in | PASS |  |
| DSDL: 'New version' opens a copy of a custom type at the next version not taken | PASS |  |
| DSDL: an empty custom namespace can be deleted, one with types cannot | PASS |  |
| DSDL: types are listed by name, then by version | PASS |  |
| DSDL: the search sits above the trees and says how many types match | PASS |  |
| DSDL: your own types come first, above the public ones | PASS |  |
| DSDL: the search finds a type by its compiled name, as messages and recordings give it | PASS |  |
| DSDL: the search finds a type by one of its constants | PASS |  |
| DSDL: the tree keeps colour for the unusual: a service's tag, a type not compiled yet | PASS |  |
| DSDL: a type's pane keeps colour for the unusual: not compiled, deprecated | PASS |  |
| DSDL: a type's buttons keep their height when the note beside them wraps | PASS |  |
| DSDL: the editor's preview shows its own source, not a closed editor's | PASS |  |
| DSDL: the tab is styled by its CSS, in rem, its splits still dragged and its rows indented as before | PASS |  |
| DSDL: the tree keeps its width across reloads, saved in rem, a px one from earlier versions too | PASS |  |
| DSDL: an editor open on a custom type locks itself once the type is compiled | PASS |  |
| DSDL: 'New version' from a locked editor opens a draft that can be edited | PASS |  |
| DSDL: after a compile that fails, the tree shows at once what did compile | PASS |  |
| DSDL: a compile error wraps between words, never inside one | PASS |  |
| Record: an export downloads the file, and one that fails keeps the dashboard and says why | PASS |  |
| Record: the tab opens without errors | PASS |  |
| Record: Start makes one recording however fast it is clicked | PASS |  |
| Record: Start is off, and says why, until CAN is connected | PASS |  |
| Record: Edit limits shows a recording's own limits, and sends only what is changed | PASS |  |
| Record: a recording's times count whole seconds, never 60 of them | PASS |  |
| Record: a list that cannot be loaded says so once, in place, and keeps what it showed | PASS |  |
| Record: opening the tab asks for each list once, and nothing while disconnected | PASS |  |
| Record: Play is off, saying why, for a recording with nothing to replay | PASS |  |
| Record: the size estimate is for what the selection would record, at the rate it is sent now | PASS |  |
| Record: a subject several nodes publish lists them all, at its total rate, and lights up for each | PASS |  |
| Record: the pickers work by keyboard, their refreshes keeping the focus | PASS |  |
| Record: a row keeps the keyboard focus when its table is drawn again | PASS |  |
| Record: Edit limits takes the focus, keeps Tab inside, and gives the focus back | PASS |  |
| Record: a recording's progress bars say what they measure, and how far it got | PASS |  |
| Record: the recording dots hold still on request | PASS |  |
| Record: Start is at the top of the tab, above the recordings however many they are | PASS |  |
| Record: a recording needs no name, taking its start time, and the next one starts with an empty name | PASS |  |
| Record: Save the last minutes keeps what the bus sent before it was pressed | PASS |  |
| Record: a service says it records all its calls on the bus | PASS |  |
| Record: a recording's name is shown whole, its times under it | PASS |  |
| Record: an auto-stopped recording says which limit stopped it, in amber only for the events cap | PASS |  |
| Record: a recording running while CAN is down says it waits for CAN | PASS |  |
| Record: the recording being replayed is marked on its card | PASS |  |
| Record: the two pickers line up, however many lines their headings take | PASS |  |
| Record: a card shows its main actions, the others in a menu that stays open as the card is drawn again | PASS |  |
| Record: a raw log's playback speed sits with the saved logs, and is remembered | PASS |  |
| Record: the Record tab's dot shows while a raw log runs | PASS |  |
| Record: the tab is styled by its CSS, in rem, without !important | PASS |  |
| Record: the history chip names the history, and is not drawn while it is unknown | PASS |  |
| Record: the pickers show types and node names whole, the columns of numbers as wide as theirs | PASS |  |
| Record: the tab takes one column when the log panel leaves too little room for two | PASS |  |
| Record: a service's row in the picker is not cut short | PASS |  |
| Record: an empty picker says why, and what to do, in the tab's own text | PASS |  |
| Record: the search lists the recordings whose name, notes or selection hold what is typed, and the one being made | PASS |  |
| Replay strip: an hour-long replay reads h:mm:ss, its counters are not read out each second, and its end counts every event | PASS |  |
| Debug: a page reopened on the tab asks once a second, and shows the bus's own frame rate | PASS |  |
| Debug: disconnected, the tab asks the backend nothing and says why, so a token can be typed in peace | PASS |  |
| Debug: the diagnostics cards show every row, and scroll when the window is short | PASS |  |
| Debug: a capture whose connection closes stops, and says so | PASS |  |
| Debug: Start capture shows the frames caught before it, then the live ones, each once | PASS |  |
| Debug: what comes while the frame table is paused shows on Resume | PASS |  |
| Debug: the frame filter shows the same rows whenever it is typed, and says when none match | PASS |  |
| Debug: screen readers are not read the frame counters on every batch | PASS |  |
| Debug: Start capture is off, saying why, until there is a bus to capture from | PASS |  |
| Debug: capture changes nothing on the bus: Stop ends it, and nothing says the bus stays in capture mode | PASS |  |
| Debug: the frame table keeps its frames, filter and pause across tabs, and takes in what came meanwhile | PASS |  |
| Debug: the diagnostics show what the interface reports, in the sidebar's units, and nothing it does not | PASS |  |
| Debug: the diagnostics update in place, and a failed update keeps the last values, marked stale | PASS |  |
| Debug: colour marks only the unusual, in the theme's own colours: own frames, lost frames, a bus in trouble | PASS |  |
| Debug: the bus's health comes first, in one strip, with the details folded beneath it | PASS |  |
| Debug: the frame table writes a standard ID in three digits, and gives a Cyphal frame's priority, transfer-ID and tail bits | PASS |  |
| Debug: the frame filter takes node:, port:, dir: and the like, and a leading - leaves frames out | PASS |  |
| Debug: every frame kept can be scrolled to, the page drawing only the rows in view, which hold still as frames come | PASS |  |
| Debug: each frame says how long after the frame shown before it it came, to the µs: with a filter, a subject's period | PASS |  |
| Debug: By ID gives each CAN ID a row, with its count, cycle and last data, and marks one gone quiet | PASS |  |
| Debug: error frames show in the trace, in red, saying what went wrong, and are counted | PASS |  |
| Debug: Save to a raw log opens the Record tab at its raw log, which keeps every frame | PASS |  |
| Test nodes: one is made in the editor, its fields built from its type, a waveform sent as the API takes it | PASS |  |
| Test nodes: the node-ID is left to Cynitor unless one is typed, and the card shows the one it was given | PASS |  |
| Test nodes: a definition the server refuses says why in the editor, which keeps what was typed | PASS |  |
| Test nodes: on a real bus Start asks first, and only a confirmed start goes again | PASS |  |
| Test nodes: health and mode change from the card, sent with the rest of the node as it is | PASS |  |
| Test nodes: without CAN, Start is off and the tab says the simulated bus needs no hardware | PASS |  |
| Test nodes: a running one is marked in the Nodes table and the Graph, and the tab has a dot meanwhile | PASS |  |
| Sidebar: collapsed, none of its controls takes the focus, and its toggle says it is collapsed | PASS |  |
| Sidebar: CAN Disconnect takes the bus's nodes and load curve with it, rather than leave them looking live | PASS |  |
| Sidebar: errors on the bus show under the CAN status, its dot amber, until they stop | PASS |  |
| Sidebar: a newer release the server found is a link by the version, to its release page | PASS |  |
| Sidebar: transmit errors alone say no other node acknowledges, not to check termination | PASS |  |
| Sidebar: a problem the backend reports while connected shows under the CAN form, and goes with it | PASS |  |
| Sidebar: a backend that stops answering is reconnected once it is back, its session kept meanwhile | PASS |  |
| Sidebar: a page opened while the backend is down connects once it answers | PASS |  |
| Sidebar: when the backend serving the page is gone a while, the page comes back connected after its reload | PASS |  |
| Sidebar: the CAN interface picked last is picked again, its bitrate too, though settings were saved before connecting | PASS |  |
| Sidebar: an adapter missing from the list for a moment is picked again once it is back | PASS |  |
| Sidebar: an adapter with CAN FD data bitrate presets is offered only those, and loses a pick it cannot run | PASS |  |
| Sidebar: the simulated bus connects without a bitrate, picked from the list or typed under Other… | PASS |  |
| Sidebar: why a CAN connect failed, or the session ended, stays under the CAN form until the next try | PASS |  |
| Sidebar: screen readers hear what its fields are, and which Connect is the server's and which is CAN's | PASS |  |
| Sidebar: a backend that wants a token says so without calling itself unreachable; the prompt can be left, and keeps what is typed | PASS |  |
| Sidebar: a CAN connect slower than other requests is waited for, its button saying Connecting… | PASS |  |
| Sidebar: with the server not connected, the CAN form is off, saying to connect the server first | PASS |  |
| Sidebar: the outage overlay names the server that serves the page, and how to start that one | PASS |  |
| Sidebar: it needs no scrollbar of its own, its version reads, and its logos show in both themes | PASS |  |
| Sidebar: in a narrow window, collapsed, it leaves no blank behind, and its toggle stays put under the pointer | PASS |  |
| Sidebar: Cynitor allocates node-IDs only once its box is ticked; Connect says so, and the choice is kept | PASS |  |
| Sidebar: Listen only connects sending nothing; while it runs the box says so, and the choice is kept | PASS |  |
| Sidebar: an allocator that another on the bus has silenced names the node that allocates there, until CAN disconnects | PASS |  |
| Sidebar: decoding on demand is switched in the Server section, and the switch follows what the server says | PASS |  |
| Sidebar: the server is told which subjects' values are on screen, once, and again when they change | PASS |  |
| Sidebar: the server's CPU use shows under its data rate, amber when high and red when it is full | PASS |  |
| Themes: muted text reads against the page and its panels, in the dark theme as in the light one | PASS |  |
| Log panel: the server's log is asked only for what came since the last record shown | PASS |  |
| Log panel: server records it could not get are marked as missing, where they are missing | PASS |  |
| Log panel: missing server records are marked also when it had none to show before, but a first backlog is not | PASS |  |
| Log panel: a server log that cannot be read marks its pill, saying why, until it can | PASS |  |
| Log panel: every message it counts is in its list, so the filters find the oldest too | PASS |  |
| Log panel: a replayed message is listed where it arrives, marked, with the time it was recorded | PASS |  |
| Log panel: shut, none of its controls takes the focus, and its toggle says whether it is open | PASS |  |
| Log panel: a row keeps its node's name once the bus is gone and the list is drawn again | PASS |  |
| Log panel: a type of your own called Record is not taken for a diagnostic record | PASS |  |
| Log panel: markup in a message's node-ID or text is shown as text, never run | PASS |  |
| Log panel: a text subject whose first string is empty is logged by the next one that says something | PASS |  |
| Log panel: a width saved on a wide screen leaves the main area room in a smaller window, and is never too narrow to read | PASS |  |
| Log panel: in a narrow window, where it sits under everything else, the page scrolls down to it | PASS |  |
| Log panel: a source's pill says what it is, and, turned on with nothing to come from, why | PASS |  |
| Log panel: empty, it says why: no backend, no bus, or what hides the messages it has | PASS |  |
| Log panel: shut, its toggle counts the warnings and errors that came since, in the colour of the worst | PASS |  |
| Log panel: scrolled up, it counts the messages that come below, and a click goes to them | PASS |  |
| Log panel: Escape shuts the subject picker, and its button says whether it is open | PASS |  |
| Log panel: a message's time is to the millisecond, as the Debug tab's frames, with its date in the tooltip | PASS |  |
| Log panel: every severity's label reads against its row in both themes, and DEBUG takes no colour | PASS |  |
| Log panel: a message said again within seconds adds to its row's count, not a row | PASS |  |
| Log panel: Save writes the messages listed, filters applied, to a file, and is off with none listed | PASS |  |
| Log panel: its width is kept across reloads, saved in rem, a px one from earlier versions too | PASS |  |
| Log panel: the Server source lists the server's records with their level and logger, and asks only while it is on | PASS |  |
| Log panel: the subject picker offers the subjects that carry text, and one is logged while it is ticked | PASS |  |
| Log panel: a source turned off hides its rows and keeps their count; Clear empties the list and the counts | PASS |  |
| Log panel: Min, autoscroll, the sources, the subjects picked and whether it is open survive a reload; the filter does not | PASS |  |
| Log panel: with autoscroll off a new message leaves the list where it is; turned on again, the list follows | PASS |  |
| Log panel: saved open, it is in place as the page loads rather than sliding in; its toggle still slides it | PASS |  |
| Log panel: a text subject that keeps saying new things does not push out what the nodes reported | PASS |  |
| Log panel: shut, a screen reader is told of the first warning, and of the first error, not of every message | PASS |  |
| Log panel: the subject picker takes the focus when it opens, so its boxes are ticked by keyboard | PASS |  |
| Log panel: reading back in a full list, what is in view stays put as old messages go and late ones come in above | PASS |  |
| Dashboard works with no internet access | PASS |  |
