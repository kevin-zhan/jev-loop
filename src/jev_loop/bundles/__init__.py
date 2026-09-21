"""Host-neutral bundle contract: manifest, discovery, resolution, validation, scaffolding.

This package is the single implementation of the Jev Bundle Specification.  The host, the
CLI, the doctor and any agent adapter share it, so a bundle is described, found, checked and
loaded the same way everywhere.  Nothing in here imports or executes bundle code: manifests
and declarations are data, and only ``host.runtime.load_controller`` runs trusted bundle
code — with the user's privileges, inside the managed worker process.
"""

from .conformance import conformance_report, run_probe, run_tests
from .discovery import (
    INSTRUCTIONS_FILENAME,
    MANIFEST_FILENAME,
    DiscoveryEntry,
    discover_bundles,
    discovery_root,
    find_bundle,
)
from .manifest import (
    BUNDLE_NAME_PATTERN,
    LEGACY_SCHEMA_VERSION,
    STANDARD_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    BundleManifest,
    ManifestError,
    load_manifest,
    parse_manifest,
)
from .resolve import (
    DIAGNOSTIC_REF,
    BundleRefError,
    ResolvedBundle,
    resolve_bundle_ref,
)
from .scaffold import ScaffoldError, create_bundle, template_tokens
from .validate import (
    ScanResult,
    fingerprint,
    manifest_summary,
    scan_bundle_files,
    validate_manifest_path,
    validate_ref,
    validate_resolved,
)

__all__ = [
    "BUNDLE_NAME_PATTERN",
    "DIAGNOSTIC_REF",
    "DiscoveryEntry",
    "INSTRUCTIONS_FILENAME",
    "LEGACY_SCHEMA_VERSION",
    "MANIFEST_FILENAME",
    "STANDARD_SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "BundleManifest",
    "BundleRefError",
    "ManifestError",
    "ResolvedBundle",
    "ScanResult",
    "ScaffoldError",
    "conformance_report",
    "create_bundle",
    "discover_bundles",
    "discovery_root",
    "find_bundle",
    "fingerprint",
    "load_manifest",
    "manifest_summary",
    "parse_manifest",
    "scan_bundle_files",
    "resolve_bundle_ref",
    "run_probe",
    "run_tests",
    "template_tokens",
    "validate_manifest_path",
    "validate_resolved",
    "validate_ref",
]
