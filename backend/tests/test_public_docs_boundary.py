"""
The Marketplace and admin-panel docs, and the cloud deployment docs, live in
internal-docs/, which the public export drops (#2141). Fail when their content
or their files come back into what the export keeps (#2160). This is the docs
counterpart of test_cloud_boundary.py. The frontend, which the export also
keeps, must not branch on the cloud mode or carry cloud-only UI (#2183).

Runs in the private tree and in the exported one: files the export removes by
name are skipped here and are already gone there, and internal-docs/ exists
only here.
"""

import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPORT_SCRIPT = Path("scripts") / "export_public_tree.sh"
# Public-facing files the export keeps outside docs/, as globs from the repository root.
PUBLIC_FILES = (
    "*.md", "backend/*.md", "frontend/*.md", "install.sh", "Dockerfile.self-hosted",
    "docker-compose.self-hosted*.yml", "backend/.env.example", ".env.self-hosted.example",
)
FRONTEND_SRC = "frontend/src/"
SKIPPED_DIRS = {"node_modules", "dist", ".astro"}

LEAK = re.compile(
    r"marketplace_webhooks|admin panel|/admin/|INSTALLATION_MODE\s*[=:]\s*[\"']?cloud"
    r"|docker-compose\.cloud|ADMIN_PASSWORD",
    re.IGNORECASE,
)

FRONTEND_LEAK = re.compile(
    "|".join((
        r"installation_?mode",
        r"\bis_?cloud",
        r"[!=]=\s*[\"'`]cloud[\"'`]|[\"'`]cloud[\"'`]\s*[!=]=",
        # Not bare "marketplace": Actions projects import GitHub Marketplace
        # action URLs. The cloud billing names pair it with one of these words.
        r"marketplace[\s-]?\w*?(?:plan|purchase|account|billing|subscription|trial|webhook)",
        r"marketplace_\w|webhooks?/marketplace|admin_?override",
        # Not "admin": isAdmin and isWorkspaceAdmin are the workspace role. Not
        # every "/admin" either: docs.github.com/en/admin/... is GitHub's own docs.
        r"[\"'`]/admin\b|/api/admin\b|admin[\s_-]?panel",
    )),
    re.IGNORECASE,
)

# Lines that may match LEAK (FRONTEND_LEAK under frontend/src/), as
# {(path, stripped line): reason}. Keep a reason on every entry so the next
# reader can tell whether it still applies.
ALLOWED = {}


def _removed_by_name(root):
    # The exported tree has no scripts/, and none of these files either. One path
    # per column-0 rm line; any other shape isn't matched, so its file is scanned.
    script = root / EXPORT_SCRIPT
    if not script.exists():
        return set()
    return set(re.findall(r"^rm -r?f (\S+)\s*$", script.read_text(encoding="utf-8"), re.MULTILINE))


def _tree(top):
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [d for d in dirnames if d not in SKIPPED_DIRS]
        for name in filenames:
            yield Path(dirpath) / name


def _text(path):
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def public_docs(root=REPO_ROOT):
    removed = _removed_by_name(root)
    files = [path for pattern in PUBLIC_FILES for path in sorted(root.glob(pattern))]
    # Every file under docs/, not only markdown: .astro components,
    # astro.config.mjs (the sidebar) and other page formats reach the site too.
    files += sorted(_tree(root / "docs"))
    return [path for path in files if path.relative_to(root).as_posix() not in removed]


def frontend_sources(root=REPO_ROOT):
    return sorted(path for path in _tree(root / FRONTEND_SRC) if path.suffix in {".ts", ".tsx"})


def _pattern_for(rel):
    return FRONTEND_LEAK if rel.startswith(FRONTEND_SRC) else LEAK


def _leaks(root, files, allowed):
    leaks = []
    for path in files:
        rel = path.relative_to(root).as_posix()
        pattern = _pattern_for(rel)
        for number, line in enumerate((_text(path) or "").splitlines(), 1):
            text = line.strip()
            if pattern.search(text) and (rel, text) not in allowed:
                leaks.append(f"{rel}:{number}: {text}")
    return leaks


def find_leaks(root=REPO_ROOT, allowed=ALLOWED):
    return _leaks(root, public_docs(root), allowed)


