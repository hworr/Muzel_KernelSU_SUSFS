#!/usr/bin/env python3
"""Regression checks for source selection and the two Muzel config layers."""

import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import unittest

import yaml

REPO = Path(__file__).resolve().parents[2]


def action_steps(name):
    path = REPO / '.github' / 'actions' / name / 'action.yml'
    return yaml.safe_load(path.read_text())['runs']['steps']


class MuzelActionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='muzel-action-')
        self.addCleanup(self.temp.cleanup)
        self.kernel = Path(self.temp.name) / 'kernel'
        self.base = self.kernel / 'common/ack/arch/arm64/configs/gki_defconfig'
        self.device = self.kernel / 'private/devices/google/muzel/muzel_defconfig'
        for config in (self.base, self.device):
            config.parent.mkdir(parents=True, exist_ok=True)
            config.write_text('# CONFIG_KSU is not set\nCONFIG_CIFS=y\n')

    def run_step(self, step, **env):
        return subprocess.run(
            ['bash', '-e', '-o', 'pipefail', '-c', step['run']],
            cwd=self.kernel, env={**os.environ, **env},
            text=True, capture_output=True,
        )

    def test_features_reach_both_image_and_device(self):
        step = action_steps('set-kernel-config')[0]
        features = '\n# Features\nCONFIG_KSU=y\nCONFIG_BBG=y\nCONFIG_NTSYNC=y\nCONFIG_NOMOUNT=y\n'
        for _ in range(2):
            result = self.run_step(step, CONFIG_LIST=features)
            self.assertEqual(result.returncode, 0, result.stderr)
        for config in (self.base, self.device):
            for option in ('KSU', 'BBG', 'NTSYNC', 'NOMOUNT'):
                self.assertEqual(config.read_text().count(f'CONFIG_{option}=y\n'), 1)
            self.assertNotIn('# CONFIG_KSU is not set', config.read_text())

    def test_missing_device_config_does_not_partially_edit_base(self):
        before = self.base.read_bytes()
        self.device.unlink()
        result = self.run_step(action_steps('set-kernel-config')[0], CONFIG_LIST='CONFIG_KSU=y')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.base.read_bytes(), before)

    def prepare_bazel(self):
        self.kernel.joinpath('aosp').symlink_to('common/ack')
        config = self.kernel / 'aosp/build.config.gki'
        config.write_text('DEFCONFIG=gki_defconfig\nPOST_DEFCONFIG_CMDS="check_defconfig"\n')
        bazel = self.kernel / 'tools/bazel'
        bazel.parent.mkdir()
        bazel.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$CAPTURE_ARGS"\n')
        bazel.chmod(0o755)
        captured = self.kernel / 'args'
        return config, captured

    def test_bazel_build_selects_patched_ack_and_disables_prebuilts(self):
        config, captured = self.prepare_bazel()
        result = self.run_step(action_steps('build-kernel')[0], CAPTURE_ARGS=str(captured))
        self.assertEqual(result.returncode, 0, result.stderr)
        args = captured.read_text().splitlines()
        self.assertEqual(args[0], 'run')
        for flag in ('--kernel_package=ack', '--nouse_prebuilt_kernel', '--nouse_prebuilt_fips140'):
            self.assertIn(flag, args)
            self.assertGreater(args.index(flag), args.index('--config=muzel'))
        self.assertEqual(args[-1], '//private/devices/google/muzel:lga_muzel_dist')
        self.assertNotIn('check_defconfig', config.read_text())

    def test_selinux_helper_linkage_and_annotation(self):
        _, captured = self.prepare_bazel()
        source = self.kernel / 'aosp/drivers/kernelsu/feature/selinux_hide.c'
        source.parent.mkdir(parents=True)
        compiler = shutil.which('gcc')
        self.assertIsNotNone(compiler, 'gcc is required for the linkage regression check')
        for declaration in ('void', 'static void'):
            with self.subTest(declaration=declaration):
                # Model the declaration/definition mismatch from the build log.
                source.write_text(
                    '#define __nocfi\n'
                    f'{declaration} security_compute_av_user_with_policy(void);\n'
                    'static void __nocfi security_compute_av_user_with_policy(void) {}\n'
                    'int main(void) { security_compute_av_user_with_policy(); return 0; }\n'
                )
                before = source.read_text()
                result = subprocess.run([compiler, '-Werror', '-fsyntax-only', str(source)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode == 0, declaration == 'static void')
                step = action_steps('build-kernel')[0]
                for _ in range(2):
                    result = self.run_step(step, CAPTURE_ARGS=str(captured))
                    self.assertEqual(result.returncode, 0, result.stderr)
                if declaration == 'static void':
                    self.assertEqual(source.read_text(), before)
                else:
                    self.assertIn('void __nocfi security_compute_av_user_with_policy', source.read_text())
                    self.assertNotIn('static void __nocfi', source.read_text())
                result = subprocess.run([compiler, '-Werror', '-fsyntax-only', str(source)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_cifs_leaves_hidden_netfs_dependency_to_kconfig(self):
        options = action_steps('cifs')[0]['with']['config_list']
        self.assertIn('CONFIG_CIFS=m', options)
        self.assertNotIn('CONFIG_NETFS_SUPPORT', options)
        result = self.run_step(action_steps('set-kernel-config')[0], CONFIG_LIST=options)
        self.assertEqual(result.returncode, 0, result.stderr)
        for config in (self.base, self.device):
            self.assertIn('CONFIG_CIFS=m\n', config.read_text())


if __name__ == '__main__':
    unittest.main()
