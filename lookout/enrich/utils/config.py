"""
Configuration loading utilities.
"""

from pathlib import Path

import yaml

from ..models import VendorsConfig


def load_vendors_config(config_path: str | Path) -> VendorsConfig:
    """
    Load vendor configuration from a YAML file.

    Args:
        config_path: Path to the vendors.yaml file.

    Returns:
        VendorsConfig object with all vendor settings.

    Raises:
        FileNotFoundError: If the config file doesn't exist.
        ValueError: If the config file is invalid.
    """
    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Vendor config file not found: {config_path}")

    with open(config_path) as f:
        raw_config = yaml.safe_load(f)

    if not raw_config:
        raise ValueError(f"Empty or invalid config file: {config_path}")

    return VendorsConfig.model_validate(raw_config)


def get_default_vendors_config_path() -> Path:
    """Get the default path to vendors.yaml."""
    # Look in current directory, then package directory
    cwd_path = Path.cwd() / "vendors.yaml"
    if cwd_path.exists():
        return cwd_path

    # Package directory
    package_path = Path(__file__).parent.parent.parent / "vendors.yaml"
    if package_path.exists():
        return package_path

    return cwd_path  # Return CWD path as default even if not exists
