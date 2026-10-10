"""Local dedup profiles: versioning, CLI precedence and report safety."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from pycut import cli, config
from pycut.commands import dedup_cmd
from pycut.core.dedup_profiles import (
    ProfileError, apply_account_profile, load_account_profile,
)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "profiles.json"
        self._save({
            "version": 1,
            "accounts": {
                "channel_one": {
                    "preset": "light", "seed": 101, "mirror": True,
                    "speed": True, "psnr_min": 18.0, "phash_min": 3.0,
                },
                "channel_two": {
                    "preset": "aggressive", "seed": 202,
                    "border": True, "badge": False, "workers": 2,
                },
            },
        })

    def _save(self, content):
        self.file.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    def _parse(self, *argv):
        return cli.build_parser().parse_args(["dedup", *argv])

    def test_absent_profile_keeps_existing_cli_contract(self):
        args = self._parse("--preset", "standard", "--mirror", "--speed")
        self.assertIsNone(apply_account_profile(args))
        self.assertEqual(args.preset, "standard")
        self.assertTrue(args.mirror)
        self.assertTrue(args.speed)
        self.assertFalse(self._parse().mirror)
        self.assertEqual(self._parse().psnr_min, config.DEDUP_MIN_PSNR)
        self.assertEqual(self._parse().seed, 0)

    def test_two_accounts_have_independent_and_deterministic_settings(self):
        a = self._parse("--profile-file", str(self.file), "--account", "channel_one")
        b = self._parse("--profile-file", str(self.file), "--account", "channel_two")
        snapshot_a = apply_account_profile(a)
        snapshot_b = apply_account_profile(b)
        self.assertEqual((a.preset, a.seed, a.mirror), ("light", 101, True))
        self.assertEqual((b.preset, b.seed, b.workers), ("aggressive", 202, 2))
        self.assertEqual(snapshot_a["sources"]["preset"], "account")
        self.assertEqual(snapshot_a["sources"]["badge"], "default")
        self.assertNotEqual(snapshot_a["effective"], snapshot_b["effective"])
        fresh = self._parse("--profile-file", str(self.file),
                            "--account", "channel_one")
        self.assertEqual(apply_account_profile(fresh), snapshot_a)

    def test_explicit_cli_values_always_override_account_even_false(self):
        args = self._parse(
            "--profile-file", str(self.file), "--account", "channel_one",
            "--preset", "standard", "--no-mirror", "--seed", "303",
            "--psnr-min", "22", "--phash-min", "0",
        )
        resolved = apply_account_profile(args)
        self.assertEqual((args.preset, args.seed, args.mirror),
                         ("standard", 303, False))
        self.assertEqual(args.psnr_min, 22)
        self.assertEqual(args.phash_min, 0)
        self.assertTrue(args.speed)
        for field in ("preset", "seed", "mirror", "phash_min", "psnr_min"):
            self.assertEqual(resolved["sources"][field], "cli")
        self.assertEqual(resolved["sources"]["speed"], "account")

    def test_missing_account_or_path_rejected_without_guess(self):
        for argv in [
            ["--profile-file", str(self.file)],
            ["--account", "channel_one"],
            ["--profile-file", str(self.file), "--account", "not_found"],
        ]:
            with self.subTest(argv=argv), self.assertRaises(ProfileError):
                apply_account_profile(self._parse(*argv))

    def test_bad_schema_and_secret_fields_never_logged_as_values(self):
        cases = [
            {"version": 2, "accounts": {"a": {"preset": "light"}}},
            {"version": 1, "accounts": {"a": {"password": "sk-MUST-NOT-LEAK"}}},
            {"version": 1, "accounts": {"a": {"preset": "invalid"}}},
            {"version": 1, "accounts": {"a": {"seed": True}}},
            {"version": 1, "accounts": {"a": {"mirror": 1}}},
            {"version": 1, "accounts": {"a": {"workers": 5000}}},
            {"version": 1, "accounts": {"a": {"psnr_min": float("nan")}}},
            {"version": 1, "accounts": {"a": {"phash_min": -2}}},
            {"version": 1, "accounts": {"a": {}}},
            {"version": 1, "accounts": {"bad key": {"preset": "light"}}},
            {"version": 1, "accounts": {"a": {"preset": "light"}}, "token": "secret"},
        ]
        for payload in cases:
            self._save(payload)
            with self.subTest(payload=list(payload)), self.assertRaises(ProfileError) as ctx:
                load_account_profile(self.file, "a")
            self.assertNotIn("sk-MUST-NOT-LEAK", str(ctx.exception))

    def test_duplicate_fields_invalid_json_and_huge_file_rejected(self):
        for content in (
            '{"version":1,"version":1,"accounts":{"a":{"seed":1}}}',
            '{"version":1,"accounts":{"a":{"seed":1,"seed":2}}}',
            '{"version":1,"accounts":',
            " " * (64 * 1024 + 1),
        ):
            self.file.write_text(content, encoding="utf-8")
            with self.subTest(content=content[:50]), self.assertRaises(ProfileError):
                load_account_profile(self.file, "a")

    def test_report_includes_effective_nonsensitive_snapshot(self):
        ns = self._parse("--profile-file", str(self.file),
                         "--account", "channel_one", "--file", "/tmp/a.mp4")
        done = [{"src": "/tmp/a.mp4", "dst": "", "ok": True,
                 "phash_dist": 5.0, "psnr": 29.0, "seconds": 1.0}]
        with mock.patch.object(dedup_cmd, "install_signal_handlers"), \
             mock.patch.object(dedup_cmd, "reset_stop"), \
             mock.patch.object(dedup_cmd, "self_check"), \
             mock.patch.object(dedup_cmd, "_pick_srcs", return_value=[Path("/tmp/a.mp4")]), \
             mock.patch.object(dedup_cmd, "process_batch", return_value=done) as batch, \
             mock.patch.object(dedup_cmd, "write_report") as write, \
             mock.patch.object(dedup_cmd, "log"), \
             mock.patch.object(dedup_cmd.STOP, "flag", False):
            self.assertEqual(dedup_cmd.run(ns), 0)
        actual_opts = batch.call_args.args[1]
        self.assertEqual(actual_opts.preset, "light")
        self.assertEqual(actual_opts.seed, 101)
        self.assertTrue(actual_opts.mirror)
        self.assertEqual(actual_opts.psnr_min, 18.0)
        report = write.call_args.args[0]
        self.assertEqual(report[0]["account_profile"]["account"], "channel_one")
        self.assertEqual(report[0]["account_profile"]["effective"]["seed"], 101)
        self.assertNotIn("profile_file", json.dumps(report))

    def test_invalid_profile_does_not_touch_media_environment(self):
        self._save({"version": 1, "accounts": {
            "a": {"api_key": "sk-NEVER-DISPLAY"}}})
        args = self._parse("--profile-file", str(self.file), "--account", "a",
                           "--file", "/tmp/not-present.mp4")
        with mock.patch.object(dedup_cmd, "self_check") as probe, \
             mock.patch.object(dedup_cmd, "install_signal_handlers") as install, \
             mock.patch.object(dedup_cmd, "log") as log:
            self.assertEqual(dedup_cmd.run(args), 2)
        probe.assert_not_called()
        install.assert_not_called()
        self.assertNotIn("sk-NEVER-DISPLAY", str(log.call_args_list))


if __name__ == "__main__":
    unittest.main()
