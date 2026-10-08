"""Fixtures shared across test modules, and the guard that keeps every test on this machine."""

from __future__ import annotations

import argparse
import ipaddress
import os
import socket
import types
import urllib.request
from typing import NoReturn

import pytest

pytest_plugins = ["pytester"]

# ---------------------------------------------------------------------- no test leaves this machine
#
# On 2026-10-08 the first capture pin in data/ made a test read that capture from Wayback, for
# real, twice: it called `save_blobs` with no stand-in for capture reads, and `save_blobs` turns a
# failed read into a quiet report. Every test now runs behind a `NetworkGuard`, and so does the
# session around the tests: collection, and fixtures wider than one test. A server a test starts on
# loopback still answers; anything else is refused like a dead network, and the test, or the
# session, fails however the code under it handled the refusal.

DEAD_END = ("127.0.0.1", 9)
"""Where the proxy variables send a child process's requests, for the whole session. The session
does not start unless a connection here is refused, and the guard counts a connection here from
this process as an attempt to leave."""

_SET_UP = "REGISTERS_CROSSWALK_TESTS_OFFLINE"
"""Holds the id of the process whose session set itself up. A session nested in that same process
(pytester) skips the set-up; any other value, such as one left over in a shell or inherited by a
child process, names another process and is ignored."""


class NetworkRefused(ConnectionRefusedError):
    """The guard's refusal: a refused connection, which is what the code under test would get with
    no network at all."""


def _is_loopback(host: object) -> bool:
    if host in (None, "", b""):
        return True  # a wildcard: there is nothing to reach
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    name = str(host).strip("[]").split("%", 1)[0]
    if name.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


class NetworkGuard:
    """Loopback, and nothing else, for one test or for the session around the tests.

    It wraps the `socket` functions that resolve a name or open a TCP connection, which is how
    `urllib` and `http.client`, the only network code in this package, leave the machine. For a
    test, `pin._fetch_capture`, the Wayback capture read, is also a tripwire whatever its host.
    Each refusal is recorded before it is raised, so code that swallows the error does not hide it.

    It does not see a socket used without those functions (a UDP `sendto`, or one opened inside a
    C extension), nor anything in a child process, which gets the proxy variables instead (see
    `pytest_sessionstart`).

    `below` is what an allowed call is handed to: by default, the functions in place when the
    guard is installed. The guard's own tests put recorders there, so nothing they do can reach a
    real socket even with the guard broken.
    """

    _RESOLVERS = ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr")

    def __init__(self, below: dict | None = None) -> None:
        self.attempts: list[str] = []
        self.real_fetch_capture = None
        self._below = below

    def install(self, mp: pytest.MonkeyPatch, *, tripwire: bool = True) -> None:
        from registers_crosswalk import pin

        below = self._below or {
            **{name: getattr(socket, name) for name in self._RESOLVERS},
            "create_connection": socket.create_connection,
            "connect": socket.socket.connect,
            "connect_ex": socket.socket.connect_ex,
        }

        def create_connection(address, *args, **kwargs):
            self._check(address, "create_connection")
            return below["create_connection"](address, *args, **kwargs)

        def connect(sock, address):
            self._check(address, "connect")
            return below["connect"](sock, address)

        def connect_ex(sock, address):
            self._check(address, "connect_ex")
            return below["connect_ex"](sock, address)

        def fetch_capture(url, *, timeout=90):
            self._refuse(f"pin._fetch_capture({url}): pass capture_fn, or patch pin._fetch_capture")

        for name in self._RESOLVERS:
            mp.setattr(socket, name, self._resolver(name, below[name]))
        mp.setattr(socket, "create_connection", create_connection)
        mp.setattr(socket.socket, "connect", connect)
        mp.setattr(socket.socket, "connect_ex", connect_ex)
        self.real_fetch_capture = pin._fetch_capture
        if tripwire:
            mp.setattr(pin, "_fetch_capture", fetch_capture)

    def _resolver(self, name, real):
        def resolve(host, *args, **kwargs):
            if not _is_loopback(host):
                self._refuse(f"{name}({host!r})")
            return real(host, *args, **kwargs)

        return resolve

    def _check(self, address, how: str) -> None:
        if not isinstance(address, tuple):
            return  # a socket file's path, which never leaves the machine
        host, port = address[0], address[1]
        if not _is_loopback(host):
            self._refuse(f"{how} to {host}:{port}")
        if port == DEAD_END[1]:
            self._refuse(f"{how} to {host}:{port}, the dead end the proxy variables point at")

    def _refuse(self, what: str) -> NoReturn:
        self.attempts.append(what)
        raise NetworkRefused(f"refused by tests/conftest.py: {what}")

    def raise_if_attempted(self) -> None:
        if self.attempts:
            pytest.fail(
                "this test reached for the network, and tests run offline (tests/conftest.py):\n  "
                + "\n  ".join(self.attempts),
                pytrace=False,
            )

    @staticmethod
    def refuse_a_live_dead_end(address: tuple[str, int]) -> None:
        """Stop the session unless a connection to `address` is refused."""
        where = f"{address[0]}:{address[1]}"
        try:
            socket.create_connection(address, timeout=10).close()
        except ConnectionRefusedError:
            return
        except OSError as exc:
            pytest.exit(
                f"tests/conftest.py: cannot confirm that nothing answers on {where} ({exc!r}), "
                "where child processes' requests are sent",
                returncode=pytest.ExitCode.USAGE_ERROR,
            )
        pytest.exit(
            f"tests/conftest.py: something answers on {where}, where child processes' requests "
            "are sent; choose another DEAD_END",
            returncode=pytest.ExitCode.USAGE_ERROR,
        )


