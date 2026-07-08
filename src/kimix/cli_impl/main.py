try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

from . import constants
from .core import _run_cli
from kimix.utils import delete_session_dir


def cli() -> None:
    try:
        _run_cli()
    finally:
        if constants.CLEAN_MODE:
            delete_session_dir()
