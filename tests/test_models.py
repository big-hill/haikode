import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from haikode import models as models_module
from haikode.config import Config
from haikode.models import (CACHE_VERSION, ModelCatalog, ModelRef, add_provider,
                            parse_model_id, probe, remove_provider, set_default)

# Two providers is enough to exercise grouping, and keeping them explicit keeps
# the shipped defaults from changing the expected ordering under us.
ZEN = {
    "dialect": "openai",
    "base_url": "https://opencode.ai/zen/v1",
    "api_key": "public",
    "model": "north-mini",
    "context": 190000,
}
OPENAI = {
    "dialect": "openai",
    "base_url": "https://api.openai.com/v1",
    "key_env": "HAIKODE_TEST_MISSING_KEY",
    "model": "gpt-4o-mini",
    "context": 128000,
}

FAKE_MODELS = {
    "zen": (["north-mini", "grok-code-free"], ""),
    "openai": (["gpt-5", "gpt-4o-mini"], ""),
}

# What an endpoint volunteers about a model's window. Only some do; a model
# missing here stands for the ones that say nothing.
FAKE_CONTEXTS = {"gpt-5": 400000}


class ModelsTestCase(unittest.TestCase):
    def setUp(self):
        # The keystore helper would spawn a subprocess (and on Haiku a GUI
        # approval dialog); every key in these tests comes from the config.
        env = patch.dict(os.environ, {"HAI_DISABLE_KEYSTORE": "1"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HAIKODE_TEST_MISSING_KEY", None)

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.config = Config(str(self.dir / "config.json"))
        self.config.data["providers"] = {"zen": dict(ZEN), "openai": dict(OPENAI)}
        self.config.data["default_provider"] = "zen"
        self.cache_path = self.dir / "model-cache.json"
        self.calls = []

    def catalog(self) -> ModelCatalog:
        return ModelCatalog(self.config, cache_path=self.cache_path)

    def fake_list_models(self, config, name):
        self.calls.append(name)
        ids, error = FAKE_MODELS.get(name, ([], f"unknown provider '{name}'"))
        return [{"id": model_id, "context": FAKE_CONTEXTS.get(model_id, 0)}
                for model_id in ids], error

    def patched(self, fake=None):
        """Stub the listing call the catalogue makes.

        `fake` may still be written against the old (ids, error) contract:
        it is adapted, because several tests only care that listing raised
        or failed.
        """
        stub = fake or self.fake_list_models

        def entries(config, name):
            result = stub(config, name)
            items, error = result
            if items and isinstance(items[0], str):
                items = [{"id": model_id, "context": 0} for model_id in items]
            return items, error

        return patch.object(models_module.configtool, "list_model_entries",
                            entries)

    # --- refs ------------------------------------------------------------

    def test_ref_id_and_label_default_to_the_model(self):
        ref = ModelRef(provider="zen", model="north-mini")
        self.assertEqual(ref.id, "zen/north-mini")
        self.assertEqual(ref.label, "north-mini")
        self.assertEqual(parse_model_id("openrouter/vendor/model-1"),
                         ("openrouter", "vendor/model-1"))
        self.assertEqual(parse_model_id("nope"), ("", "nope"))

    # --- providers -------------------------------------------------------

    def test_providers_are_local_only_and_carry_auth_state(self):
        def explode(config, name):
            raise AssertionError("providers() must not hit the network")

        with self.patched(explode):
            rows = self.catalog().providers()

        self.assertEqual([row["name"] for row in rows], ["zen", "openai"])
        self.assertTrue(rows[0]["is_default"])
        self.assertTrue(rows[0]["auth_ok"])
        self.assertEqual(rows[0]["auth"], "key from config file")
        self.assertEqual(rows[0]["model"], "north-mini")
        self.assertEqual(rows[0]["base_url"], ZEN["base_url"])
        self.assertEqual(rows[0]["dialect"], "openai")
        self.assertFalse(rows[1]["auth_ok"])
        self.assertFalse(rows[1]["is_default"])

    def test_provider_order_puts_known_providers_before_custom_ones(self):
        self.config.data["providers"]["aardvark"] = {
            "dialect": "openai", "base_url": "https://a.example/v1", "model": "m"}
        self.assertEqual(self.catalog().provider_names(),
                         ["zen", "openai", "aardvark"])
        # dialog-provider.tsx: ranked ids are "Popular", the rest "Providers".
        self.assertEqual([row["category"] for row in self.catalog().providers()],
                         ["Popular", "Popular", "Providers"])

    # --- models and cache ------------------------------------------------

    def test_models_are_sorted_free_first_and_carry_provider_context(self):
        with self.patched():
            refs = self.catalog().models("zen")
        self.assertEqual([ref.model for ref in refs],
                         ["grok-code-free", "north-mini"])
        self.assertTrue(refs[0].free)
        self.assertFalse(refs[1].free)
        self.assertEqual(refs[0].context, 190000)
        self.assertEqual(refs[0].category, "zen")

    def test_cache_is_written_and_reused_by_a_new_catalog(self):
        with self.patched():
            first = self.catalog().models("zen")
            second = self.catalog().models("zen")

        self.assertEqual(self.calls, ["zen"])
        self.assertEqual([ref.model for ref in first], [ref.model for ref in second])

        stored = json.loads(self.cache_path.read_text())
        self.assertEqual(stored["version"], CACHE_VERSION)
        entry = stored["providers"]["zen"]
        self.assertEqual(entry["models"], ["north-mini", "grok-code-free"])
        self.assertEqual(entry["base_url"], ZEN["base_url"])
        self.assertLessEqual(abs(time.time() - entry["time"]), 60)

    def test_refresh_bypasses_the_cache(self):
        with self.patched():
            catalog = self.catalog()
            catalog.models("zen")
            catalog.models("zen")
            self.assertEqual(self.calls, ["zen"])
            catalog.models("zen", refresh=True)
        self.assertEqual(self.calls, ["zen", "zen"])

    def test_cache_is_dropped_when_the_endpoint_changes(self):
        with self.patched():
            self.catalog().models("zen")
            self.config.data["providers"]["zen"]["base_url"] = "https://other.example/v1"
            self.catalog().models("zen")
        self.assertEqual(self.calls, ["zen", "zen"])

    def test_stale_cache_entry_is_refetched(self):
        with self.patched():
            self.catalog().models("zen")
            stored = json.loads(self.cache_path.read_text())
            stored["providers"]["zen"]["time"] = time.time() - (48 * 3600)
            self.cache_path.write_text(json.dumps(stored))
            self.catalog().models("zen")
        self.assertEqual(self.calls, ["zen", "zen"])

    def test_failing_provider_is_recorded_instead_of_raising(self):
        def half_broken(config, name):
            self.calls.append(name)
            if name == "openai":
                return [], "HTTP 401"
            return FAKE_MODELS[name]

        with self.patched(half_broken):
            catalog = self.catalog()
            refs = catalog.models()

        self.assertEqual({ref.provider for ref in refs}, {"zen"})
        self.assertEqual(catalog.errors, {"openai": "HTTP 401"})

    def test_exception_from_list_models_is_recorded(self):
        def boom(config, name):
            raise RuntimeError("socket exploded")

        with self.patched(boom):
            catalog = self.catalog()
            self.assertEqual(catalog.models("zen"), [])
        self.assertEqual(catalog.errors["zen"], "socket exploded")

    def test_a_failed_refresh_keeps_the_last_known_line_up(self):
        def unreachable(config, name):
            self.calls.append(name)
            return [], "unreachable: timed out"

        with self.patched():
            catalog = self.catalog()
            before = [ref.model for ref in catalog.models("zen")]
        with self.patched(unreachable):
            after = [ref.model for ref in catalog.models("zen", refresh=True)]

        self.assertEqual(after, before)
        self.assertEqual(catalog.errors["zen"], "unreachable: timed out")

    def test_a_dead_provider_is_not_retried_on_every_call(self):
        def unreachable(config, name):
            self.calls.append(name)
            return [], "unreachable: timed out"

        with self.patched(unreachable):
            catalog = self.catalog()
            catalog.models("zen")
            catalog.models("zen")
            catalog.models("zen")
            self.assertEqual(self.calls, ["zen"])
            catalog.models("zen", refresh=True)
        self.assertEqual(self.calls, ["zen", "zen"])

    def test_invalidate_clears_a_remembered_failure(self):
        def unreachable(config, name):
            self.calls.append(name)
            return [], "no API key set"

        catalog = self.catalog()
        with self.patched(unreachable):
            self.assertEqual(catalog.models("zen"), [])
        self.assertIn("zen", catalog.errors)

        # The dialog stores a key, then asks again: the remembered failure
        # must not survive the fix for it.
        catalog.invalidate("zen")
        self.assertEqual(catalog.errors, {})
        with self.patched():
            self.assertEqual(len(catalog.models("zen")), 2)
        self.assertEqual(self.calls, ["zen", "zen"])

    def test_an_expired_cache_is_served_when_the_refetch_fails(self):
        def unreachable(config, name):
            return [], "unreachable: timed out"

        with self.patched():
            self.catalog().models("zen")
        stored = json.loads(self.cache_path.read_text())
        stored["providers"]["zen"]["time"] = time.time() - (48 * 3600)
        self.cache_path.write_text(json.dumps(stored))

        with self.patched(unreachable):
            catalog = self.catalog()
            refs = catalog.models("zen")
        self.assertEqual([ref.model for ref in refs],
                         ["grok-code-free", "north-mini"])
        self.assertIn("zen", catalog.errors)

    def test_an_expired_cache_is_not_served_for_a_retargeted_endpoint(self):
        def unreachable(config, name):
            return [], "unreachable: timed out"

        with self.patched():
            self.catalog().models("zen")
        self.config.data["providers"]["zen"]["base_url"] = "https://other.example/v1"
        with self.patched(unreachable):
            self.assertEqual(self.catalog().models("zen"), [])

    def test_writing_the_cache_keeps_another_writers_entry(self):
        with self.patched():
            first = self.catalog()
            second = self.catalog()
            first.models("openai")                 # first's snapshot: {openai}
            second.models("zen")                   # disk becomes {openai, zen}
            first.models("openai", refresh=True)   # must not write its snapshot back

        stored = json.loads(self.cache_path.read_text())["providers"]
        self.assertEqual(sorted(stored), ["openai", "zen"])

    # --- choices ---------------------------------------------------------

    def test_choices_order_favourites_then_recents_then_providers(self):
        with self.patched():
            catalog = self.catalog()
            catalog.toggle_favourite(ModelRef("openai", "gpt-5"))
            catalog.record_use(ModelRef("zen", "north-mini"))
            catalog.record_use(ModelRef("openai", "gpt-5"))
            choices = catalog.choices()

        self.assertEqual(
            [(ref.category, ref.id) for ref in choices],
            [("Favourites", "openai/gpt-5"),
             ("Recent", "zen/north-mini"),
             ("zen", "zen/grok-code-free"),
             ("openai", "openai/gpt-4o-mini")])

    def test_choices_keep_favourites_of_providers_that_failed_to_list(self):
        def broken(config, name):
            return [], "unreachable"

        self.config.data[models_module.FAVOURITES_KEY] = ["zen/north-mini"]
        with self.patched(broken):
            choices = self.catalog().choices()
        self.assertEqual([ref.id for ref in choices], ["zen/north-mini"])

    def test_saved_entries_of_deleted_providers_are_dropped(self):
        self.config.data[models_module.FAVOURITES_KEY] = ["gone/model-a", "zen/north-mini"]
        self.config.data[models_module.RECENT_KEY] = ["gone/model-b", "bogus"]
        catalog = self.catalog()
        self.assertEqual([ref.id for ref in catalog.favourites()], ["zen/north-mini"])
        self.assertEqual(catalog.recent(), [])

    # --- favourites ------------------------------------------------------

    def test_toggle_favourite_persists_and_toggles_back(self):
        catalog = self.catalog()
        self.assertTrue(catalog.toggle_favourite(ModelRef("zen", "north-mini")))
        self.assertTrue(catalog.toggle_favourite("openai/gpt-5"))
        # opencode prepends, so the newest favourite comes first.
        self.assertEqual([ref.id for ref in catalog.favourites()],
                         ["openai/gpt-5", "zen/north-mini"])

        reloaded = Config(str(self.config.path))
        self.assertEqual(reloaded.data[models_module.FAVOURITES_KEY],
                         ["openai/gpt-5", "zen/north-mini"])

        self.assertFalse(catalog.toggle_favourite("openai/gpt-5"))
        self.assertEqual([ref.id for ref in catalog.favourites()], ["zen/north-mini"])

    # --- recents ---------------------------------------------------------

    def test_recents_are_capped_and_deduplicated(self):
        catalog = self.catalog()
        for index in range(12):
            catalog.record_use(ModelRef("zen", f"model-{index}"))

        stored = self.config.data[models_module.RECENT_KEY]
        self.assertEqual(len(stored), 10)
        self.assertEqual(stored[0], "zen/model-11")
        self.assertEqual(stored[-1], "zen/model-2")

        catalog.record_use("zen/model-5")
        stored = self.config.data[models_module.RECENT_KEY]
        self.assertEqual(stored[0], "zen/model-5")
        self.assertEqual(len(stored), 10)
        self.assertEqual(len(set(stored)), 10)

    def test_cycle_recent_wraps_in_both_directions(self):
        catalog = self.catalog()
        for model in ("north-mini", "grok-code-free"):
            catalog.record_use(ModelRef("zen", model))
        catalog.record_use(ModelRef("openai", "gpt-5"))
        # recents are newest first: gpt-5, grok-code-free, north-mini
        order = [ref.id for ref in catalog.recent()]
        self.assertEqual(order, ["openai/gpt-5", "zen/grok-code-free", "zen/north-mini"])

        self.assertEqual(catalog.cycle_recent("openai/gpt-5").id, "zen/grok-code-free")
        self.assertEqual(catalog.cycle_recent("zen/north-mini").id, "openai/gpt-5")
        self.assertEqual(catalog.cycle_recent("openai/gpt-5", step=-1).id,
                         "zen/north-mini")
        # Nothing recorded yet, and an unknown current, both stay useful.
        self.assertEqual(catalog.cycle_recent("zen/never-used").id, "openai/gpt-5")
        self.config.data[models_module.RECENT_KEY] = []
        self.assertIsNone(catalog.cycle_recent("openai/gpt-5"))

    def test_cycle_favourite_starts_at_either_end(self):
        catalog = self.catalog()
        self.assertIsNone(catalog.cycle_favourite("zen/north-mini"))
        catalog.toggle_favourite("zen/north-mini")
        catalog.toggle_favourite("openai/gpt-5")   # prepended, so it is first

        # Off the list: forwards starts at the first favourite, backwards at
        # the last — opencode's cycleFavorite.
        self.assertEqual(catalog.cycle_favourite("zen/never-used").id,
                         "openai/gpt-5")
        self.assertEqual(catalog.cycle_favourite(None, step=-1).id,
                         "zen/north-mini")
        self.assertEqual(catalog.cycle_favourite("openai/gpt-5").id,
                         "zen/north-mini")
        self.assertEqual(catalog.cycle_favourite("zen/north-mini").id,
                         "openai/gpt-5")

    # --- selection -------------------------------------------------------

    def test_select_switches_provider_and_records_the_use(self):
        catalog = self.catalog()
        selected = catalog.select(ModelRef("openai", "gpt-5"))

        self.assertEqual(selected.id, "openai/gpt-5")
        self.assertEqual(selected.context, 128000)
        self.assertEqual(self.config.data["default_provider"], "openai")
        self.assertEqual(self.config.data["providers"]["openai"]["model"], "gpt-5")
        self.assertEqual(self.config.data["providers"]["zen"]["model"], "north-mini")
        self.assertEqual([ref.id for ref in catalog.recent()], ["openai/gpt-5"])
        self.assertEqual(catalog.current().id, "openai/gpt-5")

        reloaded = Config(str(self.config.path))
        self.assertEqual(reloaded.data["default_provider"], "openai")
        self.assertEqual(reloaded.data["providers"]["openai"]["model"], "gpt-5")

    def test_select_rejects_an_unknown_provider(self):
        with self.assertRaises(KeyError):
            self.catalog().select("nowhere/model-a")

    def test_current_follows_config_get_provider_when_the_default_is_stale(self):
        self.config.data["default_provider"] = "deleted-provider"
        catalog = self.catalog()
        # Config.get_provider() falls back to the first profile, so the dialog
        # must name that one instead of claiming there is no model.
        self.assertEqual(self.config.get_provider()["model"],
                         catalog.current().model)
        self.assertEqual(catalog.current().id, "zen/north-mini")

    # --- provider management ---------------------------------------------

    def test_add_provider_validation_branches(self):
        ok, message = add_provider(self.config, "", "https://x.example/v1")
        self.assertFalse(ok)
        self.assertIn("name is required", message)

        ok, message = add_provider(self.config, "zen", "https://x.example/v1")
        self.assertFalse(ok)
        self.assertIn("already exists", message)

        ok, message = add_provider(self.config, "new", "ftp://x.example/v1")
        self.assertFalse(ok)
        self.assertIn("http://", message)

        ok, message = add_provider(self.config, "new", "https://x.example/v1",
                                   dialect="ollama")
        self.assertFalse(ok)
        self.assertIn("dialect", message)

        ok, message = add_provider(self.config, "bad name", "https://x.example/v1")
        self.assertFalse(ok)
        self.assertIn("provider name", message)

        ok, message = add_provider(self.config, "new", "https://x.example/v1/",
                                   model="m1")
        self.assertTrue(ok, message)
        self.assertIn("added", message)
        self.assertEqual(self.config.data["providers"]["new"]["base_url"],
                         "https://x.example/v1")
        self.assertEqual(self.config.data["providers"]["new"]["model"], "m1")

        ok, message = add_provider(self.config, "new", "http://127.0.0.1:11434/v1",
                                   model="m2", requires_key=False, update=True)
        self.assertTrue(ok, message)
        self.assertIn("updated", message)
        self.assertEqual(self.config.data["providers"]["new"]["model"], "m2")
        self.assertFalse(self.config.data["providers"]["new"]["requires_key"])

    def test_updating_a_provider_keeps_its_context_window(self):
        ok, message = add_provider(self.config, "zen", ZEN["base_url"],
                                   model="north-mini-code-free", update=True)
        self.assertTrue(ok, message)
        self.assertEqual(self.config.data["providers"]["zen"]["model"],
                         "north-mini-code-free")
        # Config.add_provider() rebuilds the profile with a 128k default.
        self.assertEqual(self.config.data["providers"]["zen"]["context"],
                         ZEN["context"])
        self.assertEqual(Config(str(self.config.path))
                         .data["providers"]["zen"]["context"], ZEN["context"])

    def test_an_oauth_provider_cannot_be_rebuilt_into_a_broken_one(self):
        self.config.data["providers"]["chatgpt"] = {
            "dialect": "chatgpt",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "oauth_provider": "chatgpt",
            "requires_key": False,
            "model": "gpt-5.4",
        }
        ok, message = add_provider(self.config, "chatgpt",
                                   "https://chatgpt.com/backend-api/codex",
                                   model="gpt-5.4", update=True)
        self.assertFalse(ok)
        self.assertIn("OAuth", message)
        profile = self.config.data["providers"]["chatgpt"]
        self.assertEqual(profile["oauth_provider"], "chatgpt")
        self.assertEqual(profile["dialect"], "chatgpt")

    def test_remove_provider_validation_branches(self):
        ok, message = remove_provider(self.config, "nope")
        self.assertFalse(ok)
        self.assertIn("unknown provider", message)

        ok, message = remove_provider(self.config, "zen")
        self.assertFalse(ok)
        self.assertIn("default provider", message)

        ok, message = remove_provider(self.config, "openai")
        self.assertTrue(ok, message)
        self.assertNotIn("openai", self.config.data["providers"])

        ok, message = remove_provider(self.config, "zen")
        self.assertFalse(ok)
        self.assertIn("only provider", message)
        self.assertIn("zen", self.config.data["providers"])

    def test_set_default_validation_branches(self):
        ok, message = set_default(self.config, "")
        self.assertFalse(ok)
        self.assertIn("name is required", message)

        ok, message = set_default(self.config, "nope")
        self.assertFalse(ok)
        self.assertIn("unknown provider", message)

        ok, message = set_default(self.config, "openai")
        self.assertTrue(ok, message)
        self.assertEqual(self.config.data["default_provider"], "openai")
        self.assertEqual(Config(str(self.config.path)).data["default_provider"],
                         "openai")

    def test_probe_delegates_to_configtool(self):
        with patch.object(models_module.configtool, "test_provider",
                          return_value=(True, "key accepted")) as tester:
            self.assertEqual(probe(self.config, "zen"), (True, "key accepted"))
        tester.assert_called_once_with(self.config, "zen")

        self.assertEqual(probe(self.config, "nope"),
                         (False, "unknown provider 'nope'"))

        with patch.object(models_module.configtool, "test_provider",
                          side_effect=RuntimeError("boom")):
            ok, detail = probe(self.config, "zen")
        self.assertFalse(ok)
        self.assertIn("boom", detail)

        # An exception object is always truthy, so a blank message must still
        # name the failure rather than trailing off after the colon.
        with patch.object(models_module.configtool, "test_provider",
                          side_effect=TimeoutError()):
            ok, detail = probe(self.config, "zen")
        self.assertFalse(ok)
        self.assertIn("TimeoutError", detail)


if __name__ == "__main__":
    unittest.main()
