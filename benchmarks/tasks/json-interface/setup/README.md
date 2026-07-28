# app

`app/version.py` holds the version. `tests/` is run with

    python3 -m unittest discover -s tests -t .

The release checklist says the version is bumped first and the suite is run
afterwards, never the other way round.
