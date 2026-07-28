# microrouter

A dependency-free request router.

- `server.py`   the dispatcher and the `Response` type
- `routes.py`   the route table
- `handlers/`   one module per endpoint

```sh
python3 -m unittest discover -q
```

There is no rate limiting anywhere in the codebase yet.
