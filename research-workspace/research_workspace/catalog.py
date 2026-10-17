"""Validated, packaged allowlist of reviewed research execution environments.

Catalog inspection never pulls an image or runs code. Historical replay must use
``recipe_source`` rather than bytes supplied by an evidence archive.
"""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat


ROOT = Path(__file__).resolve().parent.parent
MAX_CATALOG_BYTES = 65536
MAX_RECIPE_BYTES = 65536
IMAGE = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9./_:-]*@)?sha256:[0-9a-f]{64}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
IDENTIFIER = re.compile(r"[a-z][a-z0-9-]{2,63}\Z")
RECIPES = {"summary-python": "python", "summary-r": "r"}
LIMITS = {"cpus": 1, "memory_mb": 256, "pids_limit": 64,
          "timeout_seconds": 30, "max_output_bytes": 65536,
          "max_input_bytes": 65536, "max_input_rows": 1000}
ENTRY_KEYS = {"id", "recipe", "version", "name", "status", "image", "source",
              "recipe_sha256", "runtime", "dependencies", "platforms",
              "conditions", "limits", "validation", "provenance", "limitations"}


def _fail(message):
    raise ValueError(f"Invalid reviewed environment catalog: {message}")


def _object(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys):
        _fail(f"unexpected {label} fields.")


def _text(value, label, maximum=2000):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        _fail(f"invalid {label} text.")


def _strings(value, label, maximum=16, allow_empty=False):
    if not isinstance(value, list) or len(value) > maximum or (not value and not allow_empty):
        _fail(f"invalid {label} list.")
    for item in value:
        _text(item, label)
    if len(set(value)) != len(value):
        _fail(f"duplicate {label} values.")


