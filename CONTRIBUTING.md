# Contributing

Changes go to the `dev` branch; `main` takes them by pull request, and a tag on
`main` makes a release once every test suite passes.

Before sending a change, run the tests it touches.

Backend, from `server/`:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/ -v -p pytest_asyncio.plugin
```

Add `-p pytest_cov --cov=. --cov-report=term` to see which lines the tests
reach; CI puts the same table on the Python 3.12 Linux job's summary.

Lint and types, as CI checks them (`pip install ruff==0.6.9 mypy==1.13.0`):

```bash
ruff check server tools tests          # from the repository root
(cd server && mypy .)                  # settings in server/mypy.ini
```

Integration, a real Cyphal stack behind the CAN hub on a virtual bus, and a running
server's API walked over HTTP, from the repository root:

```bash
python3 tests/integration/hub_session.py
python3 tests/integration/hub_session.py --fd
python3 tests/integration/api_walk.py
```

Dashboard, end to end in Chromium (needs `pip install playwright` and
`playwright install chromium`), from `tests/e2e/`:

```bash
python3 test_landing_page.py
```

A change to the REST API or the WebSocket messages updates `WEBSOCKET_README.md`
too. Security problems are reported privately: see [SECURITY.md](SECURITY.md).
