from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Header, Response, status

from app.api.deps import Auth, Cfg, CurrentUser
from app.core.responses import ok
from app.modules.identity.schemas import LoginIn, RegisterIn, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])

REFRESH_COOKIE = "gd_refresh"


def _set_refresh_cookie(response: Response, token: str, max_age: int, secure: bool) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/api/v1/auth",
    )


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterIn, auth: Auth) -> dict:
    user = await auth.register(payload)
    return ok(UserOut.model_validate(user).model_dump(mode="json"))


@router.post("/login")
async def login(
    payload: LoginIn,
    auth: Auth,
    cfg: Cfg,
    response: Response,
    user_agent: Annotated[str | None, Header()] = None,
) -> dict:
    access, refresh, user = await auth.login(
        payload.email, payload.password, user_agent=user_agent
    )
    _set_refresh_cookie(response, refresh, cfg.refresh_token_ttl_seconds, cfg.is_production)
    return ok(
        {
            "access_token": access,
            "token_type": "bearer",
            "expires_in": cfg.access_token_ttl_seconds,
            "user": UserOut.model_validate(user).model_dump(mode="json"),
        }
    )


@router.post("/refresh")
async def refresh_tokens(
    auth: Auth,
    cfg: Cfg,
    response: Response,
    gd_refresh: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
    user_agent: Annotated[str | None, Header()] = None,
) -> dict:
    from app.core.errors import AuthenticationError

    if not gd_refresh:
        raise AuthenticationError("No session cookie was sent.")

    access, new_refresh, user = await auth.refresh(gd_refresh, user_agent=user_agent)
    _set_refresh_cookie(response, new_refresh, cfg.refresh_token_ttl_seconds, cfg.is_production)
    return ok(
        {
            "access_token": access,
            "token_type": "bearer",
            "expires_in": cfg.access_token_ttl_seconds,
            "user": UserOut.model_validate(user).model_dump(mode="json"),
        }
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    auth: Auth,
    user: CurrentUser,
    response: Response,
    gd_refresh: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> Response:
    await auth.logout(gd_refresh, user.id)
    response.delete_cookie(REFRESH_COOKIE, path="/api/v1/auth")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
