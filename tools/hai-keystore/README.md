# hai-keystore

A small native Haiku CLI that stores API keys in Haiku's own keyring
(`BKeyStore`/`BPasswordKey` — the same mechanism WebPositive uses for
passwords). The Python CLI `haikode` calls it through a subprocess.

> **The binary is named `hai-keystore` and must stay that way.**
> Haiku ties keyring approval to the application signature **and** the path to
> the binary. A new name (or a new signature) would invalidate the existing
> grant, re-trigger the "Application keyring access" dialog on the machine's
> *physical* screen, and orphan every key already stored. Only the
> *identifier namespace* changed when the project was renamed to `haikode`,
> because that is data software can migrate.

Keys are stored in the default keyring as `B_KEY_TYPE_PASSWORD` with
`B_KEY_PURPOSE_GENERIC` and `secondaryIdentifier = "hai"`, so `list` shows
only the keys this CLI stored itself.

## Building (on Haiku)

```sh
make            # g++ -O2 -Wall -o hai-keystore main.cpp -lbe
make install    # copies to ~/config/non-packaged/bin/ (on PATH)
```

## Use

```sh
printf '%s' <secret> | hai-keystore set-stdin <identifier>   # store/update
hai-keystore get <identifier>            # writes the secret to stdout + \n
hai-keystore remove <identifier>         # remove a key
hai-keystore list                        # every identifier, one per line
hai-keystore set <identifier> <secret>   # DEPRECATED, see "Security" below
```

The identifier is an opaque string; `haikode` uses the convention
`haikode:<provider>`, for example `haikode:xai`, `haikode:anthropic`.

Keys stored before the rename live under `hai:<provider>`.
`haikode/config.py` reads the old identifier automatically when the new one is
absent, and writes a copy under the new one without deleting the old. No
manual migration is needed, and no new approval dialog appears, because the
binary is unchanged.

Exit codes:

| Code | Meaning |
|------|---------|
| 0    | Success |
| 1    | Key not found (`get`/`remove`; nothing on stdout, message on stderr) |
| 2    | Bad usage (usage on stderr) |
| 3    | Keystore error or timeout (see below) |

## Known limitations

- **GUI approval dialog (verified on Haiku hrev57937):** the first time the
  program touches the keyring, `keystore_server` shows a dialog on the
  machine's *physical* screen:

  > **Application keyring access**
  > The application: `application/x-vnd.hai-keystore (<path to the binary>)`
  > requests access to keyring: Master
  > to perform the following action: Get keys from the keyring.
  > This application hasn't been granted access before.
  > [Disallow] [Allow once] [Allow always]

  Run headless — over SSH, say — the command hangs until the dialog is
  answered, which is why the binary carries a built-in `alarm()` timeout of
  10 seconds that aborts with exit code 3 and a message on stderr. One-time
  fix: run `hai-keystore list` once, go to the screen and choose **Allow
  always**. Everything works headless afterwards.
- **Requires BApplication/registrar:** `keystore_server` identifies clients
  through the registrar; without a registered `BApplication` the server
  answers `B_BAD_TEAM_ID` ("Operation on invalid team"). `main()` therefore
  creates a `BApplication` with the signature
  `application/x-vnd.hai-keystore`. The signature stays fixed for the same
  reason the binary name does — see the box at the top.
- **Approval is bound to signature + path:** the dialog shows both the
  application signature and the path to the binary, so approve from the
  *installed* binary (`~/config/non-packaged/bin/hai-keystore`), not from the
  build directory. Rebuilding or reinstalling can re-trigger the dialog.
- **Locked keyring:** if the default keyring is password-locked it must be
  unlocked through the GUI before any command works.

## Security: the secret must never reach argv

`argv` is readable by every user on the machine (`ps`), so
`set <identifier> <secret>` leaked every key it stored. Use `set-stdin`, which
reads the secret from stdin (one trailing `\n` is stripped).

`set` is kept for one more release because users may have scripts calling it.
It warns on stderr and zeroes `argv[3]` as soon as the key is stored — which
shortens the window without closing it.

`haikode/config.py` uses `set-stdin` only. Meeting an older binary that does
not know the verb (exit 2) it does *not* fall back to `set`; it warns and
stores the key in the config file (mode 0600) instead. Rebuild and reinstall
the binary to get the keyring back in use.

## Files

- `main.cpp` — the whole implementation
- `Makefile` — build with `make`