_SESSION_LAYER = pytest.StashKey[tuple[NetworkGuard, pytest.MonkeyPatch]]()


def pytest_sessionstart(session):
    """Set the session up to stay on this machine, once per process.

    A connection to `DEAD_END` must be refused, or the session does not start. A child process
    does not inherit a guard, but it does inherit the proxy variables, so any request it makes goes
    to `DEAD_END` and is refused on this machine. This process connects directly instead: urllib
    reads proxy variables when it builds an opener, and the one installed here has none. So a guard
    sees the real host and names it, and a connection to the dead end from here is still refused
    and recorded. Last, a guard for the whole session goes in under every test's own, for what runs
    outside any test; `pytest_sessionfinish` fails the session if it recorded anything.
    """
    if os.environ.get(_SET_UP) == str(os.getpid()):
        return  # a session nested in this process (pytester) runs on the outer session's set-up
    NetworkGuard.refuse_a_live_dead_end(DEAD_END)
    proxy = f"http://{DEAD_END[0]}:{DEAD_END[1]}"
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        os.environ[name] = proxy
    for name in ("no_proxy", "NO_PROXY"):
        os.environ[name] = "127.0.0.1,localhost,::1"
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    layer, mp = NetworkGuard(), pytest.MonkeyPatch()
    layer.install(mp, tripwire=False)
    session.config.stash[_SESSION_LAYER] = (layer, mp)
    os.environ[_SET_UP] = str(os.getpid())


def pytest_sessionfinish(session, exitstatus):
    """Fail the session if anything outside a test reached for the network."""
    layer, mp = session.config.stash.get(_SESSION_LAYER, (None, None))
    if layer is None:
        return
    mp.undo()
    if layer.attempts and session.exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    layer, _ = config.stash.get(_SESSION_LAYER, (None, None))
    if layer is not None and layer.attempts:
        terminalreporter.section("reached for the network outside any test", red=True)
        for what in layer.attempts:
            terminalreporter.line(f"refused by tests/conftest.py: {what}", red=True)


@pytest.fixture(autouse=True)
def network_guard():
    """Every test runs behind a `NetworkGuard`, installed with a MonkeyPatch of its own, never the
    test's: a test that calls `monkeypatch.undo()` part-way through must not take the guard down."""
    guard = NetworkGuard()
    with pytest.MonkeyPatch.context() as mp:
        guard.install(mp)
        yield guard
    guard.raise_if_attempted()


@pytest.fixture
def real_capture_read(network_guard, monkeypatch):
    """The real `pin._fetch_capture`, for the one test of that function itself, which fakes urlopen
    beneath it. The socket functions stay guarded."""
    from registers_crosswalk import pin

    monkeypatch.setattr(pin, "_fetch_capture", network_guard.real_fetch_capture)


@pytest.fixture
def unverified_fetcher(monkeypatch):
    """A fetcher module that has never been run live, registered for the length of one test.

    Every real fetcher is verified since govinfo's first live run on 2026-09-22, so the tests that
    say what an UNverified one does (it ships False, it stamps False onto what it mints, the console
    lists it as unverified and refuses to resolve through it) need one that is not real. Before
    this, each of them borrowed whichever real fetcher had not been run yet, and had to move every
    time one was.

    Its records carry `fetcher="manual"` because `Source.fetcher` is a Literal of the real names:
    the minted record has to validate, and what is under test is the stamp, not the name. It is
    registered under its own name so it never stands in for `manual` anywhere else.
    """
    from registers_crosswalk import fetchers
    from registers_crosswalk.models import Grade
    from registers_crosswalk.pin import PinSpec, default_fetch, sha256_hex

    module = types.ModuleType("unverified_fake")
    module.NAME = "unverified_fake"
    module.HELP = "a fetcher that has never been run live (test fixture)"
    module.DRIFT_KEY = "sha256"
    module.VERIFIED = False
    module.VERIFIED_AT = None

    def spec(*, url, fetch=default_fetch):
        del fetch  # nothing to look up: the url is the document
        return PinSpec(
            fetcher="manual",
            canonical_url=url,
            citation="X",
            title="X",
            grade=Grade(reliability="A", credibility=1),
            drift_value=sha256_hex,
            fetcher_verified=module.VERIFIED,
            verified_at=module.VERIFIED_AT,
        )

    def add_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--url", required=True)

    def spec_from_args(args, *, fetch=default_fetch, env=None):
        del env
        return spec(url=args.url, fetch=fetch)

    module.spec = spec
    module.add_arguments = add_arguments
    module.spec_from_args = spec_from_args
    module.drift_value = sha256_hex

    monkeypatch.setitem(fetchers._MODULES, module.NAME, module)  # noqa: SLF001 - test registration
    monkeypatch.setattr(fetchers, "NAMES", (*fetchers.NAMES, module.NAME))
    return module
