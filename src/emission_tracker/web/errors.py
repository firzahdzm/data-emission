"""App-wide error handling that keeps request bodies out of responses."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def install_error_handlers(app: FastAPI) -> None:
    """Strip the offending input out of 422 validation errors.

    Pydantic builds these against the *raw parsed body*, before the model
    exists, and puts the rejected value in `error["input"]` — for a
    model-level `missing` error that is the whole body dict. So a request
    that omits `types` but carries a wallet unlock value gets the unlock
    value echoed straight back in the error, and `SecretStr` never gets a
    chance to mask it: pydantic rejected the request before the model was
    constructed.

    It only ever goes back to the caller that sent it, over the same TLS
    connection, so this is not a disclosure to a third party. It matters
    because 4xx bodies are exactly what gets recorded elsewhere — a proxy
    or WAF error log, a HAR file pasted into a bug report, an
    error-reporting middleware added later.

    `loc`, `type` and `msg` are all a caller needs to fix their request;
    the value they just sent is not.
    """

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        safe = [
            {k: v for k, v in error.items() if k not in ("input", "ctx", "url")}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": safe})