def find_frontend_leaks(root=REPO_ROOT, allowed=ALLOWED):
    return _leaks(root, frontend_sources(root), allowed)


def reappeared_internal_docs(root=REPO_ROOT):
    internal = root / "internal-docs"
    found = []
    for path in sorted(internal.rglob("*.md")):
        rel = path.relative_to(internal)
        # internal-docs/README.md is that folder's own index, not a moved doc.
        if rel == Path("README.md"):
            continue
        found += [
            candidate.relative_to(root).as_posix()
            for candidate in (root / rel, root / "docs" / rel)
            if candidate.exists()
        ]
    return found


def test_public_docs_have_no_cloud_or_admin_panel_content():
    leaks = find_leaks()
    assert not leaks, (
        "Public docs describe the cloud build, Marketplace or admin panel. Move the "
        "text to internal-docs/, or add the line to ALLOWED with the reason it is "
        "public:\n" + "\n".join(leaks)
    )


def test_frontend_has_no_cloud_mode_branches_or_cloud_only_ui():
    leaks = find_frontend_leaks()
    assert not leaks, (
        "The frontend is public and must behave the same in every installation "
        "mode. Keep cloud-only UI out of it, or add the line to ALLOWED with the "
        "reason it is public:\n" + "\n".join(leaks)
    )


def test_scan_covers_the_public_docs():
    # An empty scan would pass the guard, so check it reaches what it should.
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in public_docs()}
    assert {"README.md", "docs/QUICK_START.md", ".env.self-hosted.example"} <= scanned
    assert {"backend/MIGRATIONS.md", "install.sh", "Dockerfile.self-hosted"} <= scanned
    assert "docker-compose.self-hosted.yml" in scanned
    assert any(name.startswith("frontend/") for name in scanned)
    assert any(name.endswith(".mdx") for name in scanned)
    assert "CLAUDE.md" not in scanned


def test_scan_covers_the_frontend_sources():
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in frontend_sources()}
    assert "frontend/src/App.tsx" in scanned
    assert any(name.endswith(".test.ts") for name in scanned)


def test_allowed_entries_still_match():
    stale = [
        f"{path}: {text}"
        for (path, text) in ALLOWED
        if not _pattern_for(path).search(text)
        or not (REPO_ROOT / path).exists()
        or text not in (_text(REPO_ROOT / path) or "")
    ]
    assert not stale, "Remove these ALLOWED entries; nothing matches them now:\n" + "\n".join(stale)


def test_moved_internal_docs_do_not_reappear():
    found = reappeared_internal_docs()
    assert not found, (
        "These files also exist in internal-docs/, which the export drops. Keep "
        "only the internal-docs/ copy:\n" + "\n".join(found)
    )


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_guard_catches_a_planted_line_and_skips_allowed_ones(tmp_path):
    _write(tmp_path, "README.md", "Self-hosted install.\n")
    _write(tmp_path, "docs/guide.md", "Intro\nOpen the Admin Panel to change tiers.\n")
    _write(tmp_path, "docs/kept.md", "Unset ADMIN_PASSWORD is fine.\n")
    _write(tmp_path, "docs/src/components/Hero.astro", "<p>INSTALLATION_MODE: cloud</p>\n")
    _write(tmp_path, "docs/node_modules/pkg/README.md", "admin panel\n")
    (tmp_path / "docs" / "logo.png").write_bytes(b"\x89PNG\0admin panel")
    _write(tmp_path, "CLAUDE.md", "Mentions the admin panel.\n")
    _write(tmp_path, EXPORT_SCRIPT, "#!/bin/sh\nrm -f CLAUDE.md\n")
    allowed = {("docs/kept.md", "Unset ADMIN_PASSWORD is fine."): "test"}

    assert find_leaks(tmp_path, allowed) == [
        "docs/guide.md:2: Open the Admin Panel to change tiers.",
        "docs/src/components/Hero.astro:1: <p>INSTALLATION_MODE: cloud</p>",
    ]


