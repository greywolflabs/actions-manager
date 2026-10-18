"""GitHub must detect the repository's license as Apache-2.0.

GitHub detects licenses with licensee. It scores every root file whose name
looks like a license (lib/licensee/project_files/license_file.rb,
FILENAME_REGEXES), and a matching file whose text is no known license counts
as "other". Two different results make the whole project "other", which the
public repository showed while COMMERCIAL-LICENSE.md and LICENSE_KEY_GUIDE.md
sat next to LICENSE. So LICENSE must be the only root file with such a name.

LICENSE itself must also be the verbatim text: a copy missing two phrases of
sections 1 and 3 was not recognised as Apache-2.0 either.
"""
import hashlib
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# sha256 of https://www.apache.org/licenses/LICENSE-2.0.txt with every run of
# whitespace collapsed to one space, so only a change of wording fails.
APACHE_2_0_NORMALISED_SHA256 = "0ffddef9e48f8a09aed5caf2d44f7ba1c1be2d9b8e0a6f693b1635b2d5566645"

_LICENSE = r"(un)?licen[sc]e"
_COPYING = r"copying"
_PREFERRED_EXT = r"\.(md|markdown|txt|html)\Z"
_OTHER_EXT = r"\.(?!xml|sh|go|gemspec)([^./]|\.\d)+\Z"
_ANY_EXT = r"\.([^./]|\.\d)+\Z"

# The non-zero rows of licensee's FILENAME_REGEXES. The LICENSE-with-extension,
# OFL, COPYRIGHT and PATENTS rows are slightly wider than licensee's, so this
# errs towards flagging a name rather than missing one.
_LICENSE_FILENAMES = [re.compile(p, re.IGNORECASE) for p in (
    rf"\A{_LICENSE}\Z",
    rf"\A{_LICENSE}{_PREFERRED_EXT}",
    rf"\A{_COPYING}\Z",
    rf"\A{_COPYING}{_PREFERRED_EXT}",
    rf"\A{_LICENSE}{_ANY_EXT}",
    rf"\A{_COPYING}{_ANY_EXT}",
    rf"\A{_LICENSE}[-_][^.]*({_OTHER_EXT})?\Z",
    rf"\A{_COPYING}[-_][^.]*({_OTHER_EXT})?\Z",
    rf"\A\w+[-_]{_LICENSE}[^.]*({_OTHER_EXT})?\Z",
    rf"\A\w+[-_]{_COPYING}[^.]*({_OTHER_EXT})?\Z",
    r"\Aofl",
    r"\Acopyright",
    r"\Apatents",
)]


def _committed_root_files(root: Path) -> list:
    """The files directly under *root* that a commit carries.

    In a checkout that is what git tracks, so an ignored local leftover such as
    test_self_hosted_license_types.db never counts. An exported tree has no
    .git and holds only what the export put there.
    """
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=False)
        if top.returncode == 0 and Path(top.stdout.strip()).resolve() == root.resolve():
            tracked = subprocess.run(["git", "-C", str(root), "ls-files"],
                                     capture_output=True, text=True, check=True).stdout
            return [name for name in tracked.splitlines() if "/" not in name]
    except FileNotFoundError:
        pass  # no git binary: fall back to the directory listing
    return [path.name for path in root.iterdir() if path.is_file()]


def license_like_files(root: Path) -> list:
    """The files directly under *root* that licensee would read as licenses."""
    return sorted(
        name for name in _committed_root_files(root)
        if any(rx.search(name) for rx in _LICENSE_FILENAMES)
    )


def test_license_is_the_only_license_like_file_at_the_root():
    found = license_like_files(REPO_ROOT)

    assert found == ["LICENSE"], (
        f"GitHub would read {found} as license files and report the license as 'Other'. "
        "Keep LICENSE the only one: move commercial or license-key documents into internal-docs/."
    )


def test_license_is_the_verbatim_apache_2_0_text():
    text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    digest = hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode("utf-8")).hexdigest()

    assert digest == APACHE_2_0_NORMALISED_SHA256, (
        "LICENSE differs from the Apache License 2.0 text at "
        "https://www.apache.org/licenses/LICENSE-2.0.txt. Restore it verbatim; "
        "put copyright and attribution in NOTICE instead."
    )


def test_the_rule_flags_license_like_names(tmp_path):
    for name in ("LICENSE", "COMMERCIAL-LICENSE.md", "LICENSE_KEY_GUIDE.md", "README.md", "LICENSING_MODEL.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")

    found = license_like_files(tmp_path)

    assert found == ["COMMERCIAL-LICENSE.md", "LICENSE", "LICENSE_KEY_GUIDE.md"]
