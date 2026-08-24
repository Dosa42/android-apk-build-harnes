from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / ".github" / "actions" / "android-build-harness" / "scripts"
DOCTOR = SCRIPTS / "project-doctor.py"
RUN_BUILD = SCRIPTS / "run-build.sh"
VERIFY = SCRIPTS / "verify-artifacts.py"
DEX_AUDIT = SCRIPTS / "dex-audit.py"


def write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")


def run(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )


def android_project(project: Path, *, agp: str = "9.1.1", gradle: str = "9.3.1") -> None:
    write(
        project / "settings.gradle.kts",
        """
        pluginManagement { repositories { google(); mavenCentral(); gradlePluginPortal() } }
        dependencyResolutionManagement { repositories { google(); mavenCentral() } }
        rootProject.name = "fixture"
        include(":app")
        """,
    )
    write(
        project / "build.gradle.kts",
        """
        plugins { alias(libs.plugins.android.application) apply false }
        """,
    )
    write(
        project / "app" / "build.gradle.kts",
        """
        plugins { alias(libs.plugins.android.application) }
        android {
          namespace = "example.fixture"
          compileSdk { version = release(36) { minorApiLevel = 1 } }
          defaultConfig { applicationId = "example.fixture"; minSdk = 24; targetSdk = 36 }
        }
        """,
    )
    write(
        project / "gradle" / "libs.versions.toml",
        f"""
        [versions]
        agp = "{agp}"
        [plugins]
        android-application = {{ id = "com.android.application", version.ref = "agp" }}
        """,
    )
    write(
        project / "gradle" / "wrapper" / "gradle-wrapper.properties",
        f"distributionUrl=https\\://services.gradle.org/distributions/gradle-{gradle}-bin.zip\n",
    )
    write(project / "gradle" / "wrapper" / "gradle-wrapper.jar", b"fixture")
    write(project / "gradlew", "#!/usr/bin/env bash\nexit 0\n")


def make_apk(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("AndroidManifest.xml", b"fixture-manifest")
        archive.writestr("classes.dex", b"dex\n035\x00fixture")


class ProjectDoctorTests(unittest.TestCase):
    def test_resolves_real_style_agp_9_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "target"
            android_project(project)
            report = base / "out" / "environment.json"
            result = run([sys.executable, str(DOCTOR), "--project", str(project), "--report", str(report)])
            self.assertEqual(result.returncode, 0, result.stdout)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["gradle"]["version"], "9.3.1")
            self.assertEqual(data["android_gradle_plugin"]["version"], "9.1.1")
            self.assertEqual(data["java"]["version"], 17)
            self.assertEqual(data["android_sdk"]["compile_sdks"], ["36.1"])
            self.assertEqual(data["selected_modules"], [":app"])
            self.assertEqual(
                data["android_sdk"]["packages"],
                ["build-tools;36.0.0", "platforms;android-36.1"],
            )

    def test_rejects_incompatible_wrapper_without_rewriting_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "target"
            android_project(project, gradle="9.2.1")
            before = hashlib.sha256((project / "gradle/wrapper/gradle-wrapper.properties").read_bytes()).hexdigest()
            report = base / "out" / "environment.json"
            result = run([sys.executable, str(DOCTOR), "--project", str(project), "--report", str(report)])
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn("requires Gradle >= 9.3.1", result.stdout)
            after = hashlib.sha256((project / "gradle/wrapper/gradle-wrapper.properties").read_bytes()).hexdigest()
            self.assertEqual(before, after)

    def test_task_discovery_can_use_a_trusted_pinned_gradle_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "target"
            android_project(project)
            write(project / "gradle/wrapper/gradle-wrapper.jar", b"corrupted wrapper bytes")
            trusted_gradle = base / "tools" / "gradle"
            write(
                trusted_gradle,
                """
                #!/usr/bin/env bash
                printf '%s\n' \
                  ':app:assembleDebug - Assemble debug APK' \
                  ':app:assembleRelease - Assemble release APK' \
                  ':app:lintDebug - Run debug lint' \
                  ':app:testDebugUnitTest - Run debug tests'
                """,
            )
            trusted_gradle.chmod(0o755)
            report = base / "out" / "environment.json"
            tasks = base / "out" / "gradle-tasks.txt"
            result = run(
                [
                    sys.executable,
                    str(DOCTOR),
                    "--project",
                    str(project),
                    "--report",
                    str(report),
                    "--tasks-file",
                    str(tasks),
                    "--gradle-command",
                    str(trusted_gradle),
                    "--resolve-tasks",
                ]
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["variants"], ["debug", "release"])
            self.assertEqual(data["task_discovery"]["command"][0], str(trusted_gradle))
            self.assertFalse(data["gradle"]["wrapper_jar_executed"])


