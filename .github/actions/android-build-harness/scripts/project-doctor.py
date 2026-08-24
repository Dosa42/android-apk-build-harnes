#!/usr/bin/env python3
"""Resolve an Android project's pinned build environment without editing its source."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - GitHub runners use Python 3.11+
    tomllib = None


class DoctorError(RuntimeError):
    """A project fact could not be resolved safely."""


@dataclass(frozen=True)
class AgpCompatibility:
    minimum_gradle: str
    gradle_major: int
    jdk: int
    default_build_tools: str
    default_ndk: str | None
    source: str


ANDROID_RELEASES = "https://developer.android.com/build/releases"

# Explicit compatibility data. A future/unknown AGP series is rejected instead of
# silently selecting a toolchain. Values are Android's documented release minima.
AGP_COMPATIBILITY: dict[str, AgpCompatibility] = {
    "3.5": AgpCompatibility("5.4.1", 5, 8, "28.0.3", None, f"{ANDROID_RELEASES}/past-releases/agp-3-5-0-release-notes"),
    "3.6": AgpCompatibility("5.6.4", 5, 8, "29.0.2", None, f"{ANDROID_RELEASES}/past-releases/agp-3-6-0-release-notes"),
    "4.0": AgpCompatibility("6.1.1", 6, 8, "29.0.2", None, f"{ANDROID_RELEASES}/past-releases/agp-4-0-0-release-notes"),
    "4.1": AgpCompatibility("6.5", 6, 8, "29.0.2", None, f"{ANDROID_RELEASES}/past-releases/agp-4-1-0-release-notes"),
    "4.2": AgpCompatibility("6.7.1", 6, 11, "30.0.2", None, f"{ANDROID_RELEASES}/past-releases/agp-4-2-0-release-notes"),
    "7.0": AgpCompatibility("7.0", 7, 11, "30.0.2", None, f"{ANDROID_RELEASES}/past-releases/agp-7-0-0-release-notes"),
    "7.1": AgpCompatibility("7.2", 7, 11, "30.0.3", None, f"{ANDROID_RELEASES}/past-releases/agp-7-1-0-release-notes"),
    "7.2": AgpCompatibility("7.3.3", 7, 11, "30.0.3", None, f"{ANDROID_RELEASES}/past-releases/agp-7-2-0-release-notes"),
    "7.3": AgpCompatibility("7.4", 7, 11, "30.0.3", None, f"{ANDROID_RELEASES}/past-releases/agp-7-3-0-release-notes"),
    "7.4": AgpCompatibility("7.5", 7, 11, "30.0.3", None, f"{ANDROID_RELEASES}/past-releases/agp-7-4-0-release-notes"),
    "8.0": AgpCompatibility("8.0", 8, 17, "30.0.3", "25.1.8937393", f"{ANDROID_RELEASES}/past-releases/agp-8-0-0-release-notes"),
    "8.1": AgpCompatibility("8.0", 8, 17, "33.0.1", "25.1.8937393", f"{ANDROID_RELEASES}/past-releases/agp-8-1-0-release-notes"),
    "8.2": AgpCompatibility("8.2", 8, 17, "34.0.0", "25.1.8937393", f"{ANDROID_RELEASES}/past-releases/agp-8-2-0-release-notes"),
    "8.3": AgpCompatibility("8.4", 8, 17, "34.0.0", "25.1.8937393", f"{ANDROID_RELEASES}/past-releases/agp-8-3-0-release-notes"),
    "8.4": AgpCompatibility("8.6", 8, 17, "34.0.0", "26.1.10909125", f"{ANDROID_RELEASES}/past-releases/agp-8-4-0-release-notes"),
    "8.5": AgpCompatibility("8.7", 8, 17, "34.0.0", "26.1.10909125", f"{ANDROID_RELEASES}/past-releases/agp-8-5-0-release-notes"),
    "8.6": AgpCompatibility("8.7", 8, 17, "34.0.0", "26.1.10909125", f"{ANDROID_RELEASES}/past-releases/agp-8-6-0-release-notes"),
    "8.7": AgpCompatibility("8.9", 8, 17, "34.0.0", "27.0.12077973", f"{ANDROID_RELEASES}/past-releases/agp-8-7-0-release-notes"),
    "8.8": AgpCompatibility("8.10.2", 8, 17, "35.0.0", "27.0.12077973", f"{ANDROID_RELEASES}/past-releases/agp-8-8-0-release-notes"),
    "8.9": AgpCompatibility("8.11.1", 8, 17, "35.0.0", "27.0.12077973", f"{ANDROID_RELEASES}/past-releases/agp-8-9-0-release-notes"),
    "8.10": AgpCompatibility("8.11.1", 8, 17, "35.0.0", "27.0.12077973", f"{ANDROID_RELEASES}/past-releases/agp-8-10-0-release-notes"),
    "8.11": AgpCompatibility("8.13", 8, 17, "35.0.0", "27.0.12077973", f"{ANDROID_RELEASES}/past-releases/agp-8-11-0-release-notes"),
    "8.12": AgpCompatibility("8.13", 8, 17, "35.0.0", "27.0.12077973", f"{ANDROID_RELEASES}/agp-8-12-0-release-notes"),
    "8.13": AgpCompatibility("8.13", 8, 17, "35.0.0", "27.0.12077973", f"{ANDROID_RELEASES}/agp-8-13-0-release-notes"),
    "9.0": AgpCompatibility("9.1.0", 9, 17, "36.0.0", "28.2.13676358", f"{ANDROID_RELEASES}/agp-9-0-0-release-notes"),
    "9.1": AgpCompatibility("9.3.1", 9, 17, "36.0.0", "28.2.13676358", f"{ANDROID_RELEASES}/agp-9-1-0-release-notes"),
    "9.2": AgpCompatibility("9.4.1", 9, 17, "36.0.0", "28.2.13676358", f"{ANDROID_RELEASES}/agp-9-2-0-release-notes"),
    "9.3": AgpCompatibility("9.5.0", 9, 17, "36.0.0", "28.2.13676358", f"{ANDROID_RELEASES}/agp-9-3-0-release-notes"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path, help="Target Android checkout root")
    parser.add_argument("--report", required=True, type=Path, help="JSON report path outside the target checkout")
    parser.add_argument("--tasks-file", type=Path, help="Full Gradle task-list log path outside the target checkout")
    parser.add_argument("--resolve-tasks", action="store_true", help="Run the Wrapper's tasks --all command and resolve real variants")
    parser.add_argument("--github-output", type=Path, help="GitHub output file; defaults to GITHUB_OUTPUT")
    return parser.parse_args()


def resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def ensure_output_outside_project(path: Path, project: Path, label: str) -> Path:
    target = resolved(path)
    try:
        target.relative_to(project)
    except ValueError:
        target.parent.mkdir(parents=True, exist_ok=True)
        return target
    raise DoctorError(f"{label} must be outside the target checkout: {target}")


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"\s*(\d+(?:\.\d+)*)", value)
    if not match:
        raise DoctorError(f"Cannot compare non-numeric version: {value!r}")
    parts = tuple(int(part) for part in match.group(1).split("."))
    return parts + (0,) * (3 - len(parts))


def version_series(value: str) -> str:
    parts = version_tuple(value)
    return f"{parts[0]}.{parts[1]}"


def normalize_accessor(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", ".", value.lower()).strip(".")


def scalar_version(value: Any) -> str | None:
    if isinstance(value, (str, int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in ("strictly", "require", "prefer"):
            candidate = value.get(key)
            if isinstance(candidate, (str, int, float)):
                return str(candidate)
    return None


def load_catalogs(project: Path) -> tuple[dict[str, str], dict[str, dict[str, str]], list[Path]]:
    versions: dict[str, str] = {}
    plugins: dict[str, dict[str, str]] = {}
    catalog_paths = sorted(project.glob("gradle/*.versions.toml"))
    default_catalog = project / "gradle" / "libs.versions.toml"
    if default_catalog.exists() and default_catalog not in catalog_paths:
        catalog_paths.append(default_catalog)
    if catalog_paths and tomllib is None:
        raise DoctorError("Python 3.11+ is required to parse Gradle version catalogs")
    for path in catalog_paths:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
        local_versions = document.get("versions", {})
        for key, value in local_versions.items():
            parsed = scalar_version(value)
            if parsed is not None:
                versions[normalize_accessor(str(key))] = parsed
        for key, value in document.get("plugins", {}).items():
            if not isinstance(value, dict):
                continue
            plugin_id = value.get("id")
            if not isinstance(plugin_id, str):
                continue
            plugin_version = scalar_version(value.get("version"))
            version_ref = None
            nested_version = value.get("version")
            if isinstance(nested_version, dict):
                ref_value = nested_version.get("ref")
                if isinstance(ref_value, str):
                    version_ref = normalize_accessor(ref_value)
            if plugin_version is None and version_ref:
                plugin_version = versions.get(version_ref)
            plugins[normalize_accessor(str(key))] = {
                "id": plugin_id,
                "version": plugin_version or "",
                "source": str(path),
            }
    return versions, plugins, sorted(set(catalog_paths))


def parse_properties(project: Path, catalog_versions: dict[str, str]) -> dict[str, str]:
    values = dict(catalog_versions)
    for properties in (project / "gradle.properties", project / "local.properties"):
        if not properties.exists():
            continue
        for raw_line in read_text(properties).splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", "!")) or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[normalize_accessor(key)] = value.strip()
    return values


def wrapper_version(project: Path) -> tuple[str, str, Path]:
    properties = project / "gradle" / "wrapper" / "gradle-wrapper.properties"
    wrapper = project / "gradlew"
    jar = project / "gradle" / "wrapper" / "gradle-wrapper.jar"
    missing = [str(path.relative_to(project)) for path in (properties, wrapper, jar) if not path.is_file()]
    if missing:
        raise DoctorError("Gradle Wrapper is incomplete; missing: " + ", ".join(missing))
    distribution_url = ""
    for line in read_text(properties).splitlines():
        if line.strip().startswith("distributionUrl="):
            distribution_url = line.split("=", 1)[1].strip().replace("\\:", ":")
            break
    distribution_url = unquote(distribution_url)
    match = re.search(r"/gradle-([0-9][0-9A-Za-z.+-]*)-(?:bin|all)\.zip(?:[?#].*)?$", distribution_url)
    if not match:
        raise DoctorError(f"Cannot derive the Gradle version from Wrapper distributionUrl: {distribution_url!r}")
    return match.group(1), distribution_url, properties


def gradle_files(project: Path) -> list[Path]:
    candidates: set[Path] = set()
    for name in ("settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts"):
        path = project / name
        if path.is_file():
            candidates.add(path)
    for pattern in ("*/build.gradle", "*/build.gradle.kts", "*/*/build.gradle", "*/*/build.gradle.kts"):
        for path in project.glob(pattern):
            if not any(part in {".git", ".gradle", "build", "android-engineer-log"} for part in path.parts):
                candidates.add(path)
    for base in (project / "buildSrc", project / "build-logic"):
        if not base.exists():
            continue
        for pattern in ("**/*.gradle", "**/*.gradle.kts", "**/*.kt"):
            for path in base.glob(pattern):
                if "build" not in path.parts:
                    candidates.add(path)
    return sorted(candidates)


def detect_agp(files: Iterable[Path], plugins: dict[str, dict[str, str]]) -> tuple[str, list[str]]:
    candidates: dict[str, set[str]] = {}
    for alias, metadata in plugins.items():
        if metadata["id"] == "com.android.application" and metadata["version"]:
            candidates.setdefault(metadata["version"], set()).add(f"{metadata['source']} [plugins].{alias}")
    patterns = (
        re.compile(r"\b(?:id\s*\(\s*|id\s+)[\"']com\.android\.(?:application|library)[\"']\s*\)?\s*version\s*[= ]?\s*[\"']([^\"']+)[\"']"),
        re.compile(r"com\.android\.tools\.build:gradle:([0-9][0-9A-Za-z.+-]*)"),
    )
    for path in files:
        text = read_text(path)
        for pattern in patterns:
            for match in pattern.finditer(text):
                candidates.setdefault(match.group(1), set()).add(str(path))
    if not candidates:
        raise DoctorError("Android Gradle Plugin version was not found in version catalogs, plugin declarations, or buildscript classpaths")
    if len(candidates) != 1:
        detail = "; ".join(f"{version}: {sorted(sources)}" for version, sources in sorted(candidates.items()))
        raise DoctorError(f"Conflicting Android Gradle Plugin versions were detected: {detail}")
    version = next(iter(candidates))
    return version, sorted(candidates[version])


def settings_modules(project: Path) -> tuple[list[str], dict[str, Path], Path | None]:
    settings = next((project / name for name in ("settings.gradle.kts", "settings.gradle") if (project / name).is_file()), None)
    if settings is None:
        return [], {}, None
    text = read_text(settings)
    modules: set[str] = set()
    for call in re.finditer(r"(?s)\binclude\s*\((.*?)\)", text):
        modules.update(value for value in re.findall(r"[\"'](:[^\"']+)[\"']", call.group(1)))
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("//") or not re.match(r"^include\s+", stripped):
            continue
        modules.update(value for value in re.findall(r"[\"'](:[^\"']+)[\"']", stripped))
    overrides: dict[str, Path] = {}
    override_pattern = re.compile(
        r"project\s*\(\s*[\"'](:[^\"']+)[\"']\s*\)\.projectDir\s*=\s*(?:file|File)\s*\(\s*[\"']([^\"']+)[\"']\s*\)"
    )
    for match in override_pattern.finditer(text):
        overrides[match.group(1)] = resolved(project / match.group(2))
    return sorted(modules), overrides, settings


def build_file_for(directory: Path) -> Path | None:
    return next((directory / name for name in ("build.gradle.kts", "build.gradle") if (directory / name).is_file()), None)


def app_plugin_applied(text: str, plugins: dict[str, dict[str, str]]) -> bool:
    for line in text.splitlines():
        code = line.split("//", 1)[0]
        if "com.android.application" in code and "apply false" not in code and "apply(false)" not in code:
            if re.search(r"(?:\bid\s*\(|\bid\s+|apply\s*\(?\s*plugin\s*=?)", code):
                return True
        for match in re.finditer(r"alias\s*\(\s*(?:libs\.)?plugins\.([A-Za-z0-9_.-]+)\s*\)", code):
            alias = normalize_accessor(match.group(1))
            metadata = plugins.get(alias)
            if metadata and metadata["id"] == "com.android.application" and "apply false" not in code and "apply(false)" not in code:
                return True
    return False


def discover_application_modules(
    project: Path, modules: list[str], overrides: dict[str, Path], plugins: dict[str, dict[str, str]]
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    candidates: list[tuple[str, Path]] = []
    root_build = build_file_for(project)
    if root_build and app_plugin_applied(read_text(root_build), plugins):
        candidates.append((":", project))
    for module in modules:
        directory = overrides.get(module, project.joinpath(*module.strip(":").split(":")))
        candidates.append((module, directory))
    if not modules:
        for build_file in sorted(project.glob("*/build.gradle*")):
            candidates.append((f":{build_file.parent.name}", build_file.parent))
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for gradle_path, directory in candidates:
        if gradle_path in seen:
            continue
        seen.add(gradle_path)
        build_file = build_file_for(directory)
        if build_file is None:
            warnings.append(f"Included module {gradle_path} has no build.gradle or build.gradle.kts at {directory}")
            continue
        if not app_plugin_applied(read_text(build_file), plugins):
            continue
        result.append(
            {
                "gradle_path": gradle_path,
                "directory": str(directory),
                "build_file": str(build_file),
            }
        )
    if not result:
        raise DoctorError("No module applying com.android.application was found; convention plugins must expose an application module statically")
    return sorted(result, key=lambda item: item["gradle_path"]), warnings


def resolve_reference(expression: str, values: dict[str, str]) -> str | None:
    direct = re.search(r"[\"'](?:android-)?([A-Za-z0-9_.-]+)[\"']", expression)
    if direct:
        return direct.group(1)
    number = re.search(r"(?<![A-Za-z0-9_])(\d+(?:\.\d+){0,3})(?![A-Za-z0-9_])", expression)
    if number:
        return number.group(1)
    catalog = re.search(r"(?:libs\.)?versions\.([A-Za-z0-9_.-]+)", expression)
    if catalog:
        return values.get(normalize_accessor(catalog.group(1)))
    identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", expression)
    ignored = {"get", "toInt", "as", "Int", "Integer", "project", "rootProject", "extra", "ext"}
    for identifier in reversed(identifiers):
        if identifier in ignored:
            continue
        candidate = values.get(normalize_accessor(identifier))
        if candidate:
            return candidate
    return None


def detect_compile_sdk(text: str, values: dict[str, str]) -> tuple[str | None, str | None]:
    # AGP 8.13+/9.x stable minor-API syntax.
    for match in re.finditer(r"(?s)\bcompileSdk\s*\{(.{0,900}?)\}", text):
        body = match.group(1)
        release = re.search(r"\bversion\s*=\s*release\s*\(\s*([^\)]+)\s*\)", body)
        if release:
            major = resolve_reference(release.group(1), values)
            minor = re.search(r"\b(?:it\.)?minorApiLevel\s*=\s*([^\s;\}\n]+)", body)
            minor_value = resolve_reference(minor.group(1), values) if minor else None
            if major:
                return (f"{major}.{minor_value}" if minor_value else major), "compileSdk release()/minorApiLevel DSL"
        preview = re.search(r"\bversion\s*=\s*preview\s*\(\s*[\"']([^\"']+)[\"']\s*\)", body)
        if preview:
            return preview.group(1), "compileSdk preview() DSL"
    line_match = re.search(r"(?m)^\s*compileSdk(?:Version)?\s*(?:=|\s)\s*([^\n;\}]+)", text)
    if line_match:
        value = resolve_reference(line_match.group(1), values)
        if value:
            minor_match = re.search(r"(?m)^\s*compileSdkMinor\s*=\s*([^\n;\}]+)", text)
            minor = resolve_reference(minor_match.group(1), values) if minor_match else None
            return (f"{value}.{minor}" if minor else value), "compileSdk/compileSdkMinor DSL"
    function_match = re.search(r"\bcompileSdkVersion\s*\(\s*([^\)]+)\s*\)", text)
    if function_match:
        value = resolve_reference(function_match.group(1), values)
        if value:
            return value, "compileSdkVersion() DSL"
    preview_match = re.search(r"\bcompileSdkPreview\s*=\s*[\"']([^\"']+)[\"']", text)
    if preview_match:
        return preview_match.group(1), "compileSdkPreview DSL"
    return None, None


def detect_declared_version(text: str, name: str, values: dict[str, str]) -> tuple[str | None, str | None]:
    match = re.search(rf"(?m)^\s*{re.escape(name)}\s*(?:=|\s)\s*([^\n;\}}]+)", text)
    if not match:
        return None, None
    value = resolve_reference(match.group(1), values)
    return (value, f"declared {name}") if value else (None, None)


def detect_cmake(text: str, values: dict[str, str]) -> tuple[str | None, str | None]:
    for match in re.finditer(r"(?s)\bcmake\s*\{(.{0,500}?)\}", text):
        version = re.search(r"(?m)^\s*version\s*=\s*([^\n;\}]+)", match.group(1))
        if version:
            value = resolve_reference(version.group(1), values)
            if value:
                return value, "declared externalNativeBuild.cmake.version"
    return None, None


def sdk_package_for_compile_sdk(value: str) -> str:
    normalized = value.removeprefix("android-")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", normalized):
        raise DoctorError(f"Unsafe or unsupported compileSdk value: {value!r}")
    return f"platforms;android-{normalized}"


def resolve_module_sdks(
    project: Path,
    application_modules: list[dict[str, Any]],
    settings: Path | None,
    root_build: Path | None,
    values: dict[str, str],
    compatibility: AgpCompatibility,
) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
    fallback_texts: list[tuple[str, str]] = []
    for path in (settings, root_build):
        if path and path.is_file():
            fallback_texts.append((str(path), read_text(path)))
    compile_sdks: set[str] = set()
    build_tools: set[str] = set()
    ndks: set[str] = set()
    cmakes: set[str] = set()
    warnings: list[str] = []
    for module in application_modules:
        text = read_text(Path(module["build_file"]))
        compile_sdk, compile_source = detect_compile_sdk(text, values)
        if compile_sdk is None:
            for fallback_path, fallback_text in fallback_texts:
                compile_sdk, compile_source = detect_compile_sdk(fallback_text, values)
                if compile_sdk:
                    compile_source = f"{compile_source} in {fallback_path}"
                    break
        if compile_sdk is None:
            raise DoctorError(f"compileSdk could not be resolved for application module {module['gradle_path']}")
        module["compile_sdk"] = compile_sdk
        module["compile_sdk_source"] = compile_source
        compile_sdks.add(compile_sdk)

        build_tools_value, build_tools_source = detect_declared_version(text, "buildToolsVersion", values)
        if build_tools_value is None:
            build_tools_value = compatibility.default_build_tools
            build_tools_source = "AGP documented default"
        module["build_tools_version"] = build_tools_value
        module["build_tools_source"] = build_tools_source
        build_tools.add(build_tools_value)

        ndk_value, ndk_source = detect_declared_version(text, "ndkVersion", values)
        if ndk_value:
            module["ndk_version"] = ndk_value
            module["ndk_source"] = ndk_source
            ndks.add(ndk_value)
        elif re.search(r"\b(?:externalNativeBuild|ndkBuild)\b", text):
            if compatibility.default_ndk:
                module["ndk_version"] = compatibility.default_ndk
                module["ndk_source"] = "AGP documented default for native project"
                ndks.add(compatibility.default_ndk)
            else:
                warnings.append(f"{module['gradle_path']} uses native build logic without a declared ndkVersion")

        cmake_value, cmake_source = detect_cmake(text, values)
        if cmake_value:
            module["cmake_version"] = cmake_value
            module["cmake_source"] = cmake_source
            cmakes.add(cmake_value)
    return sorted(compile_sdks), sorted(build_tools), sorted(ndks), sorted(cmakes), warnings


def locate_poms(project: Path) -> list[str]:
    result: list[str] = []
    for path in project.rglob("pom.xml"):
        relative = path.relative_to(project)
        if any(part in {".git", ".gradle", "build", "target", "android-engineer-log"} for part in relative.parts):
            continue
        result.append(str(relative))
    return sorted(result)


def canonical_task(token: str) -> str:
    return ":" + token.strip(":")


def discover_tasks(
    project: Path, wrapper: Path, application_modules: list[dict[str, Any]], tasks_file: Path
) -> tuple[dict[str, list[str]], list[str], list[str]]:
    command = ["bash", str(wrapper), "--no-daemon", "--console=plain", "--stacktrace", "tasks", "--all"]
    before = {item["build_file"]: sha256(Path(item["build_file"])) for item in application_modules}
    process = subprocess.run(
        command,
        cwd=project,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    tasks_file.write_text(process.stdout, encoding="utf-8")
    after = {item["build_file"]: sha256(Path(item["build_file"])) for item in application_modules}
    changed = sorted(path for path in before if before[path] != after[path])
    if changed:
        raise DoctorError("Gradle task discovery changed target configuration files: " + ", ".join(changed))
    if process.returncode != 0:
        tail = "\n".join(process.stdout.splitlines()[-80:])
        raise DoctorError(f"Gradle task discovery failed with exit code {process.returncode}. Full log: {tasks_file}\n{tail}")

    all_tasks: set[str] = set()
    for line in process.stdout.splitlines():
        if " - " not in line:
            continue
        token = line.split(None, 1)[0].strip()
        if re.fullmatch(r":?[A-Za-z0-9_.-]+(?::[A-Za-z0-9_.-]+)*", token):
            all_tasks.add(canonical_task(token))

    categorized: dict[str, list[str]] = {"assemble": [], "bundle": [], "lint": [], "test": [], "all": sorted(all_tasks)}
    variants: set[str] = set()
    module_paths = sorted((item["gradle_path"] for item in application_modules), key=len, reverse=True)
    for task in sorted(all_tasks):
        matched_module = next(
            (
                module
                for module in module_paths
                if (module == ":" and task.count(":") == 1) or (module != ":" and task.startswith(module + ":"))
            ),
            None,
        )
        if matched_module is None:
            continue
        task_name = task.rsplit(":", 1)[-1]
        for category in ("assemble", "bundle", "lint", "test"):
            if task_name == category or re.match(rf"^{category}[A-Z].*", task_name):
                categorized[category].append(task)
                break
        if task_name.startswith("assemble") and task_name != "assemble":
            suffix = task_name[len("assemble") :]
            if suffix and not suffix.endswith(("AndroidTest", "UnitTest", "TestFixtures", "Test")):
                variants.add(suffix[0].lower() + suffix[1:])
    if not categorized["assemble"]:
        raise DoctorError("No assemble task was exposed for the detected Android application modules")
    for key in ("assemble", "bundle", "lint", "test"):
        categorized[key] = sorted(set(categorized[key]))
    return categorized, sorted(variants), command


def write_github_outputs(path: Path | None, values: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            if isinstance(value, (dict, list, bool)):
                rendered = json.dumps(value, separators=(",", ":"), sort_keys=True)
            elif value is None:
                rendered = ""
            else:
                rendered = str(value)
            if "\n" in rendered or "\r" in rendered:
                marker = f"ANDROID_HARNESS_{key.upper().replace('-', '_')}"
                handle.write(f"{key}<<{marker}\n{rendered}\n{marker}\n")
            else:
                handle.write(f"{key}={rendered}\n")


def create_report(args: argparse.Namespace) -> dict[str, Any]:
    project = resolved(args.project)
    if not project.is_dir():
        raise DoctorError(f"Target checkout does not exist or is not a directory: {project}")
    report_path = ensure_output_outside_project(args.report, project, "Report path")
    tasks_file = args.tasks_file
    if args.resolve_tasks and tasks_file is None:
        raise DoctorError("--tasks-file is required with --resolve-tasks")
    resolved_tasks_file = ensure_output_outside_project(tasks_file, project, "Tasks file") if tasks_file else None

    gradle_version, distribution_url, wrapper_properties = wrapper_version(project)
    catalogs, plugin_aliases, catalog_paths = load_catalogs(project)
    values = parse_properties(project, catalogs)
    files = gradle_files(project)
    agp_version, agp_sources = detect_agp(files, plugin_aliases)
    series = version_series(agp_version)
    compatibility = AGP_COMPATIBILITY.get(series)
    if compatibility is None:
        supported = ", ".join(sorted(AGP_COMPATIBILITY, key=version_tuple))
        raise DoctorError(f"AGP {agp_version} is outside the explicit compatibility map. Supported series: {supported}")
    if version_tuple(gradle_version) < version_tuple(compatibility.minimum_gradle):
        raise DoctorError(
            f"Incompatible pinned versions: AGP {agp_version} requires Gradle >= {compatibility.minimum_gradle}, "
            f"but the Wrapper pins {gradle_version}. The harness will not upgrade either version."
        )
    if version_tuple(gradle_version)[0] != compatibility.gradle_major:
        raise DoctorError(
            f"Unverified AGP/Gradle major combination: AGP {agp_version} is mapped to Gradle "
            f"{compatibility.gradle_major}.x, but the Wrapper pins {gradle_version}. The harness will not guess."
        )

    modules, overrides, settings = settings_modules(project)
    application_modules, module_warnings = discover_application_modules(project, modules, overrides, plugin_aliases)
    root_build = build_file_for(project)
    compile_sdks, build_tools, ndks, cmakes, sdk_warnings = resolve_module_sdks(
        project, application_modules, settings, root_build, values, compatibility
    )
    packages = [sdk_package_for_compile_sdk(value) for value in compile_sdks]
    packages.extend(f"build-tools;{value}" for value in build_tools)
    packages.extend(f"ndk;{value}" for value in ndks)
    packages.extend(f"cmake;{value}" for value in cmakes)
    packages = sorted(set(packages))

    tasks: dict[str, list[str]] = {"assemble": [], "bundle": [], "lint": [], "test": [], "all": []}
    variants: list[str] = []
    task_command: list[str] | None = None
    if args.resolve_tasks and resolved_tasks_file:
        tasks, variants, task_command = discover_tasks(project, project / "gradlew", application_modules, resolved_tasks_file)

    inspected = sorted(set([wrapper_properties, *catalog_paths, *files, *(Path(item["build_file"]) for item in application_modules)]))
    poms = locate_poms(project)
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "ok",
        "project_root": str(project),
        "gradle": {
            "version": gradle_version,
            "distribution_url": distribution_url,
            "wrapper_properties": str(wrapper_properties),
            "minimum_for_agp": compatibility.minimum_gradle,
        },
        "android_gradle_plugin": {
            "version": agp_version,
            "series": series,
            "sources": agp_sources,
            "compatibility_source": compatibility.source,
        },
        "java": {
            "version": compatibility.jdk,
            "selection": "minimum runtime JDK required by the detected AGP series and compatible Wrapper",
        },
        "android_sdk": {
            "compile_sdks": compile_sdks,
            "build_tools_versions": build_tools,
            "ndk_versions": ndks,
            "cmake_versions": cmakes,
            "packages": packages,
        },
        "application_modules": application_modules,
        "selected_modules": [item["gradle_path"] for item in application_modules],
        "variants": variants,
        "tasks": tasks,
        "task_discovery": {
            "executed": bool(args.resolve_tasks),
            "command": task_command,
            "log": str(resolved_tasks_file) if resolved_tasks_file else None,
        },
        "maven": {"has_pom": bool(poms), "pom_files": poms},
        "writes": {
            "source_files_modified": False,
            "local_properties_written": False,
            "generated_paths_gradle_may_create_during_task_discovery": [".gradle/"] if args.resolve_tasks else [],
        },
        "inspected_files": [
            {"path": str(path.relative_to(project)), "sha256": sha256(path)}
            for path in inspected
            if path.is_file() and project in path.parents
        ],
        "warnings": sorted(set(module_warnings + sdk_warnings)),
    }
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    github_output = args.github_output or (Path(os.environ["GITHUB_OUTPUT"]) if os.environ.get("GITHUB_OUTPUT") else None)
    outputs = {
        "project-root": str(project),
        "gradle-version": gradle_version,
        "agp-version": agp_version,
        "java-version": compatibility.jdk,
        "compile-sdk": ",".join(compile_sdks),
        "build-tools-version": ",".join(build_tools),
        "ndk-version": ",".join(ndks),
        "cmake-version": ",".join(cmakes),
        "application-modules": [item["gradle_path"] for item in application_modules],
        "selected-modules": [item["gradle_path"] for item in application_modules],
        "variants": variants,
        "sdk-packages": packages,
        "has-pom": bool(poms),
        "environment-report": str(report_path),
        "gradle-tasks-file": str(resolved_tasks_file) if resolved_tasks_file else "",
    }
    write_github_outputs(github_output, outputs)
    return report


def main() -> int:
    args = parse_args()
    try:
        report = create_report(args)
    except DoctorError as exc:
        message = str(exc)
        try:
            project = resolved(args.project)
            report_path = ensure_output_outside_project(args.report, project, "Report path")
            report_path.write_text(
                json.dumps({"schema_version": 1, "status": "error", "error": message}, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception:
            pass
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error title=Android project doctor::{message.replace(chr(10), '%0A')}")
        else:
            print(f"ERROR: {message}", file=sys.stderr)
        return 2
    print(
        "Resolved Android environment: "
        f"Gradle {report['gradle']['version']}, AGP {report['android_gradle_plugin']['version']}, "
        f"JDK {report['java']['version']}, compileSdk {','.join(report['android_sdk']['compile_sdks'])}"
    )
    print(f"Application modules: {', '.join(report['selected_modules'])}")
    print(f"Exact SDK packages: {', '.join(report['android_sdk']['packages'])}")
    if report["task_discovery"]["executed"]:
        print(f"Discovered variants: {', '.join(report['variants']) or '(none)'}")
    print(f"Environment report: {args.report.expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
