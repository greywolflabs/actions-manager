"""
QA Test Suite for Docker Deployment Validation

This test suite validates:
1. Docker-compose configurations (self-hosted and cloud)
2. Environment variable handling
3. Deployment health checks
4. Common error scenarios

Acceptance Criteria:
- Both compose files work correctly
- Environment variables are properly loaded
- Health checks pass
"""

import pytest
import os
import re
import sys
import yaml
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Repository root directory
REPO_ROOT = Path(__file__).parent.parent.parent.absolute()

def _self_hosted_heredoc(target):
    """Return the body of a `COPY <<'EOF' <target>` heredoc in Dockerfile.self-hosted."""
    dockerfile = (REPO_ROOT / "Dockerfile.self-hosted").read_text()
    match = re.search(
        rf"COPY <<'EOF' {re.escape(target)}\n(.*?)\nEOF\n", dockerfile, re.DOTALL
    )
    assert match, f"{target} heredoc not found in Dockerfile.self-hosted"
    return match.group(1)


def assert_compose_runs_with_least_privilege(compose_path):
    """Issue #2068: drop every capability the container does not use.

    Only start.sh's root phase needs any: CHOWN and DAC_OVERRIDE to fix
    /app/data ownership, SETUID and SETGID for gosu. Everything after it
    runs as appuser on ports above 1024, and no-new-privileges stops a
    setuid binary from undoing the drop.
    """
    config = yaml.safe_load((REPO_ROOT / compose_path).read_text())

    for name, service in config["services"].items():
        assert "no-new-privileges:true" in service.get("security_opt", []), name
        assert service.get("cap_drop") == ["ALL"], name
        assert sorted(service.get("cap_add", [])) == [
            "CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID",
        ], name


