# sprocket

The user manual lives in `docs/manual.txt`.

It carries BFS attributes that the packaging step reads — `Bench:owner`,
`Bench:revision` and the usual `BEOS:TYPE`. They are part of the file just as
much as its bytes are, and an editor that drops them silently breaks the build.
