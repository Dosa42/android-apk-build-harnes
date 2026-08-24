#!/usr/bin/env python3
"""Static Samsung DeX-related audit of final merged Android manifests.

This script deliberately reports manifest evidence only.  It never describes an
emulator, generic Android desktop window, or static manifest pass as a real
Samsung DeX runtime test.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any


ANDROID_NS = "http://schemas.android.com/apk/res/android"
A = f"{{{ANDROID_NS}}}"
FLEXIBLE_ORIENTATIONS = {None, "", "unspecified", "behind", "sensor", "fullSensor", "user", "fullUser"}
MERGED_MARKERS = {"merged_manifest", "merged_manifests", "packaged_manifests"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Audit targetSdk, resizeableActivity, fixed orientation, and required "
            "touchscreen declarations. Exit 0=manifest checks pass, 1=definite "
            "manifest failure, 2=indeterminate/no readable final merged manifest."
        )
    )
    parser.add_argument("--project", "--project-dir", dest="project", required=True)
    parser.add_argument("--variant", choices=("debug", "release", "both"), default="both")
    parser.add_argument("--merged-manifest", action="append", default=[], help="Exact merged manifest; repeatable")
    parser.add_argument("--report", required=True, help="JSON report path")
    parser.add_argument("--markdown-report", help="Markdown report path")
    return parser.parse_args()


def relative_module(path: pathlib.Path, project: pathlib.Path) -> pathlib.Path:
    try:
        relative = path.relative_to(project)
    except ValueError:
        return project
    if "build" in relative.parts:
        index = relative.parts.index("build")
        return project.joinpath(*relative.parts[:index]).resolve()
    if "src" in relative.parts:
        index = relative.parts.index("src")
        return project.joinpath(*relative.parts[:index]).resolve()
    return project


def path_variant(path: pathlib.Path) -> str | None:
    lower_parts = [part.lower() for part in path.parts]
    for variant in ("debug", "release"):
        if (
            variant in lower_parts
            or any(part.endswith(variant) for part in lower_parts)
            or re.search(rf"(?:^|[-_.]){variant}(?:[-_.]|$)", path.as_posix().lower())
        ):
            return variant
    return None


def built_modules(project: pathlib.Path, report_path: pathlib.Path) -> set[pathlib.Path]:
    current = report_path.parent / "current-artifacts.txt"
    modules: set[pathlib.Path] = set()
    if not current.is_file():
        return modules
    for line in current.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        artifact = pathlib.Path(line.strip())
        if not artifact.is_absolute():
            artifact = project / artifact
        try:
            artifact = artifact.resolve(strict=True)
            modules.add(relative_module(artifact, project))
        except OSError:
            continue
    return modules


def merged_priority(path: pathlib.Path) -> tuple[int, int]:
    parts = set(path.parts)
    if "merged_manifests" in parts:
        priority = 4
    elif "merged_manifest" in parts:
        priority = 3
    elif "packaged_manifests" in parts:
        priority = 2
    else:
        priority = 0
    try:
        timestamp = path.stat().st_mtime_ns
    except OSError:
        timestamp = 0
    return priority, timestamp


def discover_manifests(
    project: pathlib.Path,
    report_path: pathlib.Path,
    requested_variant: str,
    exact: list[str],
) -> list[tuple[pathlib.Path, str]]:
    if exact:
        return [(pathlib.Path(item).resolve(), "merged-explicit") for item in exact]

    modules = built_modules(project, report_path)
    generated: list[pathlib.Path] = []
    for path in project.rglob("AndroidManifest.xml"):
        try:
            relative = path.relative_to(project)
        except ValueError:
            continue
        if "build" not in relative.parts or "intermediates" not in relative.parts:
            continue
        if not any(marker in relative.parts for marker in MERGED_MARKERS):
            continue
        if any("androidtest" in part.lower() or "unittest" in part.lower() for part in relative.parts):
            continue
        variant = path_variant(path)
        if requested_variant != "both" and variant not in {requested_variant, None}:
            continue
        module = relative_module(path, project)
        if modules and module not in modules:
            continue
        generated.append(path.resolve())

    # AGP can retain multiple equivalent merged/packaged surfaces.  Select the
    # highest-priority, newest final merged manifest per module and variant.
    selected: dict[tuple[pathlib.Path, str | None], pathlib.Path] = {}
    for path in generated:
        key = (relative_module(path, project), path_variant(path))
        previous = selected.get(key)
        if previous is None or merged_priority(path) > merged_priority(previous):
            selected[key] = path
    if selected:
        return [(path, "merged-generated") for path in sorted(selected.values(), key=str)]

    source: list[tuple[pathlib.Path, str]] = []
    for path in project.rglob("src/main/AndroidManifest.xml"):
        resolved = path.resolve()
        module = relative_module(resolved, project)
        if modules and module not in modules:
            continue
        source.append((resolved, "source-fallback"))
    return sorted(source, key=lambda item: str(item[0]))


def android_value(element: ET.Element, name: str) -> str | None:
    return element.get(A + name)


def parse_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return None


def parse_target(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value, 10)
    except ValueError:
        return None


def effective_resizable(explicit: str | None, inherited: bool | None) -> tuple[bool | None, str]:
    parsed = parse_bool(explicit)
    if explicit is not None:
        return parsed, "explicit" if parsed is not None else "unresolved-resource-or-placeholder"
    return inherited, "inherited"


def audit_manifest(path: pathlib.Path, source_kind: str, project: pathlib.Path) -> dict[str, Any]:
    tree = ET.parse(path)
    root = tree.getroot()
    uses_sdk = root.find("uses-sdk")
    target_declaration = android_value(uses_sdk, "targetSdkVersion") if uses_sdk is not None else None
    target_sdk = parse_target(target_declaration)
    target_check = "pass" if target_sdk is not None and target_sdk >= 24 else "fail" if target_sdk is not None else "unknown"

    application = root.find("application")
    application_declaration = android_value(application, "resizeableActivity") if application is not None else None
    application_explicit = parse_bool(application_declaration)
    if application_declaration is not None:
        application_effective = application_explicit
        application_source = "explicit" if application_explicit is not None else "unresolved-resource-or-placeholder"
    elif target_sdk is not None:
        application_effective = target_sdk >= 24
        application_source = "platform-default-from-targetSdk"
    else:
        application_effective = None
        application_source = "unknown-targetSdk-default"

    activities: list[dict[str, Any]] = []
    fixed_orientations: list[dict[str, str | None]] = []
    resizable_values: list[bool | None] = [application_effective]
    if application is not None:
        for activity in application.findall("activity"):
            name = android_value(activity, "name") or "(unnamed activity)"
            declaration = android_value(activity, "resizeableActivity")
            effective, effective_source = effective_resizable(declaration, application_effective)
            orientation = android_value(activity, "screenOrientation")
            is_fixed = orientation not in FLEXIBLE_ORIENTATIONS
            item = {
                "name": name,
                "resizeable_declaration": declaration,
                "resizeable_effective": effective,
                "resizeable_source": effective_source,
                "screen_orientation": orientation,
                "fixed_orientation": is_fixed,
            }
            activities.append(item)
            resizable_values.append(effective)
            if is_fixed:
                fixed_orientations.append({"activity": name, "screen_orientation": orientation})

    if any(value is False for value in resizable_values):
        resizable_check = "fail"
    elif any(value is None for value in resizable_values) or application is None:
        resizable_check = "unknown"
    else:
        resizable_check = "pass"

    features: list[dict[str, Any]] = []
    required_touchscreen: list[str] = []
    unknown_touchscreen: list[str] = []
    for feature in root.findall("uses-feature"):
        name = android_value(feature, "name")
        if not name or not name.startswith("android.hardware.touchscreen"):
            continue
        declaration = android_value(feature, "required")
        required = True if declaration is None else parse_bool(declaration)
        features.append({"name": name, "required_declaration": declaration, "required_effective": required})
        if required is True:
            required_touchscreen.append(name)
        elif required is None:
            unknown_touchscreen.append(name)

    orientation_check = "fail" if fixed_orientations else "pass"
    touchscreen_check = "fail" if required_touchscreen else "unknown" if unknown_touchscreen else "pass"
    check_values = [target_check, resizable_check, orientation_check, touchscreen_check]
    if "fail" in check_values:
        manifest_status = "failed"
    elif "unknown" in check_values or source_kind == "source-fallback":
        manifest_status = "indeterminate"
    else:
        manifest_status = "passed"

    try:
        relative = str(path.relative_to(project))
    except ValueError:
        relative = None
    return {
        "path": str(path),
        "relative_path": relative,
        "source_kind": source_kind,
        "module": str(relative_module(path, project)),
        "variant": path_variant(path),
        "status": manifest_status,
        "package": root.get("package"),
        "target_sdk": {
            "declaration": target_declaration,
            "resolved_integer": target_sdk,
            "at_least_24": target_check,
        },
        "resizeable_activity": {
            "application_declaration": application_declaration,
            "application_effective": application_effective,
            "application_effective_source": application_source,
            "check": resizable_check,
            "activities": activities,
        },
        "fixed_orientation": {
            "check": orientation_check,
            "activities": fixed_orientations,
        },
        "required_touchscreen": {
            "check": touchscreen_check,
            "required_features": required_touchscreen,
            "unresolved_features": unknown_touchscreen,
            "features": features,
        },
    }


def write_markdown(report: dict[str, Any], destination: pathlib.Path) -> None:
    lines = [
        "# Samsung DeX manifest audit", "",
        f"- Static manifest result: **{report['status']}**",
        f"- Runtime DeX verification: **{report['runtime_verification']['status']}**",
        "- A generic emulator is not treated as Samsung DeX.", "",
        "| Manifest | Kind | Variant | targetSdk ≥24 | Resizable | Fixed orientation | Touchscreen required | Result |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report["manifests"]:
        lines.append(
            f"| `{item['relative_path'] or item['path']}` | {item['source_kind']} | {item['variant'] or 'unknown'} | "
            f"{item['target_sdk']['at_least_24']} | {item['resizeable_activity']['check']} | "
            f"{item['fixed_orientation']['check']} | {item['required_touchscreen']['check']} | {item['status']} |"
        )
    if not report["manifests"]:
        lines.append("| — | — | — | unknown | unknown | unknown | unknown | indeterminate |")
    if report["findings"]:
        lines.extend(["", "## Findings", ""])
        lines.extend(f"- {finding}" for finding in report["findings"])
    lines.extend([
        "", "This is a static compatibility audit. Responsive layout, mouse/keyboard behavior, external-display behavior, and actual Samsung DeX execution require a DeX-capable Samsung device and were not tested here.", ""
    ])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    project = pathlib.Path(args.project).resolve()
    report_path = pathlib.Path(args.report).resolve()
    markdown_path = pathlib.Path(args.markdown_report).resolve() if args.markdown_report else report_path.with_suffix(".md")
    manifests: list[dict[str, Any]] = []
    findings: list[str] = []

    if not project.is_dir():
        findings.append(f"Project directory does not exist: {project}")
    else:
        for path, source_kind in discover_manifests(project, report_path, args.variant, args.merged_manifest):
            if not path.is_file():
                findings.append(f"Manifest does not exist: {path}")
                continue
            try:
                manifests.append(audit_manifest(path, source_kind, project))
            except (ET.ParseError, OSError) as error:
                findings.append(f"Could not parse {path}: {error}")

    statuses = [item["status"] for item in manifests]
    if "failed" in statuses:
        status = "failed"
        exit_code = 1
    elif not statuses or "indeterminate" in statuses:
        status = "indeterminate"
        exit_code = 2
    else:
        status = "manifest_checks_pass"
        exit_code = 0

    for item in manifests:
        rel = item["relative_path"] or item["path"]
        if item["target_sdk"]["at_least_24"] == "fail":
            findings.append(f"{rel}: targetSdkVersion is below 24.")
        elif item["target_sdk"]["at_least_24"] == "unknown":
            findings.append(f"{rel}: targetSdkVersion could not be resolved from this manifest.")
        if item["resizeable_activity"]["check"] == "fail":
            findings.append(f"{rel}: application or activity is effectively non-resizable.")
        elif item["resizeable_activity"]["check"] == "unknown":
            findings.append(f"{rel}: resizable behavior could not be fully resolved.")
        for fixed in item["fixed_orientation"]["activities"]:
            findings.append(f"{rel}: {fixed['activity']} fixes orientation to {fixed['screen_orientation']}.")
        for feature in item["required_touchscreen"]["required_features"]:
            findings.append(f"{rel}: manifest requires {feature}.")
        if item["source_kind"] == "source-fallback":
            findings.append(f"{rel}: only a source manifest was available; final Gradle manifest merging was not audited.")

    report = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "project": str(project),
        "requested_variant": args.variant,
        "status": status,
        "exit_code": exit_code,
        "manifests": manifests,
        "findings": findings,
        "runtime_verification": {
            "status": "not_performed",
            "samsung_dex_session_tested": False,
            "emulator_claimed_as_samsung_dex": False,
            "reason": "This script performs static manifest inspection only.",
        },
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    write_markdown(report, markdown_path)
    print(f"DeX manifest audit: {status}; JSON report: {report_path}; Markdown report: {markdown_path}")
    for finding in findings:
        print(f"FINDING: {finding}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
