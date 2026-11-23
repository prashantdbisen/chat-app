"""
errors.py - One consistent error format for the whole API:

    {"detail": "Message for the top of the form",
     "errors": {"username": "Message for the username field", ...}}

The browser shows each message right under the field it belongs to.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_MISSING = {
    "username": "Enter a username.",
    "password": "Enter a password.",
    "name": "Enter a group name.",
    "content": "Enter a message.",
    "file": "Choose a file to send.",
}


class FieldError(Exception):
    """Raise this to report a problem with one specific form field."""

    def __init__(self, status_code: int, field: str, message: str) -> None:
        self.status_code, self.field, self.message = status_code, field, message


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(FieldError)
    async def field_error(request: Request, exc: FieldError):
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": exc.message, "errors": {exc.field: exc.message}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors: dict[str, str] = {}
        for error in exc.errors():
            field = str(error["loc"][-1]) if error.get("loc") else "form"
            if error.get("type") == "missing":
                message = _MISSING.get(field, f"{field.capitalize()} is required.")
            else:
                message = str(error.get("msg", "This value isn't valid."))
                message = message.removeprefix("Value error, ")  # Pydantic adds this prefix
            errors.setdefault(field, message)
        first = next(iter(errors.values()), "Check the form and try again.")
        return JSONResponse(status_code=422, content={"detail": first, "errors": errors})
