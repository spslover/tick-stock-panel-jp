"""JP & US equity market & stock AI analysis backend extension."""
from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "official.global-analysis"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    from .routes import router

    registrar.include_router(router)
