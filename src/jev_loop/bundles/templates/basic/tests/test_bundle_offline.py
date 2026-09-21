"""Offline author tests for the __BUNDLE_NAME__ bundle.

These tests never call a model, a network endpoint or a device, and they never start a
managed run.  They establish the two things a static read cannot: that the factory builds a
controller with the managed contract, and that the manifest still declares the contract the
author thinks it does.

Run them from the bundle directory:

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

BUNDLE_DIR = Path(__file__).resolve().parent.parent
MANIFEST_PATH = BUNDLE_DIR / "bundle.json"


def load_controller_module():
    spec = importlib.util.spec_from_file_location("bundle_controller_under_test", BUNDLE_DIR / "bundle_controller.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeServices:
    """Minimal stand-in: enough for the factory, with no run and no cognition traffic."""

    def __init__(self) -> None:
        self.requests: list[dict] = []


class FakeSpec:
    def __init__(self, task: dict) -> None:
        self.task = task
        self.bundle = str(MANIFEST_PATH)
        self.bundle_config: dict = {}


class ManifestContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_manifest_is_the_standard_format(self) -> None:
        self.assertEqual(self.manifest["schema_version"], 2)

    def test_name_matches_the_directory(self) -> None:
        self.assertEqual(self.manifest["name"], BUNDLE_DIR.name)

    def test_contract_is_declared(self) -> None:
        self.assertIsInstance(self.manifest.get("inputs_schema"), dict)
        self.assertIn("type", self.manifest["inputs_schema"])
        self.assertIn("entrypoint", self.manifest)

    def test_scaffold_flag_matches_reality(self) -> None:
        # Flip this expectation only when the controller really is implemented and verified.
        self.assertIs(
            self.manifest.get("scaffold", False),
            True,
            "set scaffold=false in bundle.json only after the controller is implemented and verified",
        )


class FactoryContractTests(unittest.TestCase):
    def test_build_returns_the_managed_controller_shape(self) -> None:
        module = load_controller_module()
        controller = module.build(FakeSpec({"goal": "offline factory check"}), FakeServices(), {})
        for method in ("run", "snapshot", "update", "request_stop"):
            self.assertTrue(callable(getattr(controller, method)), f"missing {method}")
        snapshot = controller.snapshot()
        json.dumps(snapshot)  # the snapshot must stay JSON-serializable
        self.assertIsInstance(snapshot, dict)
        controller.request_stop("offline_factory_check")
        self.assertFalse(snapshot["inputs_held"])


if __name__ == "__main__":
    unittest.main()
