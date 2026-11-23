"""W05 本地身份认证实现包。"""

from core.auth.repository import AuthRepository, new_session_id
from core.auth.service import AuthService, LoginResult

__all__ = ["AuthRepository", "AuthService", "LoginResult", "new_session_id"]
