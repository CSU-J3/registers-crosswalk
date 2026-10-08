"""The guard in conftest.py: no test leaves this machine, and one that tries fails.

Nothing here can reach a real socket, even with the guard broken. A fresh guard hands what it
allows to recorders; the nested session's requests run above recorders the outer test installs;
the child process records the connection urllib asks for instead of opening it; and everything
else, the separate pytest sessions included, uses a numeric host, which resolves on this machine,
or loopback.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from registers_crosswalk import pin as pinmod

CONFTEST = Path(__file__).with_name("conftest.py")
TEST_NET = "192.0.2.1"  # RFC 5737 TEST-NET-1: for documentation, routed nowhere
WRAPPED = (
    "getaddrinfo",
    "gethostbyname",
    "gethostbyname_ex",
    "gethostbyaddr",
    "create_connection",
    "connect",
    "connect_ex",
)


def _recorders(handed):
    """Stand-ins for what lies below the guard: each records that a call reached it, and fails."""

    def recorder(name):
        def record(*args, **kwargs):
            handed.append(name)
            raise AssertionError(f"{name} was handed below the guard")

        return record

    return {name: recorder(name) for name in WRAPPED}


def test_every_test_runs_behind_the_guard(network_guard):
    # A numeric host: with no guard in place this resolves on this machine and sends nothing.
    with pytest.raises(ConnectionRefusedError, match="refused by tests/conftest.py"):
        socket.getaddrinfo(TEST_NET, 80)
    assert network_guard.attempts == [f"getaddrinfo({TEST_NET!r})"]
    network_guard.attempts.clear()  # asked on purpose


def test_a_tests_own_monkeypatch_undo_leaves_the_guard_up(monkeypatch, network_guard):
    # The manifest tests call monkeypatch.undo() part-way through; the guard is not in that stack.
    monkeypatch.setenv("REGISTERS_CROSSWALK_UNDO_PROBE", "1")
    monkeypatch.undo()
    with pytest.raises(ConnectionRefusedError, match="refused by tests/conftest.py"):
        socket.getaddrinfo(TEST_NET, 80)
    # This test's guard refused it, not the session's beneath, which would also refuse.
    assert network_guard.attempts == [f"getaddrinfo({TEST_NET!r})"]
    network_guard.attempts.clear()  # asked on purpose


def test_a_guard_refuses_all_but_loopback_and_hands_loopback_down(network_guard):
    handed = []
    guard = type(network_guard)(below=_recorders(handed))
    with pytest.MonkeyPatch.context() as mp:
        guard.install(mp)
        refused = [
            lambda: socket.getaddrinfo(TEST_NET, 80),
            lambda: socket.getaddrinfo("web.archive.org", 443),
            lambda: socket.gethostbyname("web.archive.org"),
            lambda: socket.create_connection(("web.archive.org", 443)),
            lambda: socket.create_connection(("127.0.0.1", 9)),
        ]
        for call in refused:
            with pytest.raises(ConnectionRefusedError, match="refused by tests/conftest.py"):
                call()
        with socket.socket() as sock, pytest.raises(ConnectionRefusedError):
            sock.connect((TEST_NET, 80))
        assert handed == []
        with pytest.raises(AssertionError, match="handed below the guard"):
            socket.create_connection(("127.0.0.1", 8080))
    assert handed == ["create_connection"]
    assert guard.attempts == [
        f"getaddrinfo({TEST_NET!r})",
        "getaddrinfo('web.archive.org')",
        "gethostbyname('web.archive.org')",
        "create_connection to web.archive.org:443",
        "create_connection to 127.0.0.1:9, the dead end the proxy variables point at",
        f"connect to {TEST_NET}:80",
    ]
    with pytest.raises(pytest.fail.Exception, match="reached for the network"):
        guard.raise_if_attempted()
    assert network_guard.attempts == []


def test_a_capture_read_hits_the_tripwire(network_guard):
    url = "https://web.archive.org/web/20260707001827id_/https://www.dscc.org/"
    with pytest.raises(ConnectionRefusedError, match=r"pin\._fetch_capture"):
        pinmod._fetch_capture(url)  # noqa: SLF001
    assert network_guard.attempts == [
        f"pin._fetch_capture({url}): pass capture_fn, or patch pin._fetch_capture"
    ]
    network_guard.attempts.clear()  # asked on purpose


def test_the_session_stops_if_something_answers_at_the_dead_end(network_guard):
    with socket.create_server(("127.0.0.1", 0)) as server:
        where = ("127.0.0.1", server.getsockname()[1])
        with pytest.raises(pytest.exit.Exception, match="something answers on"):
            type(network_guard).refuse_a_live_dead_end(where)
    assert network_guard.attempts == []  # loopback stays open through the guard


def test_a_child_process_sends_its_requests_to_the_dead_end():
    # The child records the connection urllib asks for instead of opening it, so this sends
    # nothing whether or not the proxy variables are in place.
    script = (
        "import socket, urllib.request\n"
        "def record(address, *args, **kwargs):\n"
        "    print(*address)\n"
        "    raise ConnectionRefusedError('recorded, not made')\n"
        "def refuse(sock, address):\n"
        "    record(address)\n"
        "socket.create_connection = record\n"
        "socket.socket.connect = refuse\n"
        "try:\n"
        f"    urllib.request.urlopen('http://{TEST_NET}/', timeout=5)\n"
        "except OSError:\n"
        "    pass\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=True
    )
    assert done.stdout.split() == ["127.0.0.1", "9"]


STRAY = f"""
import urllib.request


