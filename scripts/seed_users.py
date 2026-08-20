#!/usr/bin/env python
"""Seed dev users with short, human-typeable codes (spec §1.1).

    .venv/bin/python scripts/seed_users.py Kushal Rohan

Prints the codes to hand out. Re-running is safe: an existing display name keeps
its existing code rather than getting a second account.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import string
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from control_plane.db import SessionLocal, create_all  # noqa: E402
from control_plane.models import AuthKind, User  # noqa: E402

ALPHABET = string.ascii_uppercase + string.digits


def make_code(display_name: str) -> str:
    prefix = "".join(c for c in display_name.upper() if c.isalnum())[:8] or "USER"
    suffix = "".join(secrets.choice(ALPHABET) for _ in range(3))
    return f"{prefix}-{suffix}"


async def seed(names: list[str]) -> None:
    await create_all()
    issued: list[tuple[str, str]] = []
    async with SessionLocal() as session:
        for name in names:
            existing = (
                await session.execute(select(User).where(User.display_name == name))
            ).scalar_one_or_none()
            if existing is not None:
                print(f"  {name:<16} {existing.user_code}   (existing)")
                issued.append((name, existing.user_code or ""))
                continue

            user = User(
                display_name=name, user_code=make_code(name), auth_kind=AuthKind.dev
            )
            session.add(user)
            await session.commit()
            print(f"  {name:<16} {user.user_code}   (created)")
            issued.append((name, user.user_code or ""))

    if len(issued) >= 2:
        Path("dev_codes.json").write_text(
            json.dumps(
                {
                    "caller": issued[0][0],
                    "caller_code": issued[0][1],
                    "callee": issued[1][0],
                    "callee_code": issued[1][1],
                },
                indent=2,
            )
            + "\n"
        )
        print("\nwrote dev_codes.json (used by scripts/smoke_call.py)")


def main() -> None:
    names = sys.argv[1:] or ["Kushal", "Rohan"]
    print("Seeding dev users:")
    asyncio.run(seed(names))
    print("\nLog in with name + code on each device.")


if __name__ == "__main__":
    main()
