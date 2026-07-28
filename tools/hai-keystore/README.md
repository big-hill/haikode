# hai-keystore

Liten native Haiku-CLI som lagrer API-nøkler i Haikus innebygde nøkkelring
(`BKeyStore`/`BPasswordKey` — samme mekanisme som WebPositive bruker for
passord). Brukes av Python-CLI-en `haikode` via subprocess.

> **Binærnavnet forblir `hai-keystore` — ikke døp det om.**
> Haiku knytter nøkkelring-godkjenningen til app-signatur **og** sti til
> binæren. Et nytt navn (eller ny signatur) ville gjort den eksisterende
> godkjenningen ugyldig, re-trigget «Application keyring access»-dialogen på
> maskinens *fysiske* skjerm, og gjort allerede lagrede nøkler (bl.a.
> Ollama-nøkkelen) utilgjengelige. Kun *identifier-navnerommet* ble endret ved
> omdøpingen til `haikode`, fordi det er data vi kan migrere i programvare.

Nøklene lagres i standard-nøkkelringen som `B_KEY_TYPE_PASSWORD` med
`B_KEY_PURPOSE_GENERIC` og `secondaryIdentifier = "hai"`, slik at `list`
kun viser nøkler denne CLI-en selv har lagret.

## Bygging (på Haiku)

```sh
make            # g++ -O2 -Wall -o hai-keystore main.cpp -lbe
make install    # kopierer til ~/config/non-packaged/bin/ (i PATH)
```

## Bruk

```sh
printf '%s' <secret> | hai-keystore set-stdin <identifier>   # lagre/oppdater
hai-keystore get <identifier>            # skriver hemmeligheten til stdout + \n
hai-keystore remove <identifier>         # fjern nøkkel
hai-keystore list                        # alle identifiers (én per linje)
hai-keystore set <identifier> <secret>   # UTFASET, se «Sikkerhet» under
```

Identifier er en opak streng; `haikode` bruker konvensjonen
`haikode:<leverandør>`, f.eks. `haikode:xai`, `haikode:anthropic`.

Nøkler lagret før omdøpingen ligger under `hai:<leverandør>`.
`haikode/config.py` leser den gamle identifieren automatisk hvis den nye ikke
finnes, og skriver da en kopi under den nye — uten å slette den gamle. Ingen
manuell migrering trengs, og ingen ny godkjenningsdialog dukker opp siden
binæren er den samme.

Exit-koder:

| Kode | Betydning |
|------|-----------|
| 0    | Suksess |
| 1    | Nøkkel ikke funnet (`get`/`remove`; ingenting på stdout, melding på stderr) |
| 2    | Feil bruk (usage på stderr) |
| 3    | Keystore-feil eller timeout (se nedenfor) |

## Kjente begrensninger

- **GUI-godkjenningsdialog (verifisert på Haiku hrev57937):** Første gang
  programmet aksesserer nøkkelringen viser `keystore_server` en dialog på
  maskinens *fysiske* skjerm:

  > **Application keyring access**
  > The application: `application/x-vnd.hai-keystore (<sti til binæren>)`
  > requests access to keyring: Master
  > to perform the following action: Get keys from the keyring.
  > This application hasn't been granted access before.
  > [Disallow] [Allow once] [Allow always]

  Kjøres kommandoen headless (f.eks. over SSH) henger den til dialogen
  besvares — derfor har binæren en innebygd `alarm()`-timeout på 10 sekunder
  som avbryter med exit-kode 3 og en feilmelding på stderr. Engangsfix: kjør
  `hai-keystore list` én gang, gå til skjermen og velg **Allow always**.
  Deretter fungerer alt headless.
- **Krever BApplication/registrar:** `keystore_server` identifiserer
  klienter via registrar; uten en registrert `BApplication` svarer serveren
  `B_BAD_TEAM_ID` («Operation on invalid team»). Derfor oppretter `main()` en
  `BApplication` med signaturen `application/x-vnd.hai-keystore`. Signaturen
  beholdes uendret av samme grunn som binærnavnet — se boksen øverst.
- **Godkjenning er bundet til signatur + sti:** Dialogen viser både
  app-signaturen og stien til binæren. Godkjenn derfor fra den *installerte*
  binæren (`~/config/non-packaged/bin/hai-keystore`), ikke fra byggkatalogen.
  Rebygging/reinstallering kan re-trigge dialogen.
- **Låst nøkkelring:** Hvis standard-nøkkelringen er låst med passord, må den
  låses opp (via GUI) før kommandoene fungerer.
## Sikkerhet: hemmeligheten skal aldri i argv

`argv` er lesbart for alle brukere på maskinen (`ps`), så `set <identifier>
<secret>` lekket hver eneste nøkkel den lagret. Bruk `set-stdin`, som leser
hemmeligheten fra stdin (én avsluttende `\n` strippes).

`set` er beholdt én utgivelse til fordi brukere kan ha skript som kaller den.
Den advarer på stderr og nuller ut `argv[3]` så snart nøkkelen er lagret —
det forkorter vinduet, men lukker det ikke.

`haikode/config.py` bruker kun `set-stdin`. Møter den en eldre binær som ikke
kjenner verbet (exit 2), faller den *ikke* tilbake til `set`; den advarer og
lagrer nøkkelen i konfigfila (modus 0600) i stedet. Rebygg og reinstaller
binæren for å få nøkkelringen tilbake i bruk.

## Filer

- `main.cpp` — hele implementasjonen
- `Makefile` — bygg med `make`
- Master-kopi ligger på Mac-en i `~/hai/tools/hai-keystore/` (repo-katalogen
  heter fortsatt `hai`); bygges på Haiku-maskinen (`shredder`), der kilden
  installeres til `/boot/home/haikode/tools/hai-keystore/`.
