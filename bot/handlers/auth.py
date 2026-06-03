"""
Decorators and helpers for access control.
"""
from __future__ import annotations

import functools
from typing import Callable

from pyrogram import Client
from pyrogram.types import Message

from config import AUTH_USERS, AUTH_GROUPS


def is_auth_user(user_id: int) -> bool:
    return user_id in AUTH_USERS


def is_auth_group(chat_id: int) -> bool:
    return chat_id in AUTH_GROUPS


def group_only(func: Callable) -> Callable:
    """Command must be used inside an AUTH_GROUP by an AUTH_USER."""
    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        if not is_auth_group(message.chat.id):
            return  # silently ignore outside groups
        if not is_auth_user(message.from_user.id):
            return  # silently ignore unauthorised users
        return await func(client, message, *args, **kwargs)
    return wrapper


def auth_only(func: Callable) -> Callable:
    """Command usable anywhere but only by AUTH_USERS."""
    @functools.wraps(func)
    async def wrapper(client: Client, message: Message, *args, **kwargs):
        if not is_auth_user(message.from_user.id):
            return
        return await func(client, message, *args, **kwargs)
    return wrapper
