"""
FastAPI application factory for the Merchfill Web UI.
"""

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .routes import router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

logger = logging.getLogger(__name__)

# Paths
PACKAGE_DIR = Path(__file__).parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.

    Returns:
        Configured FastAPI application instance.
    """
    app = FastAPI(
        title="Merchfill Web UI",
        description="Web interface for the Merchfill automated merchandising pipeline",
        version="0.1.0",
    )

    # Mount static files
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # Include routes
    app.include_router(router)

    # Startup event
    @app.on_event("startup")
    async def startup_event() -> None:
        logger.info("Merchfill Web UI starting up")

        # Ensure runs directory exists
        from .storage import get_runs_dir
        runs_dir = get_runs_dir()
        logger.info(f"Runs directory: {runs_dir}")

        # Check for vendors.yaml
        vendors_path = Path(os.environ.get("VENDORS_YAML_PATH", "./vendors.yaml"))
        if vendors_path.exists():
            logger.info(f"Vendors config found: {vendors_path}")
        else:
            logger.warning(f"Vendors config not found: {vendors_path}")

    # Shutdown event
    @app.on_event("shutdown")
    async def shutdown_event() -> None:
        logger.info("Merchfill Web UI shutting down")

    return app


# Create the application instance
app = create_app()

# Jinja2 templates instance (shared with routes)
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def get_templates() -> Jinja2Templates:
    """Get the Jinja2 templates instance."""
    return templates
