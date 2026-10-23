"""API error responses.

Every error uses the same JSON object: error_code, message, and details.
"""

from collections.abc import Sequence

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_STATUS_CODES = {
    400: "bad_request",
    404: "not_found",
    405: "method_not_allowed",
    422: "validation_error",
    500: "internal_error",
}


def error_payload(
    error_code: str,
    message: str,
    details: Sequence[object] | None = None,
) -> dict[str, object]:
    return {
        "error_code": error_code,
        "message": message,
        "details": list(details or []),
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        del request
        details = [
            {
                "loc": list(err.get("loc", ())),
                "msg": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_payload("validation_error", "Request validation failed.", details),
        )

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        del request
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return JSONResponse(status_code=exc.status_code, content=error_payload(code, message))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=500,
            content=error_payload("internal_error", "Internal server error."),
        )
