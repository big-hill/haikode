# clampdemo

A two-file C program.

```sh
make        # builds build/app
make run    # builds and runs it
make clean
```

`build/app` is expected to print `clamped=7`: the raw reading is 12 and the
display cannot show more than `UPPER_BOUND`.
