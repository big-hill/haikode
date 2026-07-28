"""
Tests for the Haiku desktop integration.

Nothing here executes a Haiku tool. The whole point of the module is that it
is a silent no-op off Haiku, so the on-Haiku behaviour is exercised by forcing
is_haiku() true and asserting the argv that would have been run. The real
flags were verified on a live hrev57937 machine; these tests lock them in.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from haikode import haiku
from haikode.haiku import (ALERT_TIMEOUT, COMMAND_TIMEOUT, QUERY_INDICES,
                           SESSION_ATTRS, Timestamp, alert, copy_attributes,
                           ensure_query_indices, get_attributes, is_haiku,
                           list_attributes, notify, open_in_tracker,
                           open_with_preferred, read_attribute,
                           session_attributes, set_attributes, tag_session_file)


class _Result:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Recorder:
    """Stands in for subprocess.run: records argv, never executes anything."""

    def __init__(self, replies=None, default=None):
        # replies maps the tool name (argv[0]) to a _Result or a callable.
        self.replies = replies or {}
        self.default = default if default is not None else _Result()
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        reply = self.replies.get(argv[0], self.default)
        return reply(argv) if callable(reply) else reply

    @property
    def argvs(self):
        return [argv for argv, _ in self.calls]

    def argv_for(self, tool):
        return [argv for argv in self.argvs if argv and argv[0] == tool]


class _Session:
    def __init__(self, **kwargs):
        self.title = kwargs.get("title", "")
        self.provider = kwargs.get("provider", "")
        self.model = kwargs.get("model", "")
        self.cwd = kwargs.get("cwd", "")
        self.updated = kwargs.get("updated", 0.0)


def on_haiku():
    return patch("haikode.haiku.is_haiku", return_value=True)


def recording(replies=None, default=None):
    rec = Recorder(replies, default)
    return rec, patch("haikode.haiku.subprocess.run", rec)


class DetectionTest(unittest.TestCase):

    def test_true_only_when_both_probes_agree(self):
        with (patch("haikode.haiku.platform.system", return_value="Haiku"),
              patch("haikode.haiku.os.path.isdir", return_value=True)):
            self.assertTrue(is_haiku())

    def test_false_when_platform_is_not_haiku(self):
        with (patch("haikode.haiku.platform.system", return_value="Darwin"),
              patch("haikode.haiku.os.path.isdir", return_value=True)):
            self.assertFalse(is_haiku())

    def test_false_when_boot_home_is_missing(self):
        with (patch("haikode.haiku.platform.system", return_value="Haiku"),
              patch("haikode.haiku.os.path.isdir", return_value=False)):
            self.assertFalse(is_haiku())

    def test_probes_the_expected_path(self):
        with (patch("haikode.haiku.platform.system", return_value="Haiku"),
              patch("haikode.haiku.os.path.isdir", return_value=True) as isdir):
            is_haiku()
        isdir.assert_called_once_with("/boot/home")

    def test_os_error_is_not_fatal(self):
        with (patch("haikode.haiku.platform.system", return_value="Haiku"),
              patch("haikode.haiku.os.path.isdir", side_effect=OSError("boom"))):
            self.assertFalse(is_haiku())


class OffHaikuTest(unittest.TestCase):
    """Every entry point must be a no-op that never spawns a process."""

    def setUp(self):
        haiku._INDEXED.clear()
        self.rec, patcher = recording()
        patcher.start()
        self.addCleanup(patcher.stop)
        off = patch("haikode.haiku.is_haiku", return_value=False)
        off.start()
        self.addCleanup(off.stop)

    def test_all_helpers_degrade(self):
        self.assertFalse(notify("t", "m"))
        self.assertFalse(set_attributes("/x", {"a": "b"}))
        self.assertEqual(get_attributes("/x"), {})
        self.assertEqual(list_attributes("/x"), [])
        self.assertIsNone(read_attribute("/x", "a"))
        self.assertFalse(copy_attributes("/x", "/y"))
        self.assertFalse(ensure_query_indices("/x"))
        self.assertFalse(tag_session_file("/x", _Session()))
        self.assertFalse(open_in_tracker("/x"))
        self.assertFalse(open_with_preferred("/x"))
        self.assertEqual(alert("really?", ["Yes", "No"]), "")

    def test_nothing_was_executed(self):
        notify("t", "m")
        set_attributes("/x", {"a": "b"})
        get_attributes("/x")
        list_attributes("/x")
        read_attribute("/x", "a")
        copy_attributes("/x", "/y")
        copy_attributes("/x", "/y", ["BEOS:TYPE"])
        ensure_query_indices("/x")
        tag_session_file("/x", _Session())
        open_in_tracker("/x")
        open_with_preferred("/x")
        alert("really?", ["Yes"])
        self.assertEqual(self.rec.calls, [])


class NotifyTest(unittest.TestCase):

    def test_argv_matches_the_notify_tool(self):
        rec, patcher = recording()
        with on_haiku(), patcher:
            self.assertTrue(notify("Run finished", "42 steps", kind="important",
                                   group="haikode", id="run-7"))
        self.assertEqual(rec.argvs, [[
            "notify", "--type", "important", "--group", "haikode",
            "--title", "Run finished", "--messageID", "run-7", "42 steps"]])

    def test_message_id_is_omitted_when_empty(self):
        rec, patcher = recording()
        with on_haiku(), patcher:
            notify("Done", "ok")
        self.assertNotIn("--messageID", rec.argvs[0])
        self.assertEqual(rec.argvs[0][-1], "ok")
        self.assertIn("information", rec.argvs[0])

    def test_unknown_kind_falls_back_to_information(self):
        rec, patcher = recording()
        with on_haiku(), patcher:
            notify("t", "m", kind="catastrophe")
        self.assertEqual(rec.argvs[0][1:3], ["--type", "information"])

    def test_failure_is_reported_not_raised(self):
        rec, patcher = recording(default=_Result(returncode=1))
        with on_haiku(), patcher:
            self.assertFalse(notify("t", "m"))

    def test_missing_tool_returns_false(self):
        with (on_haiku(),
              patch("haikode.haiku.subprocess.run",
                    side_effect=FileNotFoundError("notify"))):
            self.assertFalse(notify("t", "m"))

    def test_timeout_returns_false(self):
        with (on_haiku(),
              patch("haikode.haiku.subprocess.run",
                    side_effect=subprocess.TimeoutExpired("notify", 5))):
            self.assertFalse(notify("t", "m"))


class AttributeTypeTest(unittest.TestCase):

    def test_types_are_mapped_for_addattr(self):
        rec, patcher = recording()
        with on_haiku(), patcher:
            set_attributes("/boot/home/f", {
                "s": "text",
                "i": 7,
                "big": 2 ** 40,
                "f": 1.5,
                "b": True,
                "t": Timestamp(0),
            })
        codes = {argv[3]: argv[2] for argv in rec.argvs}
        self.assertEqual(codes["s"], "string")
        self.assertEqual(codes["i"], "int")
        self.assertEqual(codes["big"], "int64")
        self.assertEqual(codes["f"], "double")
        self.assertEqual(codes["b"], "bool")
        self.assertEqual(codes["t"], "time")

    def test_bool_is_not_written_as_an_int(self):
        rec, patcher = recording()
        with on_haiku(), patcher:
            set_attributes("/f", {"flag": False})
        self.assertEqual(rec.argvs[0], ["addattr", "-t", "bool", "flag", "0", "/f"])

    def test_argv_shape_and_target(self):
        rec, patcher = recording()
        with on_haiku(), patcher:
            self.assertTrue(set_attributes(Path("/boot/home/x"), {"a": "b"}))
        self.assertEqual(rec.argvs, [
            ["addattr", "-t", "string", "a", "b", "/boot/home/x"]])

    def test_one_failure_makes_the_whole_write_false(self):
        def reply(argv):
            return _Result(returncode=1 if argv[3] == "bad" else 0)

        rec, patcher = recording(replies={"addattr": reply})
        with on_haiku(), patcher:
            self.assertFalse(set_attributes("/f", {"good": "1", "bad": "2"}))
        self.assertEqual(len(rec.argvs), 2)

    def test_empty_attribute_map_writes_nothing(self):
        rec, patcher = recording()
        with on_haiku(), patcher:
            self.assertFalse(set_attributes("/f", {}))
        self.assertEqual(rec.calls, [])

    def test_timestamp_formats_for_addattr(self):
        # addattr -t time parses a date string but silently stores "now" for a
        # raw epoch, so the formatted form is the contract.
        stamp = Timestamp(0)
        self.assertRegex(stamp.format(), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertEqual(Timestamp("nonsense").epoch, 0.0)
        self.assertEqual(Timestamp(3), Timestamp(3.0))


class ReadAttributesTest(unittest.TestCase):

    LISTING = (b"File: s1.session\n"
               b"        Type       Size  Name\n"
               b"----------------------------------------\n"
               b"        Text        20  \"haikode:title\"\n"
               b"      Int-32         4  \"haikode:count\"\n"
               b"     Boolean         1  \"haikode:flag\"\n"
               b"      'TIME'         8  \"haikode:updated\"\n"
               b"\n"
               b"33 bytes total in attributes.\n")

    VALUES = {
        "haikode:title": b"Refactor the parser\n",
        "haikode:count": b"42\n",
        # listattr spells B_BOOL_TYPE "Boolean"; catattr renders it as 1 or 0.
        "haikode:flag": b"1\n",
        # catattr cannot render B_TIME_TYPE and falls back to a hex dump.
        "haikode:updated": b"0x000000:  36 9b 66 6a 00 00 00 00\n",
    }

    def _reader(self):
        def reply(argv):
            if argv[0] == "listattr":
                return _Result(stdout=self.LISTING)
            return _Result(stdout=self.VALUES[argv[2]])
        return reply

    def test_values_are_typed(self):
        rec, patcher = recording(replies={"listattr": self._reader(),
                                          "catattr": self._reader()})
        with on_haiku(), patcher:
            attrs = get_attributes("/boot/home/s1.session")
        self.assertEqual(attrs["haikode:title"], "Refactor the parser")
        self.assertEqual(attrs["haikode:count"], 42)
        self.assertEqual(attrs["haikode:updated"], 0x6A669B36)
        # Not the string "1": listattr's label for B_BOOL_TYPE is "Boolean".
        self.assertIs(attrs["haikode:flag"], True)

    def test_argv_uses_listattr_then_catattr_data_only(self):
        rec, patcher = recording(replies={"listattr": self._reader(),
                                          "catattr": self._reader()})
        with on_haiku(), patcher:
            get_attributes("/boot/home/s1.session")
        self.assertEqual(rec.argvs[0], ["listattr", "/boot/home/s1.session"])
        self.assertEqual(rec.argvs[1],
                         ["catattr", "-d", "haikode:title", "/boot/home/s1.session"])

    def test_header_and_footer_lines_are_not_attributes(self):
        rec, patcher = recording(replies={"listattr": self._reader(),
                                          "catattr": self._reader()})
        with on_haiku(), patcher:
            attrs = get_attributes("/f")
        self.assertEqual(set(attrs), set(self.VALUES))

    def test_unreadable_file_yields_empty_map(self):
        rec, patcher = recording(default=_Result(returncode=1))
        with on_haiku(), patcher:
            self.assertEqual(get_attributes("/nope"), {})

    def test_unreadable_single_attribute_is_skipped(self):
        def reply(argv):
            if argv[0] == "listattr":
                return _Result(stdout=self.LISTING)
            if argv[2] == "haikode:count":
                return _Result(returncode=1)
            return _Result(stdout=self.VALUES[argv[2]])

        rec, patcher = recording(replies={"listattr": reply, "catattr": reply})
        with on_haiku(), patcher:
            attrs = get_attributes("/f")
        self.assertNotIn("haikode:count", attrs)
        self.assertIn("haikode:title", attrs)


class CopyAttributesTest(unittest.TestCase):
    """The helper that stops an atomic replace from stripping a file's identity.

    Only `copyattr` can do this. The tempting alternative — `catattr --raw`
    into `addattr -f` — was tried on hrev57937 and silently destroys typed
    attributes: addattr re-parses the file it is handed as text, so an Int-32
    of 42 came back as 0 and B_BOOL_TYPE / B_TIME_TYPE failed outright. These
    tests pin the argv so nobody "simplifies" it back to that.
    """

    def test_whole_file_copy_is_one_copyattr(self):
        rec, patcher = recording()
        with on_haiku(), patcher:
            self.assertTrue(copy_attributes("/boot/home/a", "/boot/home/b.tmp"))
        self.assertEqual(rec.argvs,
                         [["copyattr", "--", "/boot/home/a", "/boot/home/b.tmp"]])

    def test_no_data_flag_so_the_file_contents_are_never_touched(self):
        # copyattr -d would overwrite the temp file we just wrote.
        rec, patcher = recording()
        with on_haiku(), patcher:
            copy_attributes("/a", "/b")
        for argv in rec.argvs:
            self.assertNotIn("-d", argv)
            self.assertNotIn("--data", argv)

    def test_paths_sit_behind_an_end_of_options_marker(self):
        # A path beginning with "-" must not be read as a flag.
        rec, patcher = recording()
        with on_haiku(), patcher:
            copy_attributes("-weird", "-other")
        argv = rec.argvs[0]
        self.assertEqual(argv[argv.index("--") + 1:], ["-weird", "-other"])

    def test_named_attributes_are_copied_one_at_a_time(self):
        rec, patcher = recording()
        with on_haiku(), patcher:
            self.assertTrue(copy_attributes("/a", "/b",
                                            ["BEOS:TYPE", "haikode:title"]))
        self.assertEqual(rec.argvs, [
            ["copyattr", "-n", "BEOS:TYPE", "--", "/a", "/b"],
            ["copyattr", "-n", "haikode:title", "--", "/a", "/b"],
        ])

    def test_one_failure_makes_the_whole_copy_false(self):
        def reply(argv):
            return _Result(returncode=1 if "haikode:title" in argv else 0)

        rec, patcher = recording(replies={"copyattr": reply})
        with on_haiku(), patcher:
            self.assertFalse(copy_attributes("/a", "/b",
                                             ["BEOS:TYPE", "haikode:title"]))

    def test_failure_is_reported_not_raised(self):
        rec, patcher = recording(default=_Result(returncode=1))
        with on_haiku(), patcher:
            self.assertFalse(copy_attributes("/a", "/b"))

    def test_missing_tool_returns_false(self):
        patcher = patch("haikode.haiku.subprocess.run",
                        side_effect=FileNotFoundError("copyattr"))
        with on_haiku(), patcher:
            self.assertFalse(copy_attributes("/a", "/b"))

    def test_a_source_with_no_attributes_is_success_not_failure(self):
        # copyattr exits 0 for a source with nothing on it; "nothing to copy"
        # must not read as an error the caller has to special-case.
        rec, patcher = recording(default=_Result(returncode=0))
        with on_haiku(), patcher:
            self.assertTrue(copy_attributes("/a", "/b"))


class AttributePrimitiveTest(unittest.TestCase):

    LISTING = (b"File: a\n"
               b"        Type       Size  Name\n"
               b"----------------------------------------\n"
               b" MIME String        19  \"BEOS:TYPE\"\n"
               b"      Int-32         4  \"MyApp:count\"\n"
               b"\n"
               b"23 bytes total in attributes.\n")

    def test_list_attributes_returns_names_with_their_types(self):
        rec, patcher = recording(replies={"listattr": _Result(stdout=self.LISTING)})
        with on_haiku(), patcher:
            found = list_attributes("/boot/home/a")
        self.assertEqual(found, [("BEOS:TYPE", "MIME String"),
                                 ("MyApp:count", "Int-32")])
        self.assertEqual(rec.argvs, [["listattr", "/boot/home/a"]])

    def test_list_attributes_of_an_unreadable_file_is_empty(self):
        rec, patcher = recording(default=_Result(returncode=1))
        with on_haiku(), patcher:
            self.assertEqual(list_attributes("/nope"), [])

    def test_read_attribute_asks_catattr_for_raw_bytes(self):
        rec, patcher = recording(
            replies={"catattr": _Result(stdout=b"\x01\x02\xff\x00raw")})
        with on_haiku(), patcher:
            value = read_attribute("/boot/home/a", "MyApp:blob")
        # --raw, not -d: -d renders an Int-32 as text and a time as a hex dump.
        self.assertEqual(rec.argvs,
                         [["catattr", "--raw", "MyApp:blob", "/boot/home/a"]])
        self.assertEqual(value, b"\x01\x02\xff\x00raw")

    def test_read_attribute_of_a_missing_attribute_is_none(self):
        rec, patcher = recording(default=_Result(returncode=1))
        with on_haiku(), patcher:
            self.assertIsNone(read_attribute("/a", "No:Such"))


class IndexTest(unittest.TestCase):

    def setUp(self):
        haiku._INDEXED.clear()
        self.addCleanup(haiku._INDEXED.clear)

    def test_creates_one_index_per_queryable_attribute(self):
        rec, patcher = recording()
        with on_haiku(), patcher:
            self.assertTrue(ensure_query_indices("/boot/home"))
        self.assertEqual(rec.argvs, [
            ["mkindex", "-t", kind, "-d", "/boot/home", name]
            for name, kind in QUERY_INDICES])

    def test_time_attribute_is_indexed_as_llong(self):
        self.assertEqual(dict(QUERY_INDICES)["haikode:updated"], "llong")

    def test_second_call_on_the_same_volume_is_free(self):
        rec, patcher = recording()
        with on_haiku(), patcher:
            ensure_query_indices("/boot/home")
            ensure_query_indices("/boot/home")
        self.assertEqual(len(rec.argvs), len(QUERY_INDICES))

    def test_existing_index_does_not_fail_the_call(self):
        rec, patcher = recording(default=_Result(returncode=1))
        with on_haiku(), patcher:
            self.assertTrue(ensure_query_indices("/boot/home"))


class SessionTagTest(unittest.TestCase):

    def setUp(self):
        haiku._INDEXED.clear()
        self.addCleanup(haiku._INDEXED.clear)

    def test_attribute_set_is_exactly_what_is_documented(self):
        session = _Session(title="Refactor the parser", provider="anthropic",
                           model="claude-sonnet-4", cwd="/boot/home/proj",
                           updated=1753600000.0)
        attrs = session_attributes(session)
        self.assertEqual(set(attrs), set(SESSION_ATTRS))
        self.assertEqual(attrs["haikode:title"], "Refactor the parser")
        self.assertEqual(attrs["haikode:provider"], "anthropic")
        self.assertEqual(attrs["haikode:model"], "claude-sonnet-4")
        self.assertEqual(attrs["haikode:cwd"], "/boot/home/proj")
        self.assertEqual(attrs["haikode:updated"], Timestamp(1753600000.0))

    def test_missing_fields_become_empty_strings(self):
        attrs = session_attributes(object())
        self.assertEqual(attrs["haikode:title"], "")
        self.assertEqual(attrs["haikode:updated"], Timestamp(0))

    def test_tagging_indexes_the_volume_then_writes(self):
        rec, patcher = recording()
        session = _Session(title="T", provider="zen", model="grok",
                           cwd="/boot/home", updated=1753600000.0)
        with on_haiku(), patcher:
            self.assertTrue(tag_session_file("/boot/home/s/one.session", session))
        self.assertEqual(len(rec.argv_for("mkindex")), len(QUERY_INDICES))
        self.assertEqual(rec.argv_for("mkindex")[0][4], "/boot/home/s")
        written = {argv[3]: (argv[2], argv[4]) for argv in rec.argv_for("addattr")}
        self.assertEqual(set(written), set(SESSION_ATTRS))
        self.assertEqual(written["haikode:provider"], ("string", "zen"))
        self.assertEqual(written["haikode:updated"][0], "time")
        for argv in rec.argv_for("addattr"):
            self.assertEqual(argv[-1], "/boot/home/s/one.session")

    def test_documented_query_string_is_in_the_docstring(self):
        # The README quotes this line; if it moves, the README is wrong.
        self.assertIn('query \'((haikode:provider=="anthropic")'
                      '&&(haikode:title=="*parser*"))\'',
                      tag_session_file.__doc__)


class TrackerTest(unittest.TestCase):

    def test_reveal_opens_the_containing_folder(self):
        rec, patcher = recording()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "one.session"
            target.write_text("x")
            with on_haiku(), patcher:
                self.assertTrue(open_in_tracker(target))
        self.assertEqual(rec.argvs, [["open", tmp]])

    def test_reveal_of_a_directory_opens_it_directly(self):
        rec, patcher = recording()
        with tempfile.TemporaryDirectory() as tmp:
            with on_haiku(), patcher:
                open_in_tracker(tmp)
        self.assertEqual(rec.argvs, [["open", tmp]])

    def test_preferred_application_opens_the_file_itself(self):
        rec, patcher = recording()
        with on_haiku(), patcher:
            self.assertTrue(open_with_preferred("/boot/home/notes.txt"))
        self.assertEqual(rec.argvs, [["open", "/boot/home/notes.txt"]])

    def test_open_failure_is_reported(self):
        rec, patcher = recording(default=_Result(returncode=1))
        with on_haiku(), patcher:
            self.assertFalse(open_with_preferred("/nope"))


class AlertTest(unittest.TestCase):

    def _interactive(self, value=True):
        return patch("haikode.haiku._interactive", return_value=value)

    def test_argv_and_returned_button(self):
        rec, patcher = recording(default=_Result(stdout=b"Overwrite\n"))
        with on_haiku(), self._interactive(), patcher:
            self.assertEqual(alert("Overwrite the file?", ["Cancel", "Overwrite"]),
                             "Overwrite")
        self.assertEqual(rec.argvs, [
            ["alert", "--info", "Overwrite the file?", "Cancel", "Overwrite"]])

    def test_kind_selects_the_alert_icon(self):
        rec, patcher = recording()
        with on_haiku(), self._interactive(), patcher:
            alert("Careful", ["Ok"], kind="stop")
        self.assertEqual(rec.argvs[0][1], "--stop")

    def test_unknown_kind_falls_back_to_info(self):
        rec, patcher = recording()
        with on_haiku(), self._interactive(), patcher:
            alert("Hm", ["Ok"], kind="disco")
        self.assertEqual(rec.argvs[0][1], "--info")

    def test_at_most_three_buttons_are_passed(self):
        rec, patcher = recording()
        with on_haiku(), self._interactive(), patcher:
            alert("Pick", ["a", "b", "c", "d"])
        self.assertEqual(rec.argvs[0][3:], ["a", "b", "c"])

    def test_never_opens_a_window_in_a_non_interactive_run(self):
        rec, patcher = recording()
        with on_haiku(), self._interactive(False), patcher:
            self.assertEqual(alert("Are you there?", ["Yes"]), "")
        self.assertEqual(rec.calls, [])

    def test_interactive_probe_survives_a_closed_stdin(self):
        with patch("haikode.haiku.sys.stdin", None):
            self.assertFalse(haiku._interactive())
        with patch("haikode.haiku.sys.stdin.isatty", side_effect=ValueError):
            self.assertFalse(haiku._interactive())

    def test_documents_the_non_interactive_prohibition(self):
        self.assertIn("NEVER call this from a non-interactive run",
                      alert.__doc__ or "")


class TimeoutTest(unittest.TestCase):
    """Every subprocess this module starts must be bounded."""

    def setUp(self):
        haiku._INDEXED.clear()
        self.addCleanup(haiku._INDEXED.clear)

    def test_every_call_passes_a_timeout(self):
        rec, patcher = recording(
            replies={"listattr": _Result(stdout=b'  Text  4  "a"\n'),
                     "catattr": _Result(stdout=b"v\n"),
                     "alert": _Result(stdout=b"Yes\n")})
        with (on_haiku(), patch("haikode.haiku._interactive", return_value=True),
              patcher):
            notify("t", "m")
            set_attributes("/f", {"a": "b"})
            get_attributes("/f")
            list_attributes("/f")
            read_attribute("/f", "a")
            copy_attributes("/f", "/g")
            copy_attributes("/f", "/g", ["a"])
            ensure_query_indices("/boot/home")
            tag_session_file("/boot/home/x/s", _Session())
            open_in_tracker("/boot/home")
            open_with_preferred("/boot/home/f")
            alert("q", ["Yes"])
        self.assertTrue(rec.calls)
        for argv, kwargs in rec.calls:
            self.assertIn("timeout", kwargs, argv)
            self.assertGreater(kwargs["timeout"], 0, argv)

    def test_alert_gets_the_long_timeout_and_the_rest_the_short_one(self):
        rec, patcher = recording(default=_Result(stdout=b"Yes\n"))
        with (on_haiku(), patch("haikode.haiku._interactive", return_value=True),
              patcher):
            notify("t", "m")
            alert("q", ["Yes"])
        self.assertEqual(rec.calls[0][1]["timeout"], COMMAND_TIMEOUT)
        self.assertEqual(rec.calls[1][1]["timeout"], ALERT_TIMEOUT)
        self.assertGreater(ALERT_TIMEOUT, COMMAND_TIMEOUT)

    def test_output_is_captured_so_tools_never_write_to_the_screen(self):
        rec, patcher = recording()
        with on_haiku(), patcher:
            notify("t", "m")
        _, kwargs = rec.calls[0]
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.PIPE)
        self.assertFalse(kwargs["check"])


class RealMachineTest(unittest.TestCase):
    """A single end-to-end check, skipped everywhere except on Haiku."""

    def setUp(self):
        if not is_haiku():
            self.skipTest("not running on Haiku")
        haiku._INDEXED.clear()
        self.addCleanup(haiku._INDEXED.clear)

    def test_attributes_round_trip_through_bfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "one.session"
            target.write_text("x")
            session = _Session(title="Refactor the parser", provider="anthropic",
                               model="claude-sonnet-4", cwd=tmp,
                               updated=1753600000.0)
            self.assertTrue(tag_session_file(target, session))
            attrs = get_attributes(target)
        self.assertEqual(attrs["haikode:title"], "Refactor the parser")
        self.assertEqual(attrs["haikode:provider"], "anthropic")
        self.assertEqual(attrs["haikode:cwd"], tmp)
        self.assertEqual(int(attrs["haikode:updated"]), 1753600000)

    def test_typed_attributes_survive_a_copy_between_files(self):
        """The property the whole edit path now depends on.

        An Int-32 is the one that catches a regression to the addattr route:
        it round-trips as 0 there, and as 42 here.
        """
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            dst = Path(tmp) / "dst"
            src.write_text("x")
            dst.write_text("y")
            self.assertTrue(set_attributes(src, {
                "BEOS:TYPE": "text/x-source-code",
                "MyApp:count": 42,
                "MyApp:flag": True,
                "MyApp:note": "en streng",
            }))
            self.assertTrue(copy_attributes(src, dst))

            self.assertEqual(dst.read_text(), "y")   # -d was not passed
            attrs = get_attributes(dst)
            self.assertEqual(attrs["BEOS:TYPE"], "text/x-source-code")
            self.assertEqual(attrs["MyApp:count"], 42)
            self.assertEqual(attrs["MyApp:note"], "en streng")
            self.assertEqual(attrs["MyApp:flag"], True)
            self.assertEqual(dict(list_attributes(dst))["MyApp:count"], "Int-32")
            self.assertEqual(read_attribute(dst, "MyApp:count"),
                             (42).to_bytes(4, "little"))


if __name__ == "__main__":
    unittest.main()
