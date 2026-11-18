"""kimix console-script entry point.

Startup-performance design (Python 3.15+): the CLI runner and the SDK-heavy
``kimix.utils`` are bound through PEP 810 module-level ``lazy`` statements, so
``kimix --help`` (which exits inside argument parsing) never imports them.
``constants`` stays eager because it is cheap: it is pure stdlib (``os`` /
``pathlib``) since its help text moved behind a PEP 562 ``__getattr__`` and its
``kimix.ui.printing`` binding became a PEP 810 lazy import. It is also needed by
the ``--clean`` teardown on every real run.
"""

try:
    import uvloop

    uvloop.install()
except ImportError:
    pass

lazy from kimix.utils import delete_session_dir

from . import constants
lazy from .core import _run_cli


def cli() -> None:
    try:
        # Windows-only, process-wide: demote CPython's proactor
        # connection-reset teardown noise (``WinError 10054`` printed as
        # "Exception in callback _ProactorBasePipeTransport._call_connection_lost")
        # to a debug log. Installed before any loop exists so uvicorn's and the
        # session loop are both covered.
        from kimi_cli.utils.asyncio_noise import suppress_proactor_reset_noise

        suppress_proactor_reset_noise()
    except Exception:  # pragma: no cover - noise filtering must never block start-up
        pass
    try:
        _run_cli()
    finally:
        if constants.CLEAN_MODE:
            delete_session_dir()
