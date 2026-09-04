from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api.health import router as health_router
from app.api.router import api_router
from app.api.ucp import handoff_router as ucp_handoff_router
from app.api.ucp import router as ucp_router
from app.core.config import get_settings
from app.core.errors import DomainError

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="Protocol-neutral commerce core for a local cafe and specialty roastery.",
)
app.include_router(health_router)
app.include_router(api_router)
app.include_router(ucp_router)
app.include_router(ucp_handoff_router)


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, error: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"error": {"code": error.code, "message": error.message}},
    )
