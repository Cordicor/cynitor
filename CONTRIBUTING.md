# Contributing

Changes go to the `dev` branch; `main` takes them by pull request, and a tag on
`main` makes a release once every test suite passes.

Before sending a change, run the tests it touches.

Backend, from `server/`:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/ -v -p pytest_asyncio.plugin
```

Integration, a real Cyphal stack behind the CAN hub on a virtual bus, from the
repository root:

```bash
python3 tests/integration/hub_session.py
python3 tests/integration/hub_session.py --fd
```

Dashboard, end to end in Chromium (needs `pip install playwright` and
`playwright install chromium`), from `tests/e2e/`:

```bash
python3 test_landing_page.py
```

A change to the REST API or the WebSocket messages updates `WEBSOCKET_README.md`
too. Security problems are reported privately: see [SECURITY.md](SECURITY.md).
