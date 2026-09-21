"""Offline author tests for the offline-switchboard reference bundle.

They run the kernel directly in process: no managed host, no network, no model call, no
device.  They are the tests ``jev-loop bundle conformance --run-tests`` executes.

    cd .agents/jev-bundle/offline-switchboard
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

BUNDLE_DIR = Path(__file__).resolve().parent.parent
MANIFEST_PATH = BUNDLE_DIR / "bundle.json"


def load_controller_module():
    spec = importlib.util.spec_from_file_location("offline_switchboard_controller", BUNDLE_DIR / "bundle_controller.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeServices:
    """The controller only needs the change flag for a synchronous stop check."""

    def __init__(self) -> None:
        import threading

        self.changed = threading.Event()


class FakeSpec:
    def __init__(self, task: dict) -> None:
        self.task = task
        self.bundle = str(MANIFEST_PATH)
        self.bundle_config: dict = {}


def manifest_config() -> dict:
    """The real defaults from bundle.json, exactly as the host passes them to the factory."""
    return dict(json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))["config"])


class ManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_is_a_finished_standard_bundle(self) -> None:
        self.assertEqual(self.manifest["schema_version"], 2)
        self.assertEqual(self.manifest["name"], BUNDLE_DIR.name)
        self.assertIs(self.manifest["scaffold"], False, "the reference must not ship as a scaffold")

    def test_declares_an_offline_runtime(self) -> None:
        self.assertIs(self.manifest["runtime"]["requires_network"], False)
        self.assertEqual(self.manifest["dependencies"]["credential_env"], [])

    def test_contract_matches_the_controller(self) -> None:
        self.assertEqual(self.manifest["entrypoint"], "bundle_controller:build")
        self.assertEqual(self.manifest["python_path"], ".")


class KernelTests(unittest.TestCase):
    def test_factory_builds_a_controller_with_the_managed_shape(self) -> None:
        module = load_controller_module()
        controller = module.build(FakeSpec({"goal": "offline factory check"}), FakeServices(), manifest_config())
        for method in ("run", "snapshot", "update", "request_stop"):
            self.assertTrue(callable(getattr(controller, method)), f"missing {method}")
        json.dumps(controller.snapshot())

    def test_kernel_reaches_a_verified_result_without_any_network(self) -> None:
        module = load_controller_module()
        controller = module.build(
            FakeSpec({"goal": "turn on every required switch"}), FakeServices(), manifest_config()
        )
        result = controller.loop.run()
        self.assertEqual(result.status.value, "succeeded")
        records = controller.loop.state.verifications
        self.assertTrue(records, "the independent verifier must have run")
        self.assertEqual(records[-1].verdict.value, "satisfied")
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["world"], {"alpha": True, "beta": True})

    def test_inputs_override_the_config_defaults(self) -> None:
        module = load_controller_module()
        controller = module.build(
            FakeSpec(
                {
                    "goal": "turn on the switches the caller asked for",
                    "inputs": {"switches": {"gamma": False}, "required": {"gamma": True}},
                }
            ),
            FakeServices(),
            manifest_config(),
        )
        controller.loop.run()
        self.assertEqual(controller.snapshot()["world"], {"gamma": True})

    def test_request_stop_is_synchronous_and_the_engine_stops_as_cancelled(self) -> None:
        import threading

        module = load_controller_module()
        controller = module.build(FakeSpec({"goal": "stop check"}), FakeServices(), manifest_config())
        stop_event = threading.Event()
        controller.request_stop("author_test")  # synchronous boundary: must not wait for the engine
        stop_event.set()
        result = controller.run(stop_event)
        self.assertEqual(result.status.value, "cancelled")
        self.assertEqual(result.reason, "author_test")
        self.assertIs(result.resources_released, True)


if __name__ == "__main__":
    unittest.main()
