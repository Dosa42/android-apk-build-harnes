# Verificatie

Datum: 24 augustus 2026

## Publieke regressieset

`python3 -m unittest discover -s tests -v` slaagt voor alle zes scenario's:

- AGP 9.1.1, Gradle 9.3.1, JDK 17 en compileSdk 36.1 correct detecteren;
- een incompatibele Wrapper weigeren zonder de target te herschrijven;
- alleen een geldige APK uit de huidige run accepteren;
- een oude APK uit een eerdere run weigeren;
- DeX-manifestpass en definitieve DeX-manifestfailure onderscheiden;
- een tijdelijke debugkeystore buiten de target genereren en injecteren.

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

De tracked targetbestanden bleven daarbij ongewijzigd. De workflow op `main` voert na publicatie ook een echte debug-smokebuild van deze target uit; het resultaat en de APK zijn zichtbaar bij **Actions**.

## DeX-bewijsgrens

De geautomatiseerde controle is een audit van het uiteindelijke merged manifest. Er wordt geen emulator als Samsung DeX bestempeld. Een echte runtimepass vereist een DeX-capabel Samsung-toestel.

