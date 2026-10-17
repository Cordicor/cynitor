"""Tests for update_check: whether GitHub has a newer release than this one."""

import asyncio
import logging

import pytest

from update_check import UpdateCheck, parse_version

PAGE = "https://github.com/OpenCyphal-Garage/cynitor/releases/tag/v"


def answering(release):
    def fetch():
        if isinstance(release, Exception):
            raise release
        return release
    return fetch


class TestParseVersion:
    @pytest.mark.parametrize("text, version", [
        ("v1.3.0", (1, 3, 0)), ("1.10.0", (1, 10, 0)), ("v2", (2,)),
        ("v1.4.0-rc1", None), ("latest", None), (None, None),
    ])
    def test_tags(self, text, version):
        assert parse_version(text) == version

    def test_numbers_compare_as_numbers(self):
        assert parse_version("v1.10.0") > parse_version("1.9.0")


class TestUpdateCheck:
    @pytest.mark.asyncio
    async def test_a_newer_release_is_offered(self, caplog):
        caplog.set_level(logging.INFO, logger="update_check")
        check = UpdateCheck("1.3.0", answering({"tag_name": "v1.10.0", "html_url": PAGE + "1.10.0"}))
        await check.check()
        await check.check()
        assert check.available == {"version": "1.10.0", "url": PAGE + "1.10.0"}
        assert caplog.text.count("Cynitor 1.10.0 is available (this is 1.3.0)") == 1  # said once, not every day

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tag", ["v1.3.0", "v1.2.9", "v1.4.0-rc1", None])
    async def test_this_release_or_an_older_one_offers_nothing(self, tag):
        check = UpdateCheck("1.3.0", answering({"tag_name": tag, "html_url": PAGE + "x"}))
        await check.check()
        assert check.available is None

    @pytest.mark.asyncio
    async def test_a_link_off_the_project_is_not_offered(self):
        check = UpdateCheck("1.3.0", answering({"tag_name": "v9.0.0", "html_url": "https://example.com/x"}))
        await check.check()
        assert check.available is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", [OSError("Network is unreachable"), ValueError("not JSON"), ["a list"]])
    async def test_no_answer_keeps_what_was_known(self, answer):
        check = UpdateCheck("1.3.0", answering({"tag_name": "v1.4.0", "html_url": PAGE + "1.4.0"}))
        await check.check()
        check._fetch = answering(answer)
        await check.check()
        assert check.available == {"version": "1.4.0", "url": PAGE + "1.4.0"}

    @pytest.mark.asyncio
    async def test_runs_until_stopped(self):
        check = UpdateCheck("1.3.0", answering({"tag_name": "v1.4.0", "html_url": PAGE + "1.4.0"}))
        check.start()
        for _ in range(20):
            if check.available:
                break
            await asyncio.sleep(0.01)
        await check.stop()
        assert check.available is not None and check._task is None
