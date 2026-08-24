# Verificatie

Datum: 24 augustus 2026

## Publieke regressieset

`python3 -m unittest discover -s tests -v` slaagt voor alle negen scenario's:

- AGP 9.1.1, Gradle 9.3.1, JDK 17 en compileSdk 36.1 correct detecteren;
- een incompatibele Wrapper weigeren zonder de target te herschrijven;
- echte taken via de officiële, exact vastgepinde Gradle-distributie ontdekken zonder een beschadigde target-Wrapper-JAR uit te voeren;
- alleen een geldige APK uit de huidige run accepteren;
- een oude APK uit een eerdere run weigeren;
- DeX-manifestpass en definitieve DeX-manifestfailure onderscheiden;
- een tijdelijke debugkeystore buiten de target genereren en injecteren.
- na een falende unit-test toch onafhankelijk een APK packagen en de kwaliteitsfout behouden.
- uitsluitend geverifieerde staging-APK/AAB-bestanden uploaden, zonder een dubbele kopie uit de target-buildmap.

Daarnaast slagen Bash-syntaxcontrole, Python-compilatie, YAML-parsing en `actionlint 1.7.12` voor de twee workflows.

## Eerste echte target

De statische projectdoctor is uitgevoerd op `Dosa42/Apk-builder-app` commit `80f14a2a7a74567b8e8228ccded28953316b8909` en bepaalde:

| Onderdeel | Gedetecteerd |
| --- | --- |
| Gradle Wrapper | 9.3.1 |
| Android Gradle Plugin | 9.1.1 |
| JDK | 17 |
| compileSdk | 36.1 |
| Build Tools | 36.0.0 |
| Appmodule | `:app` |

De tracked targetbestanden bleven daarbij ongewijzigd. De eerste runnerpoging toonde bovendien dat beide target-Wrapper-JAR-kopieën bytebeschadigd zijn. De harness gebruikt daarom veilig de officiële Gradle 9.3.1-distributie die uit de Wrapper-properties is afgeleid en verifieert die versie vóór taakdetectie. Een volgende run bouwde een debug-APK-artifact van circa 22,5 MB; alleen de optionele target-unit-test faalde doordat `GreetingScreenshotTest.kt` nog naar een niet-bestaande `Greeting` verwijst. De standaard packaging-run laat tests daarom uit, terwijl lint en DeX-audit actief blijven. Het actuele resultaat en de APK zijn zichtbaar bij **Actions**.

## DeX-bewijsgrens

De geautomatiseerde controle is een audit van het uiteindelijke merged manifest. Er wordt geen emulator als Samsung DeX bestempeld. Een echte runtimepass vereist een DeX-capabel Samsung-toestel.
