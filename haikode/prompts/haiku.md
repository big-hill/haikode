# Haiku OS
You are running natively on Haiku, a BeOS-compatible desktop OS. It is not Linux; many assumptions do not carry over.
- Package management is `pkgman` (search / install / full-sync), and packages are HPKG. Development headers come from `haiku_devel`.
- Native GUI applications use the BeAPI (BApplication, BWindow, BView, BLooper, BMessage) and link against `-lbe`; `BFilePanel` additionally needs `-ltracker`.
- The native build tool is `jam` (Jamfile); `make` and `cmake` also exist.
- Processes are called teams: `ps` lists them, `kill <team-id>` stops one.
- Paths: `/boot/system/bin`, `/boot/system/apps`, `/boot/system/develop/headers/be`, `/boot/home/config` for user settings and `/boot/home/config/non-packaged/bin` for user binaries.
- Do not launch GUI applications unless the user asks — they appear on the machine's physical screen, which the user may not be sitting at.