def test_the_installed_opener_goes_direct():
    assert urllib.request.getproxies()["http"] == "http://127.0.0.1:9"  # the session's variables
    try:
        urllib.request.urlopen("http://{TEST_NET}/", timeout=5)
    except OSError:
        pass  # swallowed, as save_blobs swallows a failed capture read


def test_an_opener_built_from_the_environment_hits_the_dead_end():
    try:
        urllib.request.build_opener().open("http://{TEST_NET}/", timeout=5)
    except OSError:
        pass
"""


def test_a_swallowed_stray_request_fails_its_test_at_teardown(pytester, monkeypatch, network_guard):
    """An in-process urlopen to a non-loopback URL, with the session's proxy variables in place,
    is recorded under its real host and fails its test at teardown, though the test swallowed the
    error; one sent through an opener built from those variables is recorded at the dead end.

    The nested session runs this conftest, with recorders beneath its guard: a broken guard fails
    this test and sends nothing.
    """
    handed = []
    for name, record in _recorders(handed).items():
        monkeypatch.setattr(socket.socket if name.startswith("connect") else socket, name, record)
    pytester.makeconftest(CONFTEST.read_text(encoding="utf-8"))
    pytester.makepyfile(test_stray=STRAY)
    result = pytester.runpytest_inprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=2, errors=2)
    result.stdout.fnmatch_lines(
        [
            f"*create_connection to {TEST_NET}:80*",
            "*create_connection to 127.0.0.1:9, the dead end the proxy variables point at*",
        ]
    )
    assert handed == []
    assert network_guard.attempts == []


OUTSIDE = f"""
import socket

import pytest

try:
    socket.getaddrinfo("{TEST_NET}", 80)  # at import, while the session collects
except OSError:
    pass


@pytest.fixture(scope="module")
def wide():
    try:
        socket.getaddrinfo("192.0.2.2", 80)  # set up before any test's own guard
    except OSError:
        pass


def test_uses_a_module_fixture(wide):
    pass
"""


def test_an_attempt_outside_any_test_fails_the_session(pytester):
    """Collection, and fixtures wider than one test, run outside every test's guard and under the
    session's, where an attempt fails the session though every test passed. In a separate pytest
    process, so the session is its own; the hosts are numeric, so nothing is sent either way."""
    pytester.makeconftest(CONFTEST.read_text(encoding="utf-8"))
    pytester.makepyfile(test_outside=OUTSIDE)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        [
            "*reached for the network outside any test*",
            f"*getaddrinfo('{TEST_NET}')*",
            "*getaddrinfo('192.0.2.2')*",
        ]
    )


OWN_SET_UP = """
import os


def test_this_session_set_itself_up():
    assert os.environ["REGISTERS_CROSSWALK_TESTS_OFFLINE"] == str(os.getpid())
"""


@pytest.mark.parametrize("left_over", ["1", None], ids=["left-in-a-shell", "inherited"])
def test_a_stale_set_up_marker_does_not_skip_the_set_up(pytester, monkeypatch, left_over):
    """The marker names the process whose session set itself up. One left over in a shell, or
    inherited from this process, names another process, so a new session sets itself up anyway."""
    monkeypatch.setenv("REGISTERS_CROSSWALK_TESTS_OFFLINE", left_over or str(os.getpid()))
    pytester.makeconftest(CONFTEST.read_text(encoding="utf-8"))
    pytester.makepyfile(test_set_up=OWN_SET_UP)
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