def _shell_commands(script):
    return [
        line.strip()
        for line in script.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class TestDockerComposeConfigurations:
    """Test docker-compose file configurations"""
    
    def test_self_hosted_compose_file_exists(self):
        """Verify self-hosted docker-compose file exists"""
        compose_file = REPO_ROOT / "docker-compose.self-hosted.yml"
        assert compose_file.exists(), "docker-compose.self-hosted.yml not found"
    
    def test_self_hosted_compose_valid_yaml(self):
        """Verify self-hosted compose file is valid YAML"""
        compose_file = REPO_ROOT / "docker-compose.self-hosted.yml"
        with open(compose_file, 'r') as f:
            config = yaml.safe_load(f)
        
        assert config is not None
        assert 'services' in config
    
    def test_self_hosted_has_app_service(self):
        """Verify self-hosted compose has app service.

        The official self-hosted release pulls a pre-built single-container
        image from GHCR (ghcr.io/greywolflabs/actions-manager), so
        the compose file must declare an ``image:`` reference. Contributors
        who want to build from source layer ``docker-compose.self-hosted.dev.yml``
        on top, which adds the ``build:`` directive.
        """
        compose_file = REPO_ROOT / "docker-compose.self-hosted.yml"
        with open(compose_file, 'r') as f:
            config = yaml.safe_load(f)

        assert 'app' in config['services']
        app_config = config['services']['app']
        assert 'image' in app_config, (
            "Self-hosted release flow must pull a pre-built image from GHCR; "
            "set `image:` in docker-compose.self-hosted.yml"
        )
        # The compose file uses env-var indirection so operators can override
        # the registry/tag. Match the default GHCR path with a regex against
        # the dotenv-style template — this is *not* URL sanitization.
        import re as _re
        assert _re.search(
            r"ghcr\.io/greywolflabs/actions-manager:",
            app_config['image'],
        ), (
            "Self-hosted image must default to the official "
            "ghcr.io/greywolflabs/actions-manager GHCR image"
        )
        assert 'ports' in app_config
        assert 'env_file' in app_config

        # The contributor build overlay must still exist for local development.
        dev_overlay = REPO_ROOT / "docker-compose.self-hosted.dev.yml"
        assert dev_overlay.exists(), (
            "Contributors must still be able to build from source via "
            "docker-compose.self-hosted.dev.yml"
        )
        with open(dev_overlay, 'r') as f:
            dev_config = yaml.safe_load(f)
        assert 'build' in dev_config['services']['app'], (
            "Dev overlay must define a `build:` directive so contributors "
            "can build the image from local source"
        )
    
    def test_self_hosted_port_configuration(self):
        """Verify self-hosted exposes correct port"""
        compose_file = REPO_ROOT / "docker-compose.self-hosted.yml"
        with open(compose_file, 'r') as f:
            config = yaml.safe_load(f)
        
        ports = config['services']['app']['ports']
        assert '8080:8080' in ports or any('8080' in p for p in ports)
    
    def test_self_hosted_healthcheck_configured(self):
        """Verify self-hosted has health check configured"""
        compose_file = REPO_ROOT / "docker-compose.self-hosted.yml"
        with open(compose_file, 'r') as f:
            config = yaml.safe_load(f)
        
        assert 'healthcheck' in config['services']['app']
        healthcheck = config['services']['app']['healthcheck']
        assert 'test' in healthcheck
        assert 'interval' in healthcheck
    
    def test_self_hosted_environment_mode_set(self):
        """Verify self-hosted sets INSTALLATION_MODE=self-hosted"""
        compose_file = REPO_ROOT / "docker-compose.self-hosted.yml"
        with open(compose_file, 'r') as f:
            config = yaml.safe_load(f)
        
        env = config['services']['app'].get('environment', [])
        has_self_hosted = any('INSTALLATION_MODE=self-hosted' in str(e) for e in env)
        assert has_self_hosted, "Self-hosted mode not configured"
    
    def test_self_hosted_compose_runs_with_least_privilege(self):
        assert_compose_runs_with_least_privilege("docker-compose.self-hosted.yml")


class TestEnvironmentFiles:
    """Test environment file templates"""
    
    def test_self_hosted_env_example_exists(self):
        """Verify .env.self-hosted.example exists"""
        env_file = REPO_ROOT / ".env.self-hosted.example"
        assert env_file.exists(), ".env.self-hosted.example not found"
    
    def test_self_hosted_env_has_required_variables(self):
        """Verify self-hosted env example has all required variables.

        Admin credentials are NOT required in self-hosted mode since:
        1. OAuth is the primary authentication method
        2. Admin panel is cloud-only (admin.py is removed from self-hosted images)
        3. Self-hosted operators typically use OAuth and don't need local admin users
        
        URL configuration: APP_URL is the primary simplified config.
        VITE_APP_URL is a deprecated alias still accepted by start.sh.
        VITE_BACKEND_URL / VITE_FRONTEND_URL / VITE_WEBSOCKET_URL are the
        explicit per-service overrides. Legacy REACT_APP_* names are still
        accepted by start.sh as deprecated fallbacks.
        """
        env_file = REPO_ROOT / ".env.self-hosted.example"
        with open(env_file, 'r') as f:
            content = f.read()

        required_vars = [
            'INSTALLATION_MODE',
            'GITHUB_CLIENT_ID',
            'GITHUB_CLIENT_SECRET',
            # APP_URL is the primary all-in-one self-hosted config
            'APP_URL',
            # Explicit per-service overrides (documented as advanced options)
            'VITE_BACKEND_URL',
            'VITE_FRONTEND_URL',
        ]

        for var in required_vars:
            assert var in content, f"Required variable {var} not in .env.self-hosted.example"

        # Admin credentials should NOT be in self-hosted template
        # (admin panel is cloud-only)
        assert 'ADMIN_USERNAME' not in content, (
            "ADMIN_USERNAME should not be in self-hosted template - "
            "admin panel is cloud-only"
        )
        assert 'ADMIN_PASSWORD' not in content, (
            "ADMIN_PASSWORD should not be in self-hosted template - "
            "admin panel is cloud-only"
        )
    
    def test_self_hosted_env_has_license_fields(self):
        """Verify self-hosted env example has license key guidance"""
        env_file = REPO_ROOT / ".env.self-hosted.example"
        with open(env_file, 'r') as f:
            content = f.read()
        
        content_lower = content.lower()
        assert 'LICENSE_KEY' in content
        assert 'no license_secret is required' in content_lower or 'no signing secret is required' in content_lower


class TestDockerfile:
    """Test Dockerfile configurations"""
    
    def test_self_hosted_dockerfile_exists(self):
        """Verify self-hosted Dockerfile exists"""
        dockerfile = REPO_ROOT / "Dockerfile.self-hosted"
        assert dockerfile.exists(), "Dockerfile.self-hosted not found"

    def test_self_hosted_image_source_label_names_public_repo(self):
        """GHCR only lists the promoted package on greywolflabs/actions-manager when
        the image's source label names that repo; the image is copied there
        unchanged, so the label has to be baked in at build time."""
        dockerfile = (REPO_ROOT / "Dockerfile.self-hosted").read_text()
        final_stage = dockerfile.rsplit("\nFROM ", 1)[1]

        assert (
            'org.opencontainers.image.source="https://github.com/greywolflabs/actions-manager"'
            in final_stage
        )

    def test_nginx_preserves_upstream_forwarded_proto(self):
        """The container's own nginx must not overwrite the outer proxy's scheme.

        nginx listens on plain HTTP inside the container, so $scheme is always
        "http" there. Deriving X-Forwarded-Proto from it discards the "https"
        an outer proxy (Traefik, Caddy, nginx) sent, and the backend then blocks
        PAT login on a perfectly good HTTPS deployment.
        """
        dockerfile = (REPO_ROOT / "Dockerfile.self-hosted").read_text()

        assert "map $http_x_forwarded_proto $am_forwarded_proto {" in dockerfile, (
            "nginx.conf must map the upstream X-Forwarded-Proto so an outer "
            "proxy's scheme survives"
        )
        assert "~*^https  https;" in dockerfile, (
            "the map must match the first value so a chained proxy's "
            '"https, http" is not treated as plain http'
        )
        assert "proxy_set_header X-Forwarded-Proto $scheme;" not in dockerfile, (
            "X-Forwarded-Proto must come from $am_forwarded_proto, not $scheme"
        )
        assert "proxy_set_header X-Forwarded-Proto $am_forwarded_proto;" in dockerfile, (
            "proxying locations must forward the mapped scheme"
        )

    def test_nginx_proxies_only_the_pr_webhook_to_the_backend(self):
        # #2165: without this block POST /webhooks/github fell through to the
        # SPA catch-all, nginx answered 405, and no delivery reached the backend.
        site = _self_hosted_heredoc("/etc/nginx/sites-available/actions-manager")
        live = "\n".join(line for line in site.splitlines() if not line.lstrip().startswith("#"))
        block = re.search(r"^    location = /webhooks/github \{$(.*?)^    \}$", live, re.DOTALL | re.MULTILINE)

        assert block, "nginx must proxy /webhooks/github to the backend"
        directives = [line.strip() for line in block.group(1).splitlines()]
        assert "proxy_pass http://127.0.0.1:8000;" in directives
        assert "proxy_set_header X-Forwarded-Proto $am_forwarded_proto;" in directives
        # An exact match keeps every other /webhooks/ path unexposed.
        webhook_locations = re.findall(r"^[ \t]*location[ \t]+(?:\S+[ \t]+)?/webhooks\S*", live, re.MULTILINE)
        assert webhook_locations == ["    location = /webhooks/github"]

    def test_self_hosted_start_script_drops_root_before_anything_else(self):
        """Issue #2068: root is only for the /app/data chown.

        Running each command under gosu left nginx's master process as root for
        the life of the container. Re-exec the whole script as appuser instead,
        so nothing started after the chown - nginx included - has root.
        """
        commands = _shell_commands(_self_hosted_heredoc("/app/start.sh"))

        assert commands[:5] == [
            "set -e",
            'if [ "$(id -u)" = "0" ]; then',
            "chown -R appuser:appuser /app/data",
            'exec gosu appuser "$0" "$@"',
            "fi",
        ]
        assert sum("gosu" in command for command in commands) == 1, (
            "after the re-exec everything already runs as appuser"
        )

    def test_self_hosted_start_script_refuses_unusable_non_root_start(self):
        """`user: "1001:1001"` skips the root phase, so start.sh has to say why
        it cannot continue rather than fail later on a read-only path."""
        commands = _shell_commands(_self_hosted_heredoc("/app/start.sh"))

        assert 'if [ "$(id -u)" != "1001" ]; then' in commands
        # Files too, not just the directory: `docker cp` creates a root-owned
        # database inside an appuser-owned /app/data.
        assert (
            'not_owned=$(find /app/data ! -user appuser -print -quit 2>/dev/null || true)'
            in commands
        )
        assert 'if [ ! -w /app/data ] || [ -n "$not_owned" ]; then' in commands

    def test_self_hosted_entrypoint_cannot_be_replaced_by_appuser(self):
        """start.sh runs as root on every start. If appuser could rename
        entries in /app it could swap in its own start.sh and get root on the
        next restart, so /app goes back to root after the recursive chown."""
        dockerfile = (REPO_ROOT / "Dockerfile.self-hosted").read_text()

        assert (
            "RUN mkdir -p /app/data && chown -R appuser:appuser /app && chown root:root /app\n"
            in dockerfile
        )
        assert 'CMD ["/app/start.sh"]' in dockerfile

    def test_self_hosted_nginx_master_does_not_run_as_root(self):
        """Issue #2068: nginx's `user` directive only drops the workers, and
        only when the master starts as root - which it no longer does."""
        nginx_conf = _self_hosted_heredoc("/etc/nginx/nginx.conf")
        commands = _shell_commands(_self_hosted_heredoc("/app/start.sh"))

        assert not re.search(r"^\s*user\s", nginx_conf, re.MULTILINE)
        # Without -e nginx opens its compiled-in /var/log/nginx/error.log
        # before reading nginx.conf, which appuser cannot write.
        assert "nginx -e stderr" in commands

    def test_self_hosted_image_ships_empty_appuser_owned_data_dir(self):
        """Issue #2068: the build-time import check used to create
        /app/data/actions_manager.db as root inside the image, and Docker copies
        a mount point's image contents into every new named volume."""
        dockerfile = (REPO_ROOT / "Dockerfile.self-hosted").read_text()
        import_checks = [
            line for line in dockerfile.splitlines()
            if line.startswith("RUN ") and "import main" in line
        ]

        assert import_checks
        assert all("DATABASE_URL=sqlite:// " in line for line in import_checks)
        # Fails the build if anything imported at build time writes there.
        assert 'test -z "$(ls -A /app/data)"' in dockerfile
        assert "RUN mkdir -p /app/data && chown -R appuser:appuser /app " in dockerfile

    def test_self_hosted_dockerfile_removes_backend_env_files(self):
        """Verify self-hosted Dockerfile removes backend .env files.

        The self-hosted Dockerfile must remove backend/.env* files after copying
        backend sources to prevent development defaults (USE_MOCK_RESPONSES=true,
        DEBUG_MODE=true) from being baked into the production image.

        These defaults would cause startup validation failures in self-hosted
        production mode (ENVIRONMENT=production) even when no .env file is explicitly
        provided to the container.
        """
        dockerfile = REPO_ROOT / "Dockerfile.self-hosted"
        with open(dockerfile, 'r') as f:
            lines = f.readlines()

        # Find the line numbers for key operations
        copy_backend_line = None
        rm_env_backend_line = None

        for i, line in enumerate(lines):
            if 'COPY backend/' in line and copy_backend_line is None:
                copy_backend_line = i
            # Look for rm after we've seen COPY backend/
            if copy_backend_line is not None and rm_env_backend_line is None:
                if 'RUN rm -f .env' in line or ('rm -f .env' in line and i > copy_backend_line):
                    rm_env_backend_line = i

        assert copy_backend_line is not None, (
            "Dockerfile must copy backend sources with 'COPY backend/'"
        )

        assert rm_env_backend_line is not None, (
            "Dockerfile must remove .env files after copying backend sources with "
            "'rm -f .env' to prevent development defaults from being baked into the image"
        )

        assert rm_env_backend_line > copy_backend_line, (
            f"rm -f .env (line {rm_env_backend_line + 1}) must come after "
            f"COPY backend/ (line {copy_backend_line + 1}) to remove tracked .env files"
        )

    def test_backend_code_has_production_safe_defaults(self):
        """Verify backend code defaults to production-safe values.

        Even when no .env file or environment variables are set, the backend
        code must default to production-safe values to prevent self-hosted
        production containers from starting with development configurations.
        """
        import sys
        import importlib.util

        # Load auth.py module
        auth_path = REPO_ROOT / "backend" / "auth.py"
        spec = importlib.util.spec_from_file_location("auth_test", auth_path)
        auth_module = importlib.util.module_from_spec(spec)

        # Read the source to check default values
        with open(auth_path, 'r') as f:
            auth_source = f.read()

        # Check DEBUG_MODE default
        assert 'DEBUG_MODE = os.getenv("DEBUG_MODE", "false")' in auth_source, (
            "auth.py DEBUG_MODE must default to 'false' for production safety. "
            "Found in source but with wrong default value."
        )

        # Check USE_MOCK_RESPONSES default
        assert 'USE_MOCK_RESPONSES = os.getenv("USE_MOCK_RESPONSES", "false")' in auth_source, (
            "auth.py USE_MOCK_RESPONSES must default to 'false' for production safety"
        )


class TestDeploymentDocumentation:
    """Test deployment documentation completeness"""
    
    def test_docker_deployment_modes_doc_exists(self):
        """Verify DOCKER_DEPLOYMENT_MODES.md exists"""
        doc = REPO_ROOT / "DOCKER_DEPLOYMENT_MODES.md"
        assert doc.exists(), "DOCKER_DEPLOYMENT_MODES.md not found"
    
    def test_installation_doc_exists(self):
        """Verify INSTALLATION.md exists"""
        doc = REPO_ROOT / "INSTALLATION.md"
        assert doc.exists(), "INSTALLATION.md not found"
    
    def test_docker_deployment_doc_covers_self_hosted_only(self):
        """The public deployment doc describes the self-hosted container, not the cloud build (#2141)"""
        doc = REPO_ROOT / "DOCKER_DEPLOYMENT_MODES.md"
        with open(doc, 'r') as f:
            content = f.read()
        
        assert 'self-hosted' in content.lower()
        assert 'docker-compose.cloud' not in content
    
    def test_docker_deployment_doc_has_troubleshooting(self):
        """Verify documentation includes troubleshooting"""
        doc = REPO_ROOT / "DOCKER_DEPLOYMENT_MODES.md"
        with open(doc, 'r') as f:
            content = f.read()
        
        assert 'troubleshooting' in content.lower()
    
    def test_installation_doc_has_upgrade_instructions(self):
        """Verify installation doc has upgrade instructions"""
        doc = REPO_ROOT / "INSTALLATION.md"
        with open(doc, 'r') as f:
            content = f.read()
        
        assert 'upgrade' in content.lower() or 'updating' in content.lower()


class TestCommonErrorScenarios:
    """Test that common error scenarios are documented"""
    
    def test_oauth_error_documented(self):
        """Verify OAuth errors are documented"""
        doc = REPO_ROOT / "DOCKER_DEPLOYMENT_MODES.md"
        with open(doc, 'r') as f:
            content = f.read()
        
        assert 'oauth' in content.lower() or 'github' in content.lower()
    
    def test_port_conflict_error_documented(self):
        """Verify port conflict errors are documented"""
        doc = REPO_ROOT / "INSTALLATION.md"
        with open(doc, 'r') as f:
            content = f.read()
        
        assert 'port already in use' in content.lower()


class TestUpgradeDowngradePaths:
    """Test upgrade and downgrade documentation"""
    
    def test_application_upgrade_documented(self):
        """Verify application upgrade process is documented"""
        doc = REPO_ROOT / "INSTALLATION.md"
        with open(doc, 'r') as f:
            content = f.read()
        
        assert 'upgrade' in content.lower()
        assert 'docker' in content.lower()
    
    def test_license_tier_upgrade_documented(self):
        """Verify license tier upgrade is documented"""
        doc = REPO_ROOT / "INSTALLATION.md"
        with open(doc, 'r') as f:
            content = f.read()
        
        assert 'tier' in content.lower() or 'license' in content.lower()
    
    def test_database_migration_documented(self):
        """Verify database migration process is documented"""
        doc = REPO_ROOT / "INSTALLATION.md"
        with open(doc, 'r') as f:
            content = f.read()
        
        assert 'migration' in content.lower() or 'database' in content.lower()
    
    def test_startup_migrations_documented(self):
        """Verify the migrations the container runs at startup are documented"""
        doc = REPO_ROOT / "DOCKER_DEPLOYMENT_MODES.md"
        with open(doc, 'r') as f:
            content = f.read()
        
        assert 'run_migrations.py' in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
