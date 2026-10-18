#!/usr/bin/env python3
"""Browser end-to-end tests of the Cynitor dashboard.

Prerequisites:
    pip install -r requirements.txt
    playwright install chromium

Usage:
    python3 test_landing_page.py                 # every suite, in order
    python3 test_landing_page.py sidebar dsdl    # those suites only, after the page has loaded

The suites are suite_<name>.py, one per view: landing, graph, tables, compare,
dsdl, record, debug, testnodes, sidebar, log_panel. They share one page and
run in this order; a suite run alone starts from the freshly loaded page.

The script serves website/ itself on port 5500 unless something already
answers there (your own dev server, for instance), and shuts its server down
afterwards. Set CYNITOR_E2E_URL to test a frontend hosted elsewhere; the
script then never starts a server. Results go to result.md beside it.
"""

import sys

# Each suite registers its tests as it is imported: this is their order.
import suite_landing  # noqa: F401
import suite_graph  # noqa: F401
import suite_tables  # noqa: F401
import suite_compare  # noqa: F401
import suite_dsdl  # noqa: F401
import suite_record  # noqa: F401
import suite_debug  # noqa: F401
import suite_testnodes  # noqa: F401
import suite_sidebar  # noqa: F401
import suite_log_panel  # noqa: F401
from harness import main

if __name__ == "__main__":
    main(sys.argv[1:])
