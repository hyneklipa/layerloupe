"""docker-compose.yml + scripts/seed.sh static checks.

End-to-end compose behavior is exercised manually (and in the e2e tests
against a live registry). Here we verify the file is YAML-valid and stays
in sync with the deployment guide's promises:

* Three services: registry, layerloupe, seed.
* Registry has delete enabled and a healthcheck.
* LayerLoupe's environment matches the documented defaults.
* Seed depends on a healthy registry and uses crane in insecure mode so
  the plain-HTTP registry actually accepts the push.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parent.parent
COMPOSE = ROOT / "docker-compose.yml"
SEED_SH = ROOT / "scripts" / "seed.sh"


@pytest.fixture(scope="module")
def compose_data() -> dict[str, Any]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


# -- File presence + YAML shape ------------------------------------------


def test_compose_file_exists() -> None:
    assert COMPOSE.exists()


def test_seed_script_exists_and_is_executable() -> None:
    assert SEED_SH.exists()
    import os

    assert os.access(SEED_SH, os.X_OK), "scripts/seed.sh should be chmod +x"


def test_compose_declares_three_services(compose_data: dict[str, Any]) -> None:
    assert set(compose_data["services"].keys()) == {"registry", "layerloupe", "seed"}


# -- Registry service ----------------------------------------------------


def test_registry_uses_delete_enabled(compose_data: dict[str, Any]) -> None:
    """Without this setting LayerLoupe's delete flow can't actually delete."""
    env = compose_data["services"]["registry"]["environment"]
    assert env["REGISTRY_STORAGE_DELETE_ENABLED"] == "true"


def test_registry_has_healthcheck(compose_data: dict[str, Any]) -> None:
    """The other services use ``service_healthy`` to wait on this one."""
    hc = compose_data["services"]["registry"]["healthcheck"]
    assert "test" in hc
    test_cmd = hc["test"]
    joined = " ".join(test_cmd) if isinstance(test_cmd, list) else str(test_cmd)
    assert "/v2/" in joined  # actually probes the registry API


def test_registry_publishes_5000(compose_data: dict[str, Any]) -> None:
    ports = compose_data["services"]["registry"]["ports"]
    assert any("5000:5000" in p for p in ports)


def test_registry_persists_data_via_volume(compose_data: dict[str, Any]) -> None:
    volumes = compose_data["services"]["registry"]["volumes"]
    assert any("/var/lib/registry" in v for v in volumes)
    assert "registry-data" in compose_data.get("volumes", {})


# -- LayerLoupe service -----------------------------------------------------


def test_layerloupe_builds_from_dockerfile(compose_data: dict[str, Any]) -> None:
    build = compose_data["services"]["layerloupe"]["build"]
    # Either ``build: .`` shorthand or expanded form — handle both.
    if isinstance(build, dict):
        assert build.get("context") == "."
    else:
        assert build == "."


def test_layerloupe_depends_on_healthy_registry(compose_data: dict[str, Any]) -> None:
    deps = compose_data["services"]["layerloupe"]["depends_on"]
    assert deps["registry"]["condition"] == "service_healthy"


def test_layerloupe_env_points_at_registry(compose_data: dict[str, Any]) -> None:
    env = compose_data["services"]["layerloupe"]["environment"]
    # Internal URL uses the compose hostname; public URL is the operator-visible one.
    assert env["REGISTRY_URL"] == "http://registry:5000"
    assert "localhost:5000" in env["REGISTRY_PUBLIC_URL"]


def test_layerloupe_enables_delete_for_dev_demo(compose_data: dict[str, Any]) -> None:
    env = compose_data["services"]["layerloupe"]["environment"]
    assert env["ALLOW_DELETE"] == "true"


def test_layerloupe_publishes_8080(compose_data: dict[str, Any]) -> None:
    ports = compose_data["services"]["layerloupe"]["ports"]
    assert any("8080:8080" in p for p in ports)


def test_layerloupe_session_secret_is_present_with_dev_warning(
    compose_data: dict[str, Any],
) -> None:
    """A fixed dev secret keeps sessions stable across container restarts;
    the value name signals "don't ship this to production"."""
    env = compose_data["services"]["layerloupe"]["environment"]
    secret = env["SESSION_SECRET"]
    assert "dev" in secret.lower()


# -- Seed service --------------------------------------------------------


def test_seed_uses_crane_image(compose_data: dict[str, Any]) -> None:
    """``crane:debug`` ships a busybox shell so the inline entrypoint works."""
    image = compose_data["services"]["seed"]["image"]
    assert "go-containerregistry/crane" in image
    assert ":debug" in image


def test_seed_runs_one_shot(compose_data: dict[str, Any]) -> None:
    """``restart: "no"`` so it runs once and disappears, not in a loop."""
    assert compose_data["services"]["seed"]["restart"] == "no"


def test_seed_depends_on_healthy_registry(compose_data: dict[str, Any]) -> None:
    deps = compose_data["services"]["seed"]["depends_on"]
    assert deps["registry"]["condition"] == "service_healthy"


def test_seed_uses_insecure_flag_for_plain_http(
    compose_data: dict[str, Any],
) -> None:
    """Without ``--insecure`` crane defaults to HTTPS and refuses our HTTP registry."""
    entrypoint = compose_data["services"]["seed"]["entrypoint"]
    inline = " ".join(entrypoint) if isinstance(entrypoint, list) else str(entrypoint)
    assert "--insecure" in inline


def test_seed_mirrors_at_least_three_distinct_images(
    compose_data: dict[str, Any],
) -> None:
    """The UI demo isn't very interesting with only one repo."""
    entrypoint = compose_data["services"]["seed"]["entrypoint"]
    inline = " ".join(entrypoint) if isinstance(entrypoint, list) else str(entrypoint)
    # Scan for known seed images.
    seeded = sum(1 for img in ("alpine", "busybox", "hello-world", "nginx") if img in inline)
    assert seeded >= 3


# -- seed.sh fallback (used when running outside compose) ----------------


def test_seed_sh_is_busybox_shebang() -> None:
    text = SEED_SH.read_text(encoding="utf-8")
    # The :debug crane image ships busybox at /busybox/sh.
    assert text.startswith("#!/busybox/sh"), "seed.sh must shebang the crane shell"


def test_seed_sh_uses_insecure_flag() -> None:
    text = SEED_SH.read_text(encoding="utf-8")
    assert "--insecure" in text
