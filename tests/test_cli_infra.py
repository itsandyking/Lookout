"""Tests for lookout infra CLI commands."""

from unittest.mock import patch

from click.testing import CliRunner

from lookout.cli import cli


class TestInfraCommands:
    def test_infra_group_exists(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["infra", "--help"])
        assert result.exit_code == 0
        assert "up" in result.output
        assert "down" in result.output

    def test_infra_up_calls_docker_compose(self):
        runner = CliRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = runner.invoke(cli, ["infra", "up"])
            assert result.exit_code == 0
            args = mock_run.call_args[0][0]
            assert "docker" in args
            assert "compose" in args
            assert "up" in args

    def test_infra_down_calls_docker_compose(self):
        runner = CliRunner()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            result = runner.invoke(cli, ["infra", "down"])
            assert result.exit_code == 0
            args = mock_run.call_args[0][0]
            assert "down" in args
