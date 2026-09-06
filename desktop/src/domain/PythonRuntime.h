#ifndef HAI_PYTHON_RUNTIME_H
#define HAI_PYTHON_RUNTIME_H

// One launch contract for config/history and the conversation worker.
// Execute with /bin/sh -c SCRIPT haikode-runtime MODULE [ARGS...].
// No implicit developer checkout or inherited PYTHONPATH. Probe before exec:
// an installed interpreter need not have this package on its module path.
// Keep this portable shell program executable by tests/test_desktop_runtime.py.
static const char* const kPythonRuntimeScript =
    "export PYTHONPATH=\"${HAI_PYTHONPATH:-}\"\n"
    "cd / || exit 127\n"
    "try_python() {\n"
    "  runtime=$1; shift\n"
    "  if \"$runtime\" -c 'import sys, importlib; assert sys.version_info >= (3, 10); importlib.import_module(sys.argv[1])' \"$1\" >/dev/null 2>&1; then\n"
    "    exec \"$runtime\" -m \"$@\"\n"
    "  fi\n"
    "}\n"
    "try_python python3.10 \"$@\"\n"
    "try_python python3 \"$@\"\n"
    "remaining=\"${PATH}:\"\n"
    "while [ -n \"$remaining\" ]; do\n"
    "  directory=${remaining%%:*}; remaining=${remaining#*:}\n"
    "  case \"$directory\" in /*) ;; *) continue ;; esac\n"
    "  for candidate in \"$directory\"/python3.[0-9]*; do\n"
    "    [ -x \"$candidate\" ] || continue\n"
    "    try_python \"$candidate\" \"$@\"\n"
    "  done\n"
    "done\n"
    "echo 'haikode: no compatible Python 3.10+ with haikode installed; check the package installation or explicit HAI_PYTHONPATH' >&2\n"
    "exit 127\n"
;

#endif
