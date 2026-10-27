"""FastAPI app. Mounted under /api so CloudFront routes /api/* to the ALB on the same origin as the web app."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from api.admin.routes import router as admin_router
from api.config import settings
from api.routes import account, exports, messages, metadata, plans, projects
from api.schemas import ErrorEnvelope
from core.errors import AppError, ErrorCode
from db.session import engine

log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    engine(pool_size=settings().db_pool_size)
    yield


app = FastAPI(
    title="Clipora AI API",
    version="0.1.0",
    lifespan=lifespan,
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    responses={
        400: {"model": ErrorEnvelope},
        401: {"model": ErrorEnvelope},
        403: {"model": ErrorEnvelope},
        404: {"model": ErrorEnvelope},
        409: {"model": ErrorEnvelope},
        422: {"model": ErrorEnvelope},
    },
)


@app.exception_handler(AppError)
def _app_error(_: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(jsonable_encoder(exc.envelope()), status_code=exc.http_status)


@app.exception_handler(RequestValidationError)
def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    err = AppError(ErrorCode.bad_request, "Some fields are not valid.", {"issues": exc.errors()})
    return JSONResponse(jsonable_encoder(err.envelope()), status_code=400)


@app.exception_handler(Exception)
def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    err = AppError(ErrorCode.internal, "Something went wrong. Please try again.")
    return JSONResponse(err.envelope(), status_code=500)


for router in (
    projects.router,
    messages.router,
    plans.router,
    exports.router,
    metadata.router,
    account.router,
    admin_router,
):
    app.include_router(router, prefix="/api")
