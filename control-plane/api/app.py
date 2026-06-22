"""Re-export the FastAPI app from the importable package."""

from voxbench.control_plane.app import app, create_app

__all__ = ["app", "create_app"]

