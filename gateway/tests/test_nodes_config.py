"""
Tests pour cluster/nodes_config.py — parsing et validation de nodes.yaml.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from cluster.nodes_config import (
    ClusterConfig,
    NodeConfig,
    load_nodes_config,
)


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "nodes.yaml"
    p.write_text(content, encoding="utf-8")
    return p


# ── Cas valides ──────────────────────────────────────────────────────────────

class TestLoadValid:
    def test_minimal_two_nodes(self, tmp_path):
        path = _write(
            tmp_path,
            """
nodes:
  - id: dgx-a
    base_url: https://dgx-a.local:9443
  - id: dgx-b
    base_url: https://dgx-b.local:9443
""",
        )
        cfg = load_nodes_config(path)

        assert isinstance(cfg, ClusterConfig)
        assert len(cfg.nodes) == 2
        assert cfg.nodes[0].id == "dgx-a"
        assert cfg.nodes[0].base_url == "https://dgx-a.local:9443"
        assert cfg.tls_verify is True

    def test_description_optional(self, tmp_path):
        path = _write(
            tmp_path,
            """
nodes:
  - id: a
    base_url: https://x:9443
    description: "Mon premier nœud"
""",
        )
        cfg = load_nodes_config(path)
        assert cfg.nodes[0].description == "Mon premier nœud"

    def test_trailing_slash_stripped(self, tmp_path):
        path = _write(
            tmp_path,
            """
nodes:
  - id: a
    base_url: https://x:9443/
""",
        )
        cfg = load_nodes_config(path)
        assert cfg.nodes[0].base_url == "https://x:9443"

    def test_get_returns_node_by_id(self, tmp_path):
        path = _write(
            tmp_path,
            """
nodes:
  - id: a
    base_url: https://a:9443
  - id: b
    base_url: https://b:9443
""",
        )
        cfg = load_nodes_config(path)
        assert cfg.get("b") == NodeConfig(id="b", base_url="https://b:9443")
        assert cfg.get("inconnu") is None


# ── tls_verify ───────────────────────────────────────────────────────────────

class TestTlsVerify:
    def test_default_true(self, tmp_path):
        path = _write(
            tmp_path,
            """
nodes:
  - id: a
    base_url: https://a:9443
""",
        )
        assert load_nodes_config(path).tls_verify is True

    def test_explicit_false(self, tmp_path):
        path = _write(
            tmp_path,
            """
cluster:
  tls_verify: false
nodes:
  - id: a
    base_url: https://a:9443
""",
        )
        assert load_nodes_config(path).tls_verify is False

    def test_string_false_accepted(self, tmp_path):
        path = _write(
            tmp_path,
            """
cluster:
  tls_verify: "false"
nodes:
  - id: a
    base_url: https://a:9443
""",
        )
        assert load_nodes_config(path).tls_verify is False

    def test_ca_path_accepted_when_exists(self, tmp_path):
        ca = tmp_path / "ca.pem"
        ca.write_text("dummy", encoding="utf-8")
        path = _write(
            tmp_path,
            f"""
cluster:
  tls_verify: {ca}
nodes:
  - id: a
    base_url: https://a:9443
""",
        )
        cfg = load_nodes_config(path)
        assert cfg.tls_verify == str(ca)

    def test_ca_path_missing_raises(self, tmp_path):
        path = _write(
            tmp_path,
            """
cluster:
  tls_verify: /chemin/inexistant/ca.pem
nodes:
  - id: a
    base_url: https://a:9443
""",
        )
        with pytest.raises(ValueError, match="inexistant"):
            load_nodes_config(path)

    def test_invalid_type_raises(self, tmp_path):
        path = _write(
            tmp_path,
            """
cluster:
  tls_verify: 42
nodes:
  - id: a
    base_url: https://a:9443
""",
        )
        with pytest.raises(ValueError, match="tls_verify"):
            load_nodes_config(path)


# ── Validation des nœuds ─────────────────────────────────────────────────────

class TestNodeValidation:
    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="introuvable"):
            load_nodes_config(tmp_path / "absent.yaml")

    def test_no_nodes_key(self, tmp_path):
        path = _write(tmp_path, "cluster: {}\n")
        with pytest.raises(ValueError, match="nodes"):
            load_nodes_config(path)

    def test_empty_nodes_list(self, tmp_path):
        path = _write(tmp_path, "nodes: []\n")
        with pytest.raises(ValueError, match="liste non vide"):
            load_nodes_config(path)

    def test_nodes_not_a_list(self, tmp_path):
        path = _write(tmp_path, "nodes: not-a-list\n")
        with pytest.raises(ValueError, match="liste"):
            load_nodes_config(path)

    def test_duplicate_id_raises(self, tmp_path):
        path = _write(
            tmp_path,
            """
nodes:
  - id: a
    base_url: https://x:9443
  - id: a
    base_url: https://y:9443
""",
        )
        with pytest.raises(ValueError, match="dupliqué"):
            load_nodes_config(path)

    def test_duplicate_url_raises(self, tmp_path):
        path = _write(
            tmp_path,
            """
nodes:
  - id: a
    base_url: https://x:9443
  - id: b
    base_url: https://x:9443
""",
        )
        with pytest.raises(ValueError, match="base_url dupliquée"):
            load_nodes_config(path)

    def test_id_empty_raises(self, tmp_path):
        path = _write(
            tmp_path,
            """
nodes:
  - id: ""
    base_url: https://x:9443
""",
        )
        with pytest.raises(ValueError, match="sans 'id'"):
            load_nodes_config(path)

    def test_id_invalid_chars_raises(self, tmp_path):
        path = _write(
            tmp_path,
            """
nodes:
  - id: "Bad/Node"
    base_url: https://x:9443
""",
        )
        with pytest.raises(ValueError, match="invalide"):
            load_nodes_config(path)

    def test_base_url_missing(self, tmp_path):
        path = _write(
            tmp_path,
            """
nodes:
  - id: a
""",
        )
        with pytest.raises(ValueError, match="base_url manquante"):
            load_nodes_config(path)

    def test_base_url_invalid_scheme(self, tmp_path):
        path = _write(
            tmp_path,
            """
nodes:
  - id: a
    base_url: ftp://x:9443
""",
        )
        with pytest.raises(ValueError, match="http"):
            load_nodes_config(path)

    def test_base_url_with_path_rejected(self, tmp_path):
        path = _write(
            tmp_path,
            """
nodes:
  - id: a
    base_url: https://x:9443/some/path
""",
        )
        with pytest.raises(ValueError, match="chemin"):
            load_nodes_config(path)

    def test_http_logged_warning_but_accepted(self, tmp_path, caplog):
        import logging
        path = _write(
            tmp_path,
            """
nodes:
  - id: a
    base_url: http://x:9443
""",
        )
        with caplog.at_level(logging.WARNING):
            cfg = load_nodes_config(path)
        assert cfg.nodes[0].base_url == "http://x:9443"
        assert any("HTTP en clair" in r.message for r in caplog.records)
