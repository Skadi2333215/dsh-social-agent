"""验证启动方式、安装布局和路径设置，测试不依赖本机安装 DSH。"""

from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from social_agent.config import find_dsh_executable, load_config
from social_agent.harness import HarnessClient
from tests.helpers import make_config, TEST_KEY


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = make_config(self.root)

    def test_desktop_resource_layouts(self):
        for executable, resources in [
            (self.root / 'desktop' / 'harness.exe', self.root / 'desktop' / 'resources'),
            (self.root / 'Harness.app' / 'Contents' / 'MacOS' / 'Harness', self.root / 'Harness.app' / 'Contents' / 'Resources'),
        ]:
            with self.subTest(executable=executable):
                executable.parent.mkdir(parents=True)
                executable.touch()
                resources.mkdir(parents=True)
                (resources / 'app.asar').touch()
                config = replace(self.config, dsh_executable=executable)
                client = HarnessClient(config)
                self.assertEqual(config.resources_dir, resources)
                self.assertEqual(client.launch_prefix(), [str(executable), '--expose-internals', str(config.cli_script)])

    def test_custom_launcher_preserves_argument_boundaries(self):
        config = replace(self.config, dsh_command=(sys.executable, str(self.root / 'space and 中文' / 'cli.py')))
        command = HarnessClient(config)._build_command(self.root / 'config patch.yml', 'session-123')
        self.assertEqual(Path(command[0]).resolve(), Path(sys.executable).resolve())
        self.assertEqual(command[1], config.dsh_command[1])
        self.assertEqual(command[-3:], ['--session-id', 'session-123', '-'])

    def test_custom_launcher_runs_through_production_process_boundary(self):
        # 适配夹具参数后交给虚拟 DSH，生产代码仍负责 CLI 选择、管道、会话及超时。
        adapter = self.root / 'launcher with space.py'
        fake_script = Path(__file__).parent / 'fake_dsh.py'
        adapter.write_text(
            'import os, runpy, sys\n'
            'assert "ELECTRON_RUN_AS_NODE" not in os.environ\n'
            'args = sys.argv[1:]\n'
            'sys.argv = [sys.argv[0]] + (["--session-id", args[args.index("--session-id") + 1]] if "--session-id" in args else [])\n'
            f'runpy.run_path({str(fake_script)!r}, run_name="__main__")\n',
            encoding='utf-8',
        )
        config = replace(self.config, dsh_command=(sys.executable, str(adapter)))
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': TEST_KEY, 'ELECTRON_RUN_AS_NODE': '1'}):
            first = HarnessClient(config).send('portable 中文', cwd=self.root)
            self.assertTrue(first.succeeded, first.error)
            second = HarnessClient(config).send('continue', cwd=self.root, session_id=first.session_id)
        self.assertTrue(second.succeeded, second.error)
        self.assertEqual(first.session_id, second.session_id)

    def test_command_configuration_validation_and_relative_executable(self):
        path = self.root / 'agent.toml'
        with patch.dict(os.environ, {'USERPROFILE': str(self.root), 'HOME': str(self.root)}, clear=True), patch('social_agent.config.find_dsh_executable', return_value=None):
            for value in ['"dsh"', '[""]', '[1]']:
                path.write_text(f'[agent]\ndsh_command = {value}\n', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'dsh_command'):
                    load_config(path)
            path.write_text('[agent]\ndsh_command = ["./bin/dsh", "--custom"]\n', encoding='utf-8')
            config = load_config(path)
        self.assertEqual(config.dsh_command, (str(self.root / 'bin' / 'dsh'), '--custom'))

    def test_missing_custom_program_is_a_clear_error(self):
        config = replace(self.config, dsh_command=(str(self.root / 'missing-program'),))
        with self.assertRaisesRegex(ValueError, 'dsh_command'):
            HarnessClient(config).launch_prefix()

    @unittest.skipUnless(os.name == 'nt', 'Windows 批处理边界')
    def test_windows_batch_launcher_is_rejected(self):
        command = self.root / 'dsh.cmd'
        command.write_text('@echo off\n', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'node.exe'):
            HarnessClient(replace(self.config, dsh_command=(str(command),))).launch_prefix()

    def test_macos_application_discovery(self):
        executable = self.root / 'Applications' / 'DeepSeek Harness.app' / 'Contents' / 'MacOS' / 'DeepSeek Harness'
        executable.parent.mkdir(parents=True)
        executable.touch()
        with patch('social_agent.config.sys.platform', 'darwin'), patch('social_agent.config.Path.home', return_value=self.root), patch('social_agent.config.shutil.which', return_value=None), patch.dict(os.environ, {}, clear=True):
            self.assertEqual(find_dsh_executable(), executable.resolve())

    def test_config_is_independent_of_caller_working_directory(self):
        path = self.root / 'agent.toml'
        path.write_text('[agent]\ndata_dir="state"\noutput_dir="archive"\n', encoding='utf-8')
        code = (
            'import json,sys; from pathlib import Path; from social_agent import load_config; '
            'config=load_config(Path(sys.argv[1])); '
            'print(json.dumps([str(config.data_dir),str(config.output_dir),str(config.env_file)]))'
        )
        result = subprocess.run(
            [sys.executable, '-c', code, str(path)], cwd=self.root,
            capture_output=True, text=True, encoding='utf-8', timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [str(self.root / 'state'), str(self.root / 'archive'), str(self.root / '.env')])
