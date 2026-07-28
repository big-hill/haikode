# clampdemo (jam)

The same two-file C program as `build-make`, built with `jam` — the build tool
Haiku itself uses.

```sh
jam
./app
```

`./app` is expected to print `clamped=7`: the raw reading is 12 and the display
cannot show more than `UPPER_BOUND`.