def test_guard_scans_the_files_the_export_keeps_outside_docs(tmp_path):
    _write(tmp_path, "backend/MIGRATIONS.md", "See marketplace_webhooks.\n")
    _write(tmp_path, "frontend/GUIDE.md", "Build the admin panel.\n")
    _write(tmp_path, "install.sh", "#!/bin/sh\nINSTALLATION_MODE=cloud\n")
    _write(tmp_path, "Dockerfile.self-hosted", "ENV INSTALLATION_MODE=self-hosted\n")
    _write(tmp_path, "docker-compose.self-hosted.dev.yml", "ADMIN_PASSWORD: x\n")
    _write(tmp_path, "backend/NOTES.md", "admin panel\n")
    _write(tmp_path, "backend/cloud/README.md", "admin panel\n")
    _write(tmp_path, EXPORT_SCRIPT, "#!/bin/sh\nrm -f backend/NOTES.md\n")

    assert find_leaks(tmp_path, {}) == [
        "backend/MIGRATIONS.md:1: See marketplace_webhooks.",
        "frontend/GUIDE.md:1: Build the admin panel.",
        "install.sh:2: INSTALLATION_MODE=cloud",
        "docker-compose.self-hosted.dev.yml:1: ADMIN_PASSWORD: x",
    ]


def test_frontend_guard_catches_cloud_branches_and_skips_workspace_admin(tmp_path):
    _write(tmp_path, "frontend/src/App.tsx", "\n".join((
        "if (config.installationMode === 'self-hosted') {",
        "const isCloud = health.is_cloud;",
        "if (mode !== \"cloud\") return null;",
        "const hosted = `cloud` == import.meta.env.VITE_MODE;",
        "navigate('/admin');",
        "<AdminPanel />",
        "<span>{account.marketplace_next_billing_date}</span>",
        "<p>Manage your Marketplace plan</p>",
        "await api.get('/api/webhooks/marketplace/events');",
        "{user.admin_override_until && <Badge />}",
        "if (isAdmin || isWorkspaceAdmin) {",
        "<a href=\"https://docs.github.com/en/admin/policies\">",
        "const analysisCloud = results.cloud;",
        "Paste a repo URL, a GitHub Marketplace action URL",
        "// Renders an action's marketplace-style branding icon",
        "security: ['sonarcloud', 'codeql'],",
        "// so a cloud install never flashes the PAT form",
        "const role = 'admin';",
    )))
    _write(tmp_path, "frontend/src/api/client.test.ts", "expect(isCloud).toBe(false);\n")
    _write(tmp_path, "frontend/src/styles/admin.css", "/* admin panel */\n")
    _write(tmp_path, "frontend/src/node_modules/pkg/index.ts", "isCloud\n")
    _write(tmp_path, "frontend/README.md", "isCloud\n")
    allowed = {("frontend/src/api/client.test.ts", "expect(isCloud).toBe(false);"): "test"}

    assert find_frontend_leaks(tmp_path, allowed) == [
        "frontend/src/App.tsx:1: if (config.installationMode === 'self-hosted') {",
        "frontend/src/App.tsx:2: const isCloud = health.is_cloud;",
        "frontend/src/App.tsx:3: if (mode !== \"cloud\") return null;",
        "frontend/src/App.tsx:4: const hosted = `cloud` == import.meta.env.VITE_MODE;",
        "frontend/src/App.tsx:5: navigate('/admin');",
        "frontend/src/App.tsx:6: <AdminPanel />",
        "frontend/src/App.tsx:7: <span>{account.marketplace_next_billing_date}</span>",
        "frontend/src/App.tsx:8: <p>Manage your Marketplace plan</p>",
        "frontend/src/App.tsx:9: await api.get('/api/webhooks/marketplace/events');",
        "frontend/src/App.tsx:10: {user.admin_override_until && <Badge />}",
    ]


def test_guard_catches_a_moved_doc_copied_back(tmp_path):
    _write(tmp_path, "internal-docs/README.md", "Index\n")
    _write(tmp_path, "internal-docs/MARKETPLACE_WEBHOOKS.md", "Moved\n")
    _write(tmp_path, "internal-docs/features/ADMIN_FEATURE.md", "Moved\n")
    _write(tmp_path, "README.md", "Public\n")
    _write(tmp_path, "MARKETPLACE_WEBHOOKS.md", "Copied back\n")
    _write(tmp_path, "docs/features/ADMIN_FEATURE.md", "Copied back\n")

    assert reappeared_internal_docs(tmp_path) == [
        "MARKETPLACE_WEBHOOKS.md",
        "docs/features/ADMIN_FEATURE.md",
    ]
