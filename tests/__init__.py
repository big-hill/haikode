"""Test package.

Importing it redirects haikode's global state directory to a throwaway one.
`unittest discover` imports this package before any test module, so the
redirect is in place before the first SessionStore exists.

Without it the suite writes into the user's real store: a run on the Haiku
machine left 96 test sessions in a picker holding 125, because a test only
has to forget one patch to reach ~/config/settings/haikode. Per-test patching
of global_config_dir stays valid; this is the backstop under it.
"""

import atexit
import os
import shutil
import tempfile

if not os.environ.get("HAIKODE_CONFIG_DIR"):
    _sandbox = tempfile.mkdtemp(prefix="haikode-tests-")
    os.environ["HAIKODE_CONFIG_DIR"] = _sandbox
    atexit.register(shutil.rmtree, _sandbox, True)
