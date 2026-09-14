"""Persistence models."""

from dataclasses import dataclass


@dataclass
class User:
    id: str
    email: str
    is_admin: bool = False

    @classmethod
    def from_claims(cls, claims: dict) -> "User":
        """Build a User from verified token claims."""
        return cls(
            id=claims["sub"],
            email=claims.get("email", ""),
            is_admin=bool(claims.get("admin", False)),
        )


@dataclass
class Session:
    token: str
    user: User

    def is_admin_session(self) -> bool:
        return self.user.is_admin
