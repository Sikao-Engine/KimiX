"""kimix console-script entry point.

Startup-performance design (Python 3.15+): the CLI runner and the SDK-heavy
``kimix.utils`` are bound through PEP 810 module-level ``lazy`` statements, so
``kimix --help`` (which exits inside argument parsing) never imports them.
``constants`` stays eager because it is cheap (terminal printing only) and is
needed by the ``--clean`` teardown on every real run.
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
        _run_cli()
    finally:
        if constants.CLEAN_MODE:
            delete_session_dir()