class ArtifactVerificationTests(unittest.TestCase):
    def verifier_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.pop("ANDROID_HOME", None)
        env.pop("ANDROID_SDK_ROOT", None)
        env["PATH"] = "/usr/bin:/bin"
        return env

    def test_accepts_only_a_current_valid_apk_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "target"
            report_dir = base / "out" / "reports"
            artifact_dir = base / "out" / "artifacts"
            report_dir.mkdir(parents=True)
            boundary = time.time_ns()
            (report_dir / "build-start.epoch-ns").write_text(str(boundary), encoding="utf-8")
            apk = project / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
            make_apk(apk)
            os.utime(apk, ns=(boundary + 10_000_000, boundary + 10_000_000))
            result = run(
                [
                    sys.executable,
                    str(VERIFY),
                    "--project",
                    str(project),
                    "--variant",
                    "debug",
                    "--artifact-dir",
                    str(artifact_dir),
                    "--report-dir",
                    str(report_dir),
                    "--require-apk",
                ],
                env=self.verifier_env(),
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            data = json.loads((report_dir / "artifact-verification.json").read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "success")
            self.assertEqual(len(data["artifacts"]), 1)
            self.assertTrue(data["artifacts"][0]["checks"]["zip"]["zip_integrity"])
            self.assertEqual(data["artifacts"][0]["checks"]["apksigner"]["status"], "unavailable")

    def test_rejects_a_stale_apk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "target"
            report_dir = base / "out" / "reports"
            artifact_dir = base / "out" / "artifacts"
            apk = project / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
            make_apk(apk)
            report_dir.mkdir(parents=True)
            boundary = time.time_ns() + 20_000_000
            (report_dir / "build-start.epoch-ns").write_text(str(boundary), encoding="utf-8")
            result = run(
                [
                    sys.executable,
                    str(VERIFY),
                    "--project",
                    str(project),
                    "--variant",
                    "debug",
                    "--artifact-dir",
                    str(artifact_dir),
                    "--report-dir",
                    str(report_dir),
                    "--require-apk",
                ],
                env=self.verifier_env(),
            )
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn("No current debug APK", result.stdout)


class DexAuditTests(unittest.TestCase):
    def manifest(self, *, compatible: bool) -> str:
        if compatible:
            resizable = "true"
            orientation = ""
            touchscreen = "false"
        else:
            resizable = "false"
            orientation = ' android:screenOrientation="portrait"'
            touchscreen = "true"
        return f"""\
        <manifest xmlns:android="http://schemas.android.com/apk/res/android" package="example.fixture">
          <uses-sdk android:minSdkVersion="24" android:targetSdkVersion="36" />
          <uses-feature android:name="android.hardware.touchscreen" android:required="{touchscreen}" />
          <application android:resizeableActivity="{resizable}">
            <activity android:name=".MainActivity"{orientation} />
          </application>
        </manifest>
        """

    def test_pass_and_failure_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "target"
            project.mkdir()
            for compatible, expected in ((True, 0), (False, 1)):
                manifest = base / f"merged-{compatible}.xml"
                write(manifest, self.manifest(compatible=compatible))
                report = base / f"dex-{compatible}.json"
                result = run(
                    [
                        sys.executable,
                        str(DEX_AUDIT),
                        "--project",
                        str(project),
                        "--variant",
                        "debug",
                        "--merged-manifest",
                        str(manifest),
                        "--report",
                        str(report),
                    ]
                )
                self.assertEqual(result.returncode, expected, result.stdout)
                data = json.loads(report.read_text(encoding="utf-8"))
                self.assertFalse(data["runtime_verification"]["samsung_dex_session_tested"])
                self.assertEqual(data["status"], "manifest_checks_pass" if compatible else "failed")


class BuildRunnerTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("keytool"), "JDK keytool is required for the signing fixture")
    def test_debug_build_uses_external_generated_keystore(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "target"
            output = base / "out"
            write(
                project / "gradlew",
                r"""
                #!/usr/bin/env bash
                set -euo pipefail
                for argument in "$@"; do
                  case "$argument" in
                    tasks)
                      printf '%s\n' \
                        ':app:assembleDebug - Assemble debug APK' \
                        ':app:lintDebug - Run debug lint' \
                        ':app:testDebugUnitTest - Run debug tests'
                      exit 0
                      ;;
                    :app:assembleDebug)
                      properties="${GRADLE_USER_HOME}/gradle.properties"
                      grep -q '^android.injected.signing.store.file=' "$properties"
                      keystore="$(sed -n 's/^android.injected.signing.store.file=//p' "$properties")"
                      test -f "$keystore"
                      test ! -e "${PWD}/debug.keystore"
                      python3 - "${PWD}/app/build/outputs/apk/debug/app-debug.apk" <<'PY'
                import pathlib, sys, zipfile
                target = pathlib.Path(sys.argv[1])
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(target, "w") as archive:
                    archive.writestr("AndroidManifest.xml", b"fixture")
                    archive.writestr("classes.dex", b"dex fixture")
                PY
                      exit 0
                      ;;
                  esac
                done
                exit 0
                """,
            )
            (project / "gradlew").chmod(0o755)
            result = run(
                [
                    "bash",
                    str(RUN_BUILD),
                    "--project",
                    str(project),
                    "--variant",
                    "debug",
                    "--output-dir",
                    str(output),
                    "--skip-tests",
                    "--skip-lint",
                ],
                env={**os.environ, "HARNESS_GRADLE_COMMAND": str(project / "gradlew")},
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertFalse((project / "debug.keystore").exists())
            summary = json.loads((output / "reports" / "build-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "success")
            self.assertEqual(summary["debug_signing_preflight"], "generated_ready")

    @unittest.skipUnless(shutil.which("keytool"), "JDK keytool is required for the signing fixture")
    def test_failed_unit_test_does_not_hide_successful_apk_packaging(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "target"
            output = base / "out"
            trusted_gradle = base / "tools" / "gradle"
            project.mkdir()
            write(
                trusted_gradle,
                r"""
                #!/usr/bin/env bash
                set -euo pipefail
                for argument in "$@"; do
                  case "$argument" in
                    tasks)
                      printf '%s\n' \
                        ':app:assembleDebug - Assemble debug APK' \
                        ':app:testDebugUnitTest - Run debug tests'
                      exit 0
                      ;;
                    :app:testDebugUnitTest)
                      echo 'fixture unit-test compilation failed'
                      exit 9
                      ;;
                    :app:assembleDebug)
                      python3 - "${PWD}/app/build/outputs/apk/debug/app-debug.apk" <<'PY'
                import pathlib, sys, zipfile
                target = pathlib.Path(sys.argv[1])
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(target, "w") as archive:
                    archive.writestr("AndroidManifest.xml", b"fixture")
                    archive.writestr("classes.dex", b"dex fixture")
                PY
                      exit 0
                      ;;
                  esac
                done
                exit 0
                """,
            )
            trusted_gradle.chmod(0o755)
            result = run(
                [
                    "bash",
                    str(RUN_BUILD),
                    "--project",
                    str(project),
                    "--variant",
                    "debug",
                    "--output-dir",
                    str(output),
                    "--run-tests",
                    "--skip-lint",
                ],
                env={**os.environ, "HARNESS_GRADLE_COMMAND": str(trusted_gradle)},
            )
            self.assertEqual(result.returncode, 1, result.stdout)
            apk = project / "app/build/outputs/apk/debug/app-debug.apk"
            self.assertTrue(apk.is_file())
            summary = json.loads((output / "reports/build-summary.json").read_text(encoding="utf-8"))
            results = {(item["phase"], item["status"]) for item in summary["task_results"]}
            self.assertIn(("test", "failed"), results)
            self.assertIn(("assemble", "success"), results)
            self.assertEqual(summary["status"], "failed")


if __name__ == "__main__":
    unittest.main()
