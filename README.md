# Android APK Build Harness

[![Build Android APK](https://github.com/Dosa42/android-apk-build-harnes/actions/workflows/build-apk.yml/badge.svg)](https://github.com/Dosa42/android-apk-build-harnes/actions/workflows/build-apk.yml)

Een zelfstandige GitHub Actions-harness die een Android-repository uitleest, de vastgepinde Gradle/AGP/JDK/Android-SDK-combinatie bepaalt en daarna APK's bouwt en controleert. De target-repository wordt apart en zonder blijvende Git-credentials uitgecheckt; de harness commit of pusht daar nooit naartoe.

## Snelste gebruik

1. Open **Actions** > **Build Android APK** > **Run workflow**.
2. Laat de standaardwaarden staan voor een debugbuild van `Dosa42/Apk-builder-app`, of vul een andere `owner/repository` in. Unit-tests staan standaard uit; lint en DeX-audit staan aan.
3. Klik **Run workflow**.
4. Download na afloop het `*-apks`-artifact onderaan de workflow-run. Het `*-reports-and-logs`-artifact bevat de diagnose, test-, lint-, signing-, artifact- en DeX-rapporten.

De standaarddebugbuild heeft geen signingsecret nodig. De harness maakt een tijdelijke standaard-debugkeystore buiten de target-checkout en verwijdert die na de run.

Tests zijn bewust een aparte schakelaar: een app met verouderde of kapotte testbron kan zo nog steeds een APK opleveren. Als je **Run tests** inschakelt, worden testfouten wel als workflowfout gerapporteerd, maar assemble, artifactcontrole en upload blijven doorgaan.

## Wat automatisch wordt bepaald

- de Gradle-versie uit de Gradle Wrapper van het project;
- de Android Gradle Plugin-versie uit een version catalog, pluginblok of buildscript-classpath;
- de bijpassende JDK op basis van een expliciete AGP-compatibiliteitsmatrix;
- alle `com.android.application`-modules;
- `compileSdk`, Build Tools en expliciet gebruikte NDK/CMake-versies;
- echte assemble-, test-, lint- en bundletaken uit `./gradlew tasks --all`;
- debug-, release- en flavored varianten zonder taaknamen te gokken;
- Maven alleen wanneer het project werkelijk een `pom.xml` bevat.

De harness gebruikt altijd de versiepin uit de Wrapper-properties van de target. Op GitHub wordt exact die officiële Gradle-distributie geïnstalleerd en gecontroleerd; target-owned Wrapper-JAR-code wordt daar niet uitgevoerd. Daardoor kan ook een beschadigde Wrapper-JAR veilig worden omzeild zonder Gradle, AGP of bronbestanden stilzwijgend bij te werken. Een onbekende of aantoonbaar incompatibele combinatie geeft een gerichte fout met een diagnoserapport.

## Release-APK ondertekenen

Maak in **Settings** > **Secrets and variables** > **Actions** deze repositorysecrets:

| Secret | Betekenis |
| --- | --- |
| `ANDROID_RELEASE_KEYSTORE_BASE64` | De volledige keystore als base64 |
| `ANDROID_RELEASE_STORE_PASSWORD` | Wachtwoord van de keystore |
| `ANDROID_RELEASE_KEY_PASSWORD` | Wachtwoord van de sleutel |
| `ANDROID_RELEASE_KEY_ALIAS` | Alias; standaardfallback is `upload` |

Kies daarna `release` of `both` in **Run workflow**. Ontbrekende of ongeldige signinggegevens blokkeren een releasebuild vóór packaging. Geheimen worden niet in Gradle-commandoregels geplaatst en bekende secretwaarden worden uit bewaarde logs verwijderd.

Voor een private target-repository kan optioneel `TARGET_REPO_TOKEN` worden toegevoegd met alleen leesrechten op die repository. Publieke targets hebben dit niet nodig.

## Samsung DeX en Samsung SDK's

Een normale Android-app heeft geen algemene “Samsung DeX SDK-library” nodig om in DeX te draaien. De harness voegt daarom geen willekeurige Samsung- of Knox-dependency aan een app toe. Als een app werkelijk een Samsung SDK gebruikt, moet die dependency en de bijbehorende Maven-repository in de Gradle-configuratie van die app staan; de Wrapper haalt hem dan normaal op.

De ingebouwde DeX-audit controleert het uiteindelijke merged manifest op:

- `targetSdkVersion >= 24`;
- effectieve `resizeableActivity`-waarden;
- vaste schermoriëntaties;
- verplicht touchscreengebruik.

Dit is een statische compatibiliteitscontrole. Responsief gedrag, toetsenbord/muis en echte DeX-uitvoering kunnen alleen betrouwbaar worden bevestigd op een DeX-capabel Samsung-toestel. Een gewone Android-emulator wordt nooit als een geslaagde Samsung DeX-runtime-test gerapporteerd.

## Als herbruikbare workflow gebruiken

Een andere workflow kan de harness zo aanroepen:

```yaml
jobs:
  apk:
    permissions:
      contents: read
    uses: Dosa42/android-apk-build-harnes/.github/workflows/reusable-android-harness.yml@main
    with:
      target_repository: owner/android-app
      target_ref: main
      variant: debug
      dex_audit: true
      run_tests: false
      run_lint: true
    secrets: inherit
```

De herbruikbare workflow uploadt APK's en diagnostiek en exposeert de artifactnamen plus de paden van het artifactmanifest en DeX-rapport.

## Bewijs en foutdiagnose

Elke run bewaart onder `android-harness-output`:

- `artifacts/`: alleen APK/AAB-bestanden die aantoonbaar in de huidige run zijn gemaakt;
- `reports/`: gedetecteerde omgeving, exacte SDK-packages, gekozen taken, buildstatus, SHA-256-hashes, signing/alignment en DeX-resultaat;
- `logs/`: afzonderlijke Gradle-logs per taak.

Een oude APK uit een cache of eerdere build wordt niet als nieuw resultaat geaccepteerd. De herhaalbare succes- en foutfixtures staan in `tests/`; een lokaal commandolog met machinepaden wordt bewust niet openbaar gecommit.

Bij wijzigingen aan de harness op `main` draait na de bron- en regressietests automatisch een parallelle echte debug-smokebuild van `Dosa42/Apk-builder-app` en `Dosa42/voice-to-melodiSHEET`. Zo worden niet alleen de YAML, maar ook beide volledige Android-buildketens bewaakt.

## Lokale broncontrole

```bash
python3 -m unittest discover -s tests -v
bash -n .github/actions/android-build-harness/scripts/*.sh
python3 -m compileall -q .github/actions/android-build-harness/scripts tests
```

De uitgevoerde verificaties voor de gepubliceerde versie staan in [VERIFICATION.md](VERIFICATION.md).