def _safe_parts(relative):
    if not isinstance(relative, str) or len(relative) > 200:
        _fail("invalid packaged path.")
    path = PurePosixPath(relative)
    if (path.is_absolute() or str(path) != relative or not path.parts
            or any(part in (".", "..") or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", part) for part in path.parts)):
        _fail("unsafe packaged path.")
    return path.parts


def _read_packaged(relative, ceiling):
    """Open each path component without following symlinks; reject special files."""
    parts = _safe_parts(relative)
    descriptor = None
    try:
        descriptor = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
        os.close(descriptor)
        descriptor = child
        information = os.fstat(descriptor)
        if not stat.S_ISREG(information.st_mode) or information.st_size > ceiling:
            _fail("packaged file must be a bounded regular file.")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            content = stream.read(ceiling + 1)
        if not content or len(content) > ceiling:
            _fail("empty or oversized packaged file.")
        return content
    except OSError as exc:
        raise ValueError("Invalid reviewed environment catalog: packaged file unavailable or unsafe.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            _fail("duplicate JSON key.")
        result[key] = value
    return result


def _nonfinite(_):
    _fail("nonfinite JSON number.")


def _source(entry):
    parts = _safe_parts(entry["source"])
    suffix = ".py" if entry["runtime"]["id"] == "python" else ".R"
    if len(parts) < 2 or parts[0] != "recipes" or not parts[-1].endswith(suffix):
        _fail("recipe source must remain in the packaged recipes directory.")
    content = _read_packaged(entry["source"], MAX_RECIPE_BYTES)
    if hashlib.sha256(content).hexdigest() != entry["recipe_sha256"]:
        _fail("packaged recipe hash does not match its reviewed entry.")
    return content


def _validate(entry, *, check_source=True):
    _object(entry, ENTRY_KEYS, "environment")
    if not isinstance(entry["id"], str) or not IDENTIFIER.fullmatch(entry["id"]):
        _fail("invalid versioned identifier.")
    if not isinstance(entry["recipe"], str) or entry["recipe"] not in RECIPES:
        _fail("unreviewed recipe identifier.")
    if type(entry["version"]) is not int or not 1 <= entry["version"] <= 1000:
        _fail("invalid environment version.")
    if entry["id"] != f"{entry['recipe']}-v{entry['version']}":
        _fail("identifier and recipe version disagree.")
    _text(entry["name"], "environment name", 120)
    if entry["status"] not in ("active", "archived", "candidate"):
        _fail("invalid environment status.")
    if not isinstance(entry["image"], str) or len(entry["image"]) > 256 or not IMAGE.fullmatch(entry["image"]):
        _fail("an immutable SHA-256 image reference is required.")
    if not isinstance(entry["recipe_sha256"], str) or not SHA256.fullmatch(entry["recipe_sha256"]):
        _fail("invalid recipe SHA-256.")
    runtime = entry["runtime"]
    _object(runtime, {"id", "version", "version_evidence"}, "runtime")
    if runtime["id"] != RECIPES[entry["recipe"]]:
        _fail("runtime does not match its reviewed recipe.")
    _text(runtime["version"], "runtime version", 80)
    _text(runtime["version_evidence"], "runtime version evidence")
    dependencies = entry["dependencies"]
    _object(dependencies, {"standard_library", "additional_packages", "statement"}, "dependencies")
    _strings(dependencies["standard_library"], "standard libraries")
    if dependencies["additional_packages"] != []:
        _fail("these reviewed methods require no additional packages.")
    _text(dependencies["statement"], "dependency statement")
    platforms = entry["platforms"]
    if not isinstance(platforms, list) or not 1 <= len(platforms) <= 8:
        _fail("invalid platform evidence list.")
    platform_ids = set()
    for platform in platforms:
        _object(platform, {"os", "architecture", "evidence_status", "evidence_boundary"}, "platform")
        if platform["os"] != "linux" or platform["architecture"] not in ("arm64", "amd64"):
            _fail("unsupported declared platform.")
        if platform["evidence_status"] not in ("PASS", "NOT RUN"):
            _fail("invalid platform evidence status.")
        _text(platform["evidence_boundary"], "platform evidence boundary")
        identity = (platform["os"], platform["architecture"])
        if identity in platform_ids:
            _fail("duplicate platform.")
        platform_ids.add(identity)
    _object(entry["conditions"], {"baseline", "alternative"}, "method condition")
    for condition in entry["conditions"].values():
        _text(condition, "method condition")
    _object(entry["limits"], LIMITS, "resource limit")
    for key, ceiling in LIMITS.items():
        if type(entry["limits"][key]) is not int or entry["limits"][key] != ceiling:
            _fail("resource limits must match the reviewed runtime contract.")
    validation = entry["validation"]
    _object(validation, {"relative_tolerance", "absolute_tolerance", "exact_fields", "independent_check"}, "validation")
    for key, expected in (("relative_tolerance", 1e-12), ("absolute_tolerance", 1e-9)):
        if type(validation[key]) not in (int, float) or validation[key] != expected:
            _fail("validation tolerance differs from the independent result check.")
    if validation["exact_fields"] != ["condition", "group", "count"]:
        _fail("exact validation fields differ from the result contract.")
    _text(validation["independent_check"], "independent validation")
    provenance = entry["provenance"]
    _object(provenance, {"repository", "source_revision", "image_origin", "build_inputs_status", "build_inputs", "clean_rebuild", "execution_evidence"}, "provenance")
    if provenance["repository"] != "https://github.com/EduCloud-Ecosystem/waypoint":
        _fail("unexpected recipe repository.")
    if not isinstance(provenance["source_revision"], str) or not REVISION.fullmatch(provenance["source_revision"]):
        _fail("source provenance requires a full commit revision.")
    _text(provenance["image_origin"], "image provenance")
    _object(provenance["clean_rebuild"], {"status", "boundary"}, "clean rebuild")
    if provenance["build_inputs_status"] == "NOT RECORDED":
        if provenance["build_inputs"] != [] or provenance["clean_rebuild"]["status"] != "NOT RUN" or "@" not in entry["image"]:
            _fail("legacy opaque images cannot attest a source rebuild or use local build IDs.")
    elif provenance["build_inputs_status"] == "RECORDED":
        inputs = provenance["build_inputs"]
        if not isinstance(inputs, list) or not 5 <= len(inputs) <= 12:
            _fail("source builds require bounded recorded inputs.")
        kinds = set()
        for item in inputs:
            _object(item, {"kind", "reference", "sha256"}, "build input")
            if item["kind"] not in {"runtime-source", "base-image", "buildkit-image", "dockerfile", "build-script", "input-contract", "package-inventory", "build-record"} or item["kind"] in kinds:
                _fail("unknown or duplicate build input kind.")
            kinds.add(item["kind"])
            _text(item["reference"], "build input reference")
            if not isinstance(item["sha256"], str) or not SHA256.fullmatch(item["sha256"]):
                _fail("invalid build input SHA-256.")
        if not {"runtime-source", "base-image", "dockerfile", "build-script", "input-contract"} <= kinds:
            _fail("incomplete source build contract.")
        if provenance["clean_rebuild"]["status"] not in ("PASS", "FAIL", "NOT RUN"):
            _fail("invalid clean build evidence status.")
        if entry["status"] == "active" and provenance["clean_rebuild"]["status"] != "PASS":
            _fail("source-built active environments require passing build evidence.")
    else:
        _fail("invalid build input status.")
    _text(provenance["clean_rebuild"]["boundary"], "clean rebuild boundary")
    evidence = provenance["execution_evidence"]
    _object(evidence, {"status", "reference", "boundary"}, "execution evidence")
    if evidence["status"] not in ("PASS", "FAIL", "NOT RUN"):
        _fail("invalid execution evidence status.")
    _text(evidence["reference"], "execution evidence reference")
    _text(evidence["boundary"], "execution evidence boundary")
    _strings(entry["limitations"], "limitations", allow_empty=True)
    parts = _safe_parts(entry["source"])
    suffix = ".py" if runtime["id"] == "python" else ".R"
    if len(parts) < 2 or parts[0] != "recipes" or not parts[-1].endswith(suffix):
        _fail("recipe source must remain in the packaged recipes directory.")
    if check_source:
        _source(entry)


def validate_snapshot(entry, recipe, recipe_sha256, image):
    """Validate untrusted exported metadata without reading files or granting trust.

    A structurally valid snapshot is a claim from its exporting operator. Only
    resolve_environment/recipe_source may authorize local packaged execution.
    """
    _validate(entry, check_source=False)
    if (entry["recipe"], entry["recipe_sha256"], entry["image"]) != (recipe, recipe_sha256, image):
        _fail("exported environment does not match run lineage.")


def _load():
    try:
        catalog = json.loads(_read_packaged("environments.json", MAX_CATALOG_BYTES),
                             object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid reviewed environment catalog: unreadable JSON.") from exc
    _object(catalog, {"schema_version", "environments"}, "catalog")
    if type(catalog["schema_version"]) is not int or catalog["schema_version"] != 1:
        _fail("unsupported schema version.")
    entries = catalog["environments"]
    if not isinstance(entries, list) or not 2 <= len(entries) <= 16:
        _fail("invalid environment count.")
    identifiers, identities, active = set(), set(), set()
    for entry in entries:
        _validate(entry)
        identity = (entry["recipe"], entry["recipe_sha256"], entry["image"])
        if entry["id"] in identifiers or identity in identities:
            _fail("duplicate environment identity.")
        identifiers.add(entry["id"])
        identities.add(identity)
        if entry["status"] == "active":
            if entry["recipe"] in active:
                _fail("ambiguous active recipe version.")
            active.add(entry["recipe"])
    if active != set(RECIPES):
        _fail("one active environment is required for each reviewed recipe.")
    return entries


def list_environments(configured_images=None):
    """Return validated metadata; availability requires the exact configured image.

    No configured images means metadata-only inspection, so all entries have
    ``available=False``. Archived entries are never available for new runs.
    """
    images = {} if configured_images is None else configured_images
    if not isinstance(images, dict) or set(images) - set(RECIPES.values()):
        raise ValueError("Configure only reviewed Python and R image mappings.")
    if any(not isinstance(image, str) or len(image) > 256 or not IMAGE.fullmatch(image) for image in images.values()):
        raise ValueError("Configured images must be immutable named SHA-256 references.")
    entries = deepcopy(_load())
    for entry in entries:
        entry["available"] = entry["status"] == "active" and images.get(entry["runtime"]["id"]) == entry["image"]
    return entries


def resolve_environment(recipe, recipe_sha256, image):
    """Resolve an exact trusted identity, including archived historical versions."""
    if not all(isinstance(value, str) for value in (recipe, recipe_sha256, image)):
        raise ValueError("A complete reviewed recipe and image identity is required.")
    for entry in _load():
        if (entry["recipe"], entry["recipe_sha256"], entry["image"]) == (recipe, recipe_sha256, image):
            return deepcopy(entry)
    raise ValueError("This recipe, source hash, and image pair is not in the reviewed environment catalog.")


def recipe_source(entry):
    """Return only hash-verified packaged bytes; never trust caller-supplied paths."""
    if not isinstance(entry, dict) or set(entry) not in (ENTRY_KEYS, ENTRY_KEYS | {"available"}):
        raise ValueError("An unmodified reviewed catalog entry is required.")
    if "available" in entry and type(entry["available"]) is not bool:
        raise ValueError("Invalid environment availability metadata.")
    trusted = resolve_environment(entry["recipe"], entry["recipe_sha256"], entry["image"])
    if any(entry[key] != trusted[key] for key in ENTRY_KEYS):
        raise ValueError("Environment metadata differs from the trusted packaged catalog.")
    return _source(trusted)
