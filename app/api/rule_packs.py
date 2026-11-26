"""Local rule-pack reads and credentialed governance operations."""
from __future__ import annotations

import os
import secrets
from typing import Annotated, Callable

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from app.config import get_config
from app.reports import rule_packs as packs
from app.store.database import get_database

router = APIRouter(prefix="/api/v1/rule-packs", tags=["rule-packs"])
Version = Annotated[str, Path(pattern=packs.VERSION_PATTERN)]
Credential = Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))]


def _require_credential(credential: HTTPAuthorizationCredentials | None, env: str) -> None:
    expected = os.environ.get(env)
    if not expected:
        raise HTTPException(503, "规则包管理凭据未配置")
    if credential is None:
        raise HTTPException(401, "需要规则包管理凭据", headers={"WWW-Authenticate": "Bearer"})
    if not secrets.compare_digest(credential.credentials.encode(), expected.encode()):
        raise HTTPException(403, "规则包管理凭据无效")


def require_admin(credential: Credential) -> None:
    _require_credential(credential, "ENGINE_RULE_PACK_ADMIN_TOKEN")


def require_signer(credential: Credential) -> None:
    _require_credential(credential, "ENGINE_RULE_PACK_SIGNER_TOKEN")


def _result(action: Callable[[], dict]) -> dict:
    try:
        return action()
    except packs.PackNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except packs.PackConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("")
def list_rule_packs() -> dict:
    db = get_database()
    with db.lock:
        active = packs.active_pack(db, get_config())
        return {"active_version": active["version"] if active else None, "packs": packs.list_packs(db)}


@router.get("/{version}")
def read_rule_pack(version: Version) -> dict:
    return _result(lambda: packs.get_pack(get_database(), version))


@router.put("/{version}", dependencies=[Depends(require_admin)])
def put_rule_pack(version: Version, body: packs.PackDraft) -> dict:
    return _result(lambda: packs.save_draft(get_database(), version, body))


@router.post("/{version}/publish", dependencies=[Depends(require_admin)])
def publish_rule_pack(version: Version, body: packs.PackActor) -> dict:
    return _result(lambda: packs.publish_pack(get_database(), version, body.actor_id))


@router.post("/{version}/sign", dependencies=[Depends(require_signer)])
def sign_rule_pack(version: Version, body: packs.PackActor) -> dict:
    return _result(lambda: packs.sign_pack(get_database(), version, body.actor_id))


@router.get("/{version}/audit")
def read_rule_pack_audit(version: Version) -> dict:
    return _result(lambda: {"events": packs.audit_events(get_database(), version)})
