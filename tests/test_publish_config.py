"""Regression tests for publish.py's package lists.

The standalone ``kosong`` / ``kaos`` / ``kimi-code`` packages under
``kimi-cli/packages/`` were removed from the workspace; ``kosong`` and ``kaos``
are now vendored inside the ``kimi-cli-x`` wheel. Any leftover entry pointing at
the removed packages would make ``publish.py --bump-version`` crash with
``FileNotFoundError`` or make the build/test-install steps fail, so pin the
lists to on-disk reality here.
"""

import publish


def test_bump_version_package_tomls_exist() -> None:
    assert publish.BUMP_VERSION_PACKAGES, "expected at least one package to bump"
    for name, toml_path in publish.BUMP_VERSION_PACKAGES:
        assert toml_path.is_file(), f"{name}: missing pyproject.toml: {toml_path}"


def test_publish_package_workdirs_exist() -> None:
    assert publish.PUBLISH_PACKAGES, "expected at least one package to publish"
    for name, cwd, _pkg_name in publish.PUBLISH_PACKAGES:
        if cwd is not None:
            workdir = publish.CURRENT_ROOT / cwd
            assert workdir.is_dir(), f"{name}: missing workdir: {workdir}"


def test_no_removed_package_references() -> None:
    combined = " ".join(
        [name for name, _ in publish.BUMP_VERSION_PACKAGES]
        + [f"{name} {cwd or ''} {pkg}" for name, cwd, pkg in publish.PUBLISH_PACKAGES]
    ).lower()
    for removed in ("kosong", "kaos", "kimi-code", "pykaos"):
        assert removed not in combined, f"publish.py still references removed package: {removed}"
