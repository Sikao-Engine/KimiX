from enum import StrEnum


class FileOpsWindow:
    """File operations window."""

    pass


class FileActions(StrEnum):
    READ = "read file"
    EDIT = "edit file"
    EDIT_OUTSIDE = "edit file outside working directory"


from .edit import EditFile  # noqa: E402
from .glob import Glob  # noqa: E402
from .grep_local import Grep  # noqa: E402
from .hash_line import HashEdit, HashLine, HashRead  # noqa: E402
from .read import ReadFile  # noqa: E402
from .read_media import ReadMediaFile  # noqa: E402
from .write import WriteFile  # noqa: E402

__all__ = (
    "ReadFile",
    "ReadMediaFile",
    "Glob",
    "Grep",
    "WriteFile",
    "EditFile",
    "HashLine",
    "HashRead",
    "HashEdit",
)

# Migrated from the former ``kimi-cli/src/kimi_cli/tools/file`` package (now merged here):
#
# The unused `Mkdir` / `Rm` demo classes that used to live in that package were
# removed in the built-in tools review (FP-03): they were reachable from no
# agent manifest, had no tests, no native-shim mirror and no documentation, so
# registering them would have granted the model a file-deletion capability for
# no demonstrated need. See reviews/tools/93-orphans-and-removal.md.
#
# This package now also hosts the shell/exec tools migrated from ``kimix``:
# ``bash/`` (Bash, Powershell), ``run.py`` (Run) and ``find_str.py`` (FindStr).
