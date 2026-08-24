#!/usr/bin/env python3
"""Verify and stage only APK/AAB files attributable to the current build run."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from typing import Any


EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_MISSING = 2
EXIT_INVALID = 3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Stage and inspect current-run Android artifacts. Exit 2 means a "
            "required current artifact is missing; exit 3 means an artifact "
            "failed an available integrity/signing/alignment check."
        )
    )
    parser.add_argument("--project", "--project-dir", dest="project", required=True)
    parser.add_argument("--variant", choices=("debug", "release", "both"), default="debug")
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--report-dir", required=True)
    parser.add_argument("--current-list", help="Manifest of source artifact paths")
    parser.add_argument("--since-file", help="Timestamp marker or file containing an epoch")
    parser.add_argument("--since-epoch", help="Epoch seconds or epoch nanoseconds")
    parser.add_argument("--require-apk", dest="require_apk", action="store_true", default=True)
    parser.add_argument("--allow-missing-apk", dest="require_apk", action="store_false")
    parser.add_argument("--require-aab", action="store_true")
    parser.add_argument("--json-report")
    parser.add_argument("--markdown-report")
    return parser.parse_args()


def utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_relative_to(path: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def parse_epoch(value: str) -> int:
    rendered = value.strip()
    if re.fullmatch(r"[0-9]+", rendered):
        number = int(rendered)
        return number * 1_000_000_000 if number < 100_000_000_000_000 else number
    number = float(rendered)
    return int(number * 1_000_000_000)


def resolve_boundary(args: argparse.Namespace, report_dir: pathlib.Path) -> tuple[int | None, str | None]:
    if args.since_epoch:
        return parse_epoch(args.since_epoch), "--since-epoch"
    marker = pathlib.Path(args.since_file).resolve() if args.since_file else report_dir / "build-start.marker"
    if not marker.is_file():
        epoch_file = report_dir / "build-start.epoch-ns"
        if not epoch_file.is_file():
            return None, None
        try:
            return parse_epoch(epoch_file.read_text(encoding="utf-8")), str(epoch_file)
        except (OSError, ValueError):
            return None, str(epoch_file)
    try:
        text = marker.read_text(encoding="utf-8").strip()
        if text:
            try:
                return parse_epoch(text), str(marker)
            except ValueError:
                pass
        return marker.stat().st_mtime_ns, str(marker)
    except OSError:
        return None, str(marker)


def output_artifact(path: pathlib.Path, project: pathlib.Path) -> bool:
    try:
        relative = path.relative_to(project)
    except ValueError:
        return False
    parts = relative.parts
    return (
        path.suffix.lower() in {".apk", ".aab"}
        and "build" in parts
        and "outputs" in parts
    )


def read_candidates(
    project: pathlib.Path,
    report_dir: pathlib.Path,
    current_list_arg: str | None,
    boundary_ns: int,
) -> tuple[list[pathlib.Path], str]:
    manifest = pathlib.Path(current_list_arg).resolve() if current_list_arg else report_dir / "current-artifacts.txt"
    candidates: list[pathlib.Path] = []
    if manifest.is_file():
        mode = f"manifest:{manifest}"
        for entry in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
            if not entry.strip():
                continue
            candidate = pathlib.Path(entry.strip())
            if not candidate.is_absolute():
                candidate = project / candidate
            try:
                candidate = candidate.resolve(strict=True)
                if (
                    candidate.is_file()
                    and is_relative_to(candidate, project)
                    and output_artifact(candidate, project)
                    and candidate.stat().st_mtime_ns > boundary_ns
                ):
                    candidates.append(candidate)
            except OSError:
                continue
    else:
        mode = "strict timestamp scan"
        for candidate in project.rglob("*"):
            try:
                resolved = candidate.resolve(strict=True)
                if (
                    resolved.is_file()
                    and output_artifact(resolved, project)
                    and resolved.stat().st_mtime_ns > boundary_ns
                ):
                    candidates.append(resolved)
            except OSError:
                continue
    return sorted(set(candidates), key=str), mode


def artifact_variant(path: pathlib.Path) -> str | None:
    lower_parts = [part.lower() for part in path.parts]
    for variant in ("debug", "release"):
        if variant in lower_parts:
            return variant
        if re.search(rf"(?:^|[-_.]){variant}(?:[-_.]|$)", path.stem.lower()):
            return variant
    return None


def version_key(path: pathlib.Path) -> tuple[int, ...]:
    numbers = tuple(int(item) for item in re.findall(r"\d+", path.parent.name))
    return numbers or (0,)


def sdk_roots(project: pathlib.Path) -> list[pathlib.Path]:
    roots: list[pathlib.Path] = []
    for name in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = os.environ.get(name)
        if value:
            roots.append(pathlib.Path(value).expanduser())
    local_properties = project / "local.properties"
    if local_properties.is_file():
        for line in local_properties.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("sdk.dir="):
                value = line.split("=", 1)[1].strip().replace("\\:", ":").replace("\\\\", "\\")
                roots.append(pathlib.Path(value).expanduser())
    result: list[pathlib.Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            continue
        if resolved not in result:
            result.append(resolved)
    return result


def find_tool(name: str, project: pathlib.Path) -> pathlib.Path | None:
    on_path = shutil.which(name)
    if on_path:
        return pathlib.Path(on_path).resolve()
    candidates: list[pathlib.Path] = []
    for sdk in sdk_roots(project):
        if name in {"apksigner", "zipalign", "aapt", "aapt2"}:
            candidates.extend(sdk.glob(f"build-tools/*/{name}"))
        elif name == "apkanalyzer":
            candidates.extend(sdk.glob("cmdline-tools/*/bin/apkanalyzer"))
            candidates.extend(sdk.glob("tools/bin/apkanalyzer"))
    executable = [item for item in candidates if item.is_file() and os.access(item, os.X_OK)]
    return sorted(executable, key=version_key, reverse=True)[0].resolve() if executable else None


def compact_output(output: str, limit: int = 6000) -> str:
    cleaned = output.replace("\x00", "").strip()
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "\n… output truncated …"


def run_tool(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "exit_code": completed.returncode,
        "output": compact_output(completed.stdout),
    }


def unavailable() -> dict[str, Any]:
    return {"status": "unavailable", "exit_code": None, "output": None}


def parse_badging(output: str) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    package = re.search(r"^package:\s+name='([^']*)'(?:\s+versionCode='([^']*)')?(?:\s+versionName='([^']*)')?", output, re.MULTILINE)
    if package:
        facts.update({"package_name": package.group(1), "version_code": package.group(2), "version_name": package.group(3)})
    for label, key in (("sdkVersion", "min_sdk"), ("targetSdkVersion", "target_sdk")):
        match = re.search(rf"^{label}:'([^']*)'", output, re.MULTILINE)
        if match:
            facts[key] = match.group(1)
    native = re.search(r"^native-code:\s+(.+)$", output, re.MULTILINE)
    if native:
        facts["abis"] = re.findall(r"'([^']+)'", native.group(1))
    return facts


def zip_facts(path: pathlib.Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        bad = archive.testzip()
    abis = sorted({match.group(1) for name in names if (match := re.search(r"(?:^|/)lib/([^/]+)/[^/]+\.so$", name))})
    dex_files = sorted(name for name in names if re.fullmatch(r"(?:.*/)?classes\d*\.dex", name))
    return {"zip_integrity": bad is None, "first_bad_entry": bad, "abis": abis, "dex_files": dex_files}


def inspect_apk(path: pathlib.Path, tools: dict[str, pathlib.Path | None]) -> tuple[dict[str, Any], list[str]]:
    checks: dict[str, Any] = {}
    validation_errors: list[str] = []
    try:
        facts = zip_facts(path)
        checks["zip"] = {"status": "passed" if facts["zip_integrity"] else "failed", **facts}
        if not facts["zip_integrity"]:
            validation_errors.append("ZIP integrity failed")
    except (OSError, zipfile.BadZipFile) as error:
        checks["zip"] = {"status": "failed", "error": str(error)}
        validation_errors.append("not a readable APK ZIP archive")

    apksigner = tools.get("apksigner")
    if apksigner:
        result = run_tool([str(apksigner), "verify", "--verbose", "--print-certs", str(path)])
        output = result.get("output") or ""
        result["verifies"] = result["status"] == "passed"
        schemes = {}
        for version in ("v1", "v2", "v3", "v3.1", "v4"):
            match = re.search(rf"Verified using {re.escape(version)} scheme[^:]*:\s*(true|false)", output, re.IGNORECASE)
            if match:
                schemes[version] = match.group(1).lower() == "true"
        result["signature_schemes"] = schemes
        cert = re.search(r"Signer #1 certificate SHA-256 digest:\s*(\S+)", output)
        if cert:
            result["signer_certificate_sha256"] = cert.group(1)
        checks["apksigner"] = result
        if result["status"] != "passed":
            validation_errors.append("APK signature verification failed")
    else:
        checks["apksigner"] = unavailable()

    zipalign = tools.get("zipalign")
    if zipalign:
        result = run_tool([str(zipalign), "-c", "-v", "4", str(path)])
        result["aligned_4_bytes"] = result["status"] == "passed"
        checks["zipalign"] = result
        if result["status"] != "passed":
            validation_errors.append("APK zip alignment verification failed")
    else:
        checks["zipalign"] = unavailable()

    aapt = tools.get("aapt") or tools.get("aapt2")
    if aapt:
        result = run_tool([str(aapt), "dump", "badging", str(path)])
        result["facts"] = parse_badging(result.get("output") or "") if result["status"] == "passed" else {}
        checks["aapt_badging"] = result
    else:
        checks["aapt_badging"] = unavailable()

    apkanalyzer = tools.get("apkanalyzer")
    if apkanalyzer:
        analyzer_facts: dict[str, Any] = {}
        analyzer_runs: dict[str, Any] = {}
        commands = {
            "package_name": ["manifest", "application-id"],
            "version_name": ["manifest", "version-name"],
            "version_code": ["manifest", "version-code"],
            "min_sdk": ["manifest", "min-sdk"],
            "target_sdk": ["manifest", "target-sdk"],
        }
        for key, command in commands.items():
            result = run_tool([str(apkanalyzer), *command, str(path)])
            analyzer_runs[key] = {"status": result["status"], "exit_code": result["exit_code"]}
            if result["status"] == "passed":
                analyzer_facts[key] = (result.get("output") or "").strip()
        checks["apkanalyzer"] = {
            "status": "passed" if analyzer_facts else "failed",
            "facts": analyzer_facts,
            "commands": analyzer_runs,
        }
    else:
        checks["apkanalyzer"] = unavailable()
    return checks, validation_errors


def inspect_aab(path: pathlib.Path, tools: dict[str, pathlib.Path | None]) -> tuple[dict[str, Any], list[str]]:
    checks: dict[str, Any] = {}
    validation_errors: list[str] = []
    try:
        facts = zip_facts(path)
        checks["zip"] = {"status": "passed" if facts["zip_integrity"] else "failed", **facts}
        if not facts["zip_integrity"]:
            validation_errors.append("ZIP integrity failed")
    except (OSError, zipfile.BadZipFile) as error:
        checks["zip"] = {"status": "failed", "error": str(error)}
        validation_errors.append("not a readable AAB ZIP archive")
    jarsigner = tools.get("jarsigner")
    if jarsigner:
        result = run_tool([str(jarsigner), "-verify", "-strict", str(path)])
        output = (result.get("output") or "").lower()
        verifies = result["status"] == "passed" and "jar verified" in output and "jar is unsigned" not in output
        if not verifies:
            result["status"] = "failed"
        result["verifies"] = verifies
        checks["jarsigner"] = result
        if result["status"] != "passed":
            validation_errors.append("AAB JAR signature verification failed")
    else:
        checks["jarsigner"] = unavailable()
    checks["apksigner"] = {"status": "not_applicable", "reason": "apksigner verifies APK files, not app bundles"}
    checks["zipalign"] = {"status": "not_applicable", "reason": "zipalign is an APK check"}
    return checks, validation_errors


def tool_inventory(project: pathlib.Path) -> dict[str, pathlib.Path | None]:
    return {
        "apksigner": find_tool("apksigner", project),
        "zipalign": find_tool("zipalign", project),
        "aapt": find_tool("aapt", project),
        "aapt2": find_tool("aapt2", project),
        "apkanalyzer": find_tool("apkanalyzer", project),
        "jarsigner": find_tool("jarsigner", project),
    }


def write_reports(report: dict[str, Any], json_path: pathlib.Path, markdown_path: pathlib.Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    lines = [
        "# Android artifact verification", "",
        f"- Status: **{report['status']}**",
        f"- Requested variant: `{report['requested_variant']}`",
        f"- Current-run boundary: `{report.get('boundary_utc') or 'unavailable'}`",
        f"- Discovery mode: `{report.get('discovery_mode') or 'not run'}`",
        "",
        "| Type | Variant | Size | SHA-256 | Signature | Alignment | Staged path |",
        "| --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for item in report["artifacts"]:
        checks = item["checks"]
        signature = checks.get("apksigner", checks.get("jarsigner", {})).get("status", "n/a")
        alignment = checks.get("zipalign", {}).get("status", "n/a")
        lines.append(
            f"| {item['type'].upper()} | {item['variant'] or 'unknown'} | {item['size_bytes']} | "
            f"`{item['sha256']}` | {signature} | {alignment} | `{item['staged_path']}` |"
        )
    if not report["artifacts"]:
        lines.append("| — | — | — | — | — | — | no current artifact |")
    if report["errors"]:
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {error}" for error in report["errors"])
    lines.extend(["", "Unavailable Android inspection tools are reported as unavailable; they are never reported as passed.", ""])
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()
    project = pathlib.Path(args.project).resolve()
    artifact_dir = pathlib.Path(args.artifact_dir).resolve()
    report_dir = pathlib.Path(args.report_dir).resolve()
    json_path = pathlib.Path(args.json_report).resolve() if args.json_report else report_dir / "artifact-verification.json"
    markdown_path = pathlib.Path(args.markdown_report).resolve() if args.markdown_report else report_dir / "artifact-verification.md"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    boundary_ns, boundary_source = resolve_boundary(args, report_dir)
    candidates: list[pathlib.Path] = []
    discovery_mode: str | None = None
    if not project.is_dir():
        errors.append(f"Project directory does not exist: {project}")
    elif boundary_ns is None:
        errors.append("No current-run timestamp boundary is available; stale artifacts will not be accepted.")
    else:
        candidates, discovery_mode = read_candidates(project, report_dir, args.current_list, boundary_ns)

    requested_variants = [args.variant] if args.variant != "both" else ["debug", "release"]
    selected_candidates = [
        path for path in candidates
        if args.variant == "both" or artifact_variant(path) in {args.variant, None}
    ]
    coverage: dict[str, dict[str, int]] = {
        variant: {
            "apk": sum(path.suffix.lower() == ".apk" and artifact_variant(path) == variant for path in selected_candidates),
            "aab": sum(path.suffix.lower() == ".aab" and artifact_variant(path) == variant for path in selected_candidates),
        }
        for variant in requested_variants
    }
    for variant in requested_variants:
        if args.require_apk and coverage[variant]["apk"] == 0:
            errors.append(f"No current {variant} APK was found.")
        if args.require_aab and coverage[variant]["aab"] == 0:
            errors.append(f"No current {variant} AAB was found.")

    tools = tool_inventory(project)
    inspected: list[dict[str, Any]] = []
    invalid = False
    for source in selected_candidates:
        relative = source.relative_to(project)
        destination = artifact_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        kind = source.suffix.lower().lstrip(".")
        if kind == "apk":
            checks, validation_errors = inspect_apk(destination, tools)
        else:
            checks, validation_errors = inspect_aab(destination, tools)
        if validation_errors:
            invalid = True
            errors.extend(f"{relative}: {message}" for message in validation_errors)
        stat = destination.stat()
        inspected.append({
            "type": kind,
            "variant": artifact_variant(source),
            "source_path": str(source),
            "source_relative_path": str(relative),
            "staged_path": str(destination),
            "size_bytes": stat.st_size,
            "sha256": sha256(destination),
            "modified_utc": utc_iso(source.stat().st_mtime),
            "checks": checks,
        })

    missing = any(message.startswith("No current") for message in errors) or boundary_ns is None or not project.is_dir()
    exit_code = EXIT_MISSING if missing else EXIT_INVALID if invalid else EXIT_OK
    report = {
        "schema_version": 1,
        "status": "success" if exit_code == EXIT_OK else "missing" if exit_code == EXIT_MISSING else "invalid",
        "exit_code": exit_code,
        "project": str(project),
        "requested_variant": args.variant,
        "required": {"apk": args.require_apk, "aab": args.require_aab},
        "boundary_epoch_ns": boundary_ns,
        "boundary_utc": utc_iso(boundary_ns / 1_000_000_000) if boundary_ns is not None else None,
        "boundary_source": boundary_source,
        "discovery_mode": discovery_mode,
        "coverage": coverage,
        "tools": {name: str(path) if path else None for name, path in tools.items()},
        "artifacts": inspected,
        "errors": errors,
    }
    write_reports(report, json_path, markdown_path)
    print(f"Artifact verification report: {json_path}")
    for item in inspected:
        print(f"{item['type'].upper()} {item['variant'] or 'unknown'} {item['size_bytes']} bytes {item['sha256']} {item['staged_path']}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Artifact verification internal failure: {error}", file=sys.stderr)
        raise SystemExit(EXIT_INTERNAL)
