# series

Small numeric helpers for evenly sampled time series.

- `series.io` — the one-number-per-line file format
- `series.windows` — sliding-window reductions
- `series.normalize` — rescaling

Every window function is documented to reduce over exactly `size` consecutive
values, and to return `len(values) - size + 1` results.
