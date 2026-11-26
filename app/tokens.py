"""预览凭证的签名/校验。

预览时把“所列现库端口记录”的快照放进签名 token，随确认表单一起提交。
签名防止值班员或中间环节篡改快照；服务端不引入第三份数据库。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from itsdangerous import BadSignature, URLSafeTimedSerializer

SALT = "idc-recovery-preview-v1"
# 预览凭证最长接受 24 小时；超时后重新预览即可
MAX_AGE = 24 * 3600


def make_serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=SALT)


def dump_token(serializer: URLSafeTimedSerializer, payload: Dict[str, Any]) -> str:
    return serializer.dumps(payload)


def load_token(
    serializer: URLSafeTimedSerializer, token: str
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not token:
        return None, "缺少预览凭证，请先勾选链路并预览。"
    try:
        payload = serializer.loads(token, max_age=MAX_AGE)
    except Exception as exc:  # SignatureExpired / BadSignature / malformed
        if isinstance(exc, BadSignature):
            return None, "预览凭证无效或已被篡改，请重新预览。"
        return None, "预览已过期或无效，请重新预览。"
    if not isinstance(payload, dict) or "sel" not in payload:
        return None, "预览凭证内容不完整，请重新预览。"
    return payload, None
