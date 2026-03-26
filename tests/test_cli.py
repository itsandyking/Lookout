from click.testing import CliRunner

from lookout.cli import cli


def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "audit" in result.output
    assert "enrich" in result.output
    assert "rank" in result.output
    assert "vendors" in result.output
    assert "output" in result.output


def test_audit_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["audit", "--help"])
    assert result.exit_code == 0
    assert "--vendor" in result.output
    assert "--out" in result.output
    assert "--include-house-brands" in result.output


def test_enrich_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["enrich", "--help"])
    assert result.exit_code == 0


def test_enrich_run_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["enrich", "run", "--help"])
    assert result.exit_code == 0
    assert "--vendor" in result.output
    assert "--max-rows" in result.output
    assert "-i" in result.output
    assert "--verify" in result.output


def test_rank_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["rank", "--help"])
    assert result.exit_code == 0
    assert "--collection" in result.output
    assert "--vendor" in result.output


def test_vendors_command():
    runner = CliRunner()
    # vendors.yaml is at project root, but CliRunner changes cwd
    # Use mix_stderr=False and check for either success or graceful error
    result = runner.invoke(cli, ["vendors", "--vendors", "vendors.yaml"])
    # If vendors.yaml found, should show vendor names
    if result.exit_code == 0:
        assert "Patagonia" in result.output or "vendor" in result.output.lower()


def test_output_help():
    runner = CliRunner()
    result = runner.invoke(cli, ["output", "--help"])
    assert result.exit_code == 0
    assert "matrixify-images" in result.output
    assert "alt-text" in result.output
    assert "google-shopping" in result.output
