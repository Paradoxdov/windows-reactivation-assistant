import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

import main
from waa import config, traces
from waa.errors import UnsafeProfileDirectory


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(str(message))

    warn = error = debug = info


class ProfileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.temp.name, "portable-app")
        os.makedirs(self.base)
        self.program_dir = mock.patch.object(
            config, "program_dir", return_value=self.base)
        self.program_dir.start()

    def tearDown(self):
        self.program_dir.stop()
        self.temp.cleanup()

    def test_rejects_program_root_and_outside_path(self):
        outside = os.path.join(self.temp.name, "outside")
        with self.assertRaises(UnsafeProfileDirectory):
            config.validate_profile_dir(self.base)
        with self.assertRaises(UnsafeProfileDirectory):
            config.validate_profile_dir(outside)

    def test_rejects_temp_and_its_descendants_as_profile(self):
        temp_root = os.path.join(self.base, config.LOCAL_TEMP_NAME)
        for profile in (temp_root, os.path.join(temp_root, "BrowserProfile")):
            with self.subTest(profile=profile):
                with self.assertRaises(UnsafeProfileDirectory):
                    config.validate_profile_dir(profile)

    def test_claims_new_profile_with_ownership_marker(self):
        profile = os.path.join(self.base, "CustomProfile")
        claimed = config.claim_profile_dir(profile)
        self.assertEqual(os.path.realpath(profile), claimed)
        self.assertTrue(os.path.isfile(
            os.path.join(profile, config.PROFILE_MARKER_NAME)))
        self.assertEqual(claimed, config.require_owned_profile_dir(profile))

    def test_refuses_nonempty_unowned_custom_directory(self):
        profile = os.path.join(self.base, "src")
        os.makedirs(profile)
        with open(os.path.join(profile, "important.txt"), "w", encoding="utf-8") as handle:
            handle.write("keep")
        with self.assertRaises(UnsafeProfileDirectory):
            config.claim_profile_dir(profile)
        with self.assertRaises(UnsafeProfileDirectory):
            config.require_owned_profile_dir(profile)

    def test_refuses_tampered_ownership_marker(self):
        profile = os.path.join(self.base, "CustomProfile")
        os.makedirs(profile)
        with open(os.path.join(profile, config.PROFILE_MARKER_NAME),
                  "w", encoding="ascii") as handle:
            handle.write("not our marker\n")
        with self.assertRaises(UnsafeProfileDirectory):
            config.claim_profile_dir(profile)
        with self.assertRaises(UnsafeProfileDirectory):
            config.require_owned_profile_dir(profile)

    def test_refuses_profile_redirected_inside_program_directory(self):
        target = os.path.join(self.base, "src")
        link = os.path.join(self.base, "BrowserProfile")
        os.makedirs(os.path.join(target, "Default"))
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError as exc:
            self.skipTest("directory symlinks are unavailable: %s" % exc)
        with self.assertRaises(UnsafeProfileDirectory):
            config.require_owned_profile_dir(link)

    def test_accepts_legacy_default_edge_profile(self):
        profile = os.path.join(self.base, "BrowserProfile")
        os.makedirs(os.path.join(profile, "Default"))
        with open(os.path.join(profile, "Local State"), "w", encoding="utf-8") as handle:
            handle.write("{}")
        with open(os.path.join(profile, "Default", "Preferences"),
                  "w", encoding="utf-8") as handle:
            handle.write("{}")
        self.assertEqual(os.path.realpath(profile),
                         config.require_owned_profile_dir(profile))

    def test_rejects_generic_default_subdirectory_as_legacy_profile(self):
        profile = os.path.join(self.base, "BrowserProfile")
        os.makedirs(os.path.join(profile, "Default"))
        with self.assertRaises(UnsafeProfileDirectory):
            config.require_owned_profile_dir(profile)

    def test_reset_removes_only_owned_profile(self):
        profile = config.claim_profile_dir(os.path.join(self.base, "CustomProfile"))
        with open(os.path.join(profile, "data"), "w", encoding="utf-8") as handle:
            handle.write("runtime")
        self.assertEqual(0, main.reset_profile(_Logger(), profile))
        self.assertFalse(os.path.exists(profile))

    def test_temp_sweep_does_not_touch_foreign_pyinstaller_directories(self):
        scratch = os.path.join(self.base, "Temp")
        temp_root = os.path.join(self.temp.name, "system-temp")
        foreign = os.path.join(temp_root, "_MEIforeign")
        os.makedirs(scratch)
        os.makedirs(foreign)
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "_MEIPASS", os.path.join(temp_root, "_MEIcurrent"),
                                  create=True), \
                mock.patch.dict(os.environ, {"TEMP": temp_root, "TMP": temp_root}):
            main.sweep_temp_leftovers(_Logger())
        self.assertFalse(os.path.exists(scratch))
        self.assertTrue(os.path.isdir(foreign))

    def test_unsafe_cleanup_preserves_profile_but_removes_safe_local_data(self):
        outside = os.path.join(self.temp.name, "outside")
        logs = os.path.join(self.base, "Logs")
        scratch = os.path.join(self.base, "Temp")
        os.makedirs(outside)
        os.makedirs(logs)
        os.makedirs(scratch)
        sentinel = os.path.join(outside, "keep.txt")
        with open(sentinel, "w", encoding="utf-8") as handle:
            handle.write("keep")
        with mock.patch.object(traces, "clean_shell_traces") as clean_traces, \
                redirect_stdout(StringIO()):
            result = main.cleanup_everything(outside, logs)
        self.assertEqual(2, result)
        self.assertTrue(os.path.isfile(sentinel))
        self.assertFalse(os.path.exists(logs))
        self.assertFalse(os.path.exists(scratch))
        clean_traces.assert_not_called()

    def test_cleanup_preserves_temp_when_it_is_configured_as_profile(self):
        profile = os.path.join(self.base, config.LOCAL_TEMP_NAME, "BrowserProfile")
        logs = os.path.join(self.base, "Logs")
        os.makedirs(profile)
        os.makedirs(logs)
        sentinel = os.path.join(profile, "keep.txt")
        with open(sentinel, "w", encoding="utf-8") as handle:
            handle.write("keep")
        with mock.patch.object(traces, "clean_shell_traces") as clean_traces, \
                redirect_stdout(StringIO()):
            result = main.cleanup_everything(profile, logs)
        self.assertEqual(2, result)
        self.assertTrue(os.path.isfile(sentinel))
        self.assertFalse(os.path.exists(logs))
        clean_traces.assert_not_called()

    def test_cleanup_command_never_sweeps_conflicting_temp_profile(self):
        profile = os.path.join(self.base, config.LOCAL_TEMP_NAME, "BrowserProfile")
        os.makedirs(profile)
        sentinel = os.path.join(profile, "keep.txt")
        with open(sentinel, "w", encoding="utf-8") as handle:
            handle.write("keep")
        settings = mock.Mock()
        settings.profile_dir = profile
        with mock.patch.object(config, "load", return_value=settings), \
                mock.patch.object(main, "sweep_temp_leftovers") as sweep, \
                mock.patch.object(traces, "clean_shell_traces") as clean_traces, \
                redirect_stdout(StringIO()):
            result = main.main(["--cleanup"])
        self.assertEqual(2, result)
        self.assertTrue(os.path.isfile(sentinel))
        sweep.assert_not_called()
        clean_traces.assert_not_called()

    def test_cleanup_skips_traces_for_missing_custom_profile(self):
        profile = os.path.join(self.base, "MissingCustomProfile")
        logs = os.path.join(self.base, "Logs")
        os.makedirs(logs)
        with mock.patch("waa.browser.Browser._release_profile") as release, \
                mock.patch.object(traces, "clean_shell_traces") as clean_traces, \
                redirect_stdout(StringIO()):
            result = main.cleanup_everything(profile, logs)
        self.assertEqual(0, result)
        release.assert_not_called()
        clean_traces.assert_not_called()

    def test_main_validates_profile_before_sweeping_temp(self):
        settings = mock.Mock()
        settings.profile_dir = os.path.join(
            self.base, config.LOCAL_TEMP_NAME, "BrowserProfile")
        settings.clear_profile_on_exit = False
        logger = mock.Mock()
        logger.session_log = os.path.join(self.base, "session.log")
        logger.debug_log = os.path.join(self.base, "debug.log")
        with mock.patch.object(main, "Logger", return_value=logger), \
                mock.patch.object(config, "load", return_value=settings), \
                mock.patch.object(main, "sweep_temp_leftovers") as sweep, \
                mock.patch.object(main, "run") as run_workflow:
            result = main.main(["--no-pause"])
        self.assertEqual(2, result)
        sweep.assert_not_called()
        run_workflow.assert_not_called()

    def test_cleanup_reports_recursive_delete_failure(self):
        profile = config.claim_profile_dir(os.path.join(self.base, "CustomProfile"))
        logs = os.path.join(self.base, "Logs")
        os.makedirs(logs)
        with mock.patch("waa.browser.Browser._release_profile", return_value=False), \
                mock.patch.object(main.shutil, "rmtree", side_effect=OSError("busy")), \
                mock.patch.object(main.time, "sleep"), \
                mock.patch.object(traces, "clean_shell_traces", return_value=[]), \
                redirect_stdout(StringIO()):
            result = main.cleanup_everything(profile, logs)
        self.assertEqual(2, result)

    def test_cleanup_reports_shell_trace_failure(self):
        profile = config.claim_profile_dir(os.path.join(self.base, "CustomProfile"))
        logs = os.path.join(self.base, "Logs")
        os.makedirs(logs)
        with mock.patch("waa.browser.Browser._release_profile", return_value=False), \
                mock.patch.object(traces, "clean_shell_traces",
                                  side_effect=RuntimeError("trace cleanup failed")), \
                redirect_stdout(StringIO()):
            result = main.cleanup_everything(profile, logs)
        self.assertEqual(2, result)

    def test_unsafe_erase_never_inspects_or_deletes_external_profile(self):
        outside = os.path.join(self.temp.name, "outside")
        os.makedirs(outside)
        sentinel = os.path.join(outside, "keep.txt")
        with open(sentinel, "w", encoding="utf-8") as handle:
            handle.write("keep")
        with mock.patch.object(main, "Browser") as browser:
            main.erase_session(_Logger(), outside)
        browser.assert_not_called()
        self.assertTrue(os.path.isfile(sentinel))

    def test_powershell_values_are_single_quote_escaped(self):
        script = traces._script(os.path.join(self.base, "x'; Write-Output HACK; #"), True)
        self.assertIn("$base = 'x''; Write-Output HACK; #'", script)
        self.assertNotIn("$base = 'x'; Write-Output HACK; #'", script)

    def test_powershell_matching_treats_profile_name_as_literal(self):
        script = traces._script(os.path.join(self.base, "Profile[x]"), True)
        self.assertIn("$aumids -contains $plain", script)
        self.assertIn("[regex]::Escape($a)", script)
        self.assertIn("(?![A-Za-z0-9_.-])", script)
        self.assertNotIn('-like "*$base*"', script)


if __name__ == "__main__":
    unittest.main()
