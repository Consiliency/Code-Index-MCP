"""
Environment detection and management for production deployments.
"""

import os
from enum import Enum
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_DOTENV_LOADED = False

# Same switch and truthy values python-dotenv itself uses; checked here so it
# also works with python-dotenv releases that predate it.
DOTENV_DISABLED_ENV = "PYTHON_DOTENV_DISABLED"
_DOTENV_DISABLED_VALUES = frozenset({"1", "true", "t", "yes", "y"})


def local_env_files_disabled() -> bool:
    """Return True when ``PYTHON_DOTENV_DISABLED`` turns local env file loading off."""
    return os.getenv(DOTENV_DISABLED_ENV, "").strip().casefold() in _DOTENV_DISABLED_VALUES


def load_local_env_files() -> None:
    """Load local env files once for CLI and development workflows.

    Copies ``.env.local``, ``.env`` and ``.env.native`` from the checkout root
    into ``os.environ`` without overriding values already set. Set
    ``PYTHON_DOTENV_DISABLED=1`` to skip the load; the test suite does so in
    ``tests/conftest.py`` so that the tracked ``.env.native`` cannot leak into
    a test process. A skipped load is not recorded as done, so clearing the
    variable later lets the next call load the files.
    """
    global _DOTENV_LOADED

    if _DOTENV_LOADED or local_env_files_disabled():
        return

    repo_root = Path(__file__).resolve().parents[2]
    for name in (".env.local", ".env", ".env.native"):
        env_path = repo_root / name
        if env_path.exists():
            load_dotenv(env_path, override=False)

    _DOTENV_LOADED = True


class Environment(str, Enum):
    """Supported deployment environments."""

    DEVELOPMENT = "development"
    TESTING = "testing"
    STAGING = "staging"
    PRODUCTION = "production"


def get_environment() -> Environment:
    """
    Get the current environment from environment variables.

    Returns:
        Environment: Current deployment environment

    Environment Variables:
        MCP_ENVIRONMENT: Explicit environment setting
        NODE_ENV: Node.js style environment (fallback)
        ENVIRONMENT: Generic environment variable (fallback)
    """
    # Check explicit MCP environment variable first
    env_str = os.getenv("MCP_ENVIRONMENT")

    # Fallback to common environment variables
    if not env_str:
        env_str = os.getenv("NODE_ENV")
    if not env_str:
        env_str = os.getenv("ENVIRONMENT")
    if not env_str:
        env_str = os.getenv("ENV")

    # Default to development if not set
    if not env_str:
        env_str = "development"

    # Normalize common variations
    env_str = env_str.lower().strip()

    # Map common variations to standard values
    env_mapping = {
        "dev": Environment.DEVELOPMENT,
        "development": Environment.DEVELOPMENT,
        "local": Environment.DEVELOPMENT,
        "test": Environment.TESTING,
        "testing": Environment.TESTING,
        "tests": Environment.TESTING,
        "stage": Environment.STAGING,
        "staging": Environment.STAGING,
        "pre-prod": Environment.STAGING,
        "preprod": Environment.STAGING,
        "prod": Environment.PRODUCTION,
        "production": Environment.PRODUCTION,
        "live": Environment.PRODUCTION,
    }

    return env_mapping.get(env_str, Environment.DEVELOPMENT)


def is_production() -> bool:
    """Check if running in production environment."""
    return get_environment() == Environment.PRODUCTION


def is_development() -> bool:
    """Check if running in development environment."""
    return get_environment() == Environment.DEVELOPMENT


def is_testing() -> bool:
    """Check if running in testing environment."""
    return get_environment() == Environment.TESTING


def is_staging() -> bool:
    """Check if running in staging environment."""
    return get_environment() == Environment.STAGING


def require_production() -> None:
    """
    Raise an error if not running in production.

    Raises:
        RuntimeError: If not in production environment
    """
    if not is_production():
        raise RuntimeError(
            f"This operation requires production environment. "
            f"Current environment: {get_environment().value}"
        )


def require_non_production() -> None:
    """
    Raise an error if running in production.

    Raises:
        RuntimeError: If in production environment
    """
    if is_production():
        raise RuntimeError("This operation is not allowed in production environment")


def get_environment_prefix() -> str:
    """
    Get environment variable prefix for current environment.

    Returns:
        str: Environment variable prefix (e.g., 'MCP_PROD_', 'MCP_DEV_')
    """
    env = get_environment()
    prefixes = {
        Environment.DEVELOPMENT: "MCP_DEV_",
        Environment.TESTING: "MCP_TEST_",
        Environment.STAGING: "MCP_STAGE_",
        Environment.PRODUCTION: "MCP_PROD_",
    }
    return prefixes[env]


def get_env_var(key: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    """
    Get environment variable with environment-specific prefix support.

    Args:
        key: Environment variable key
        default: Default value if not found
        required: Whether the variable is required

    Returns:
        str: Environment variable value

    Raises:
        ValueError: If required variable is not found

    Example:
        # In production, looks for MCP_PROD_SECRET_KEY, then SECRET_KEY
        secret = get_env_var("SECRET_KEY", required=True)
    """
    load_local_env_files()

    # Try environment-specific variable first
    env_prefix = get_environment_prefix()
    prefixed_key = f"{env_prefix}{key}"
    value = os.getenv(prefixed_key)

    # Fallback to generic key
    if value is None:
        value = os.getenv(key)

    # Use default if provided
    if value is None:
        value = default

    # Check if required
    if required and value is None:
        raise ValueError(f"Required environment variable not found: {prefixed_key} or {key}")

    return value
