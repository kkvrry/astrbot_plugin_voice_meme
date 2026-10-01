# -*- coding: utf-8 -*-
"""llm_gate.py — 语音触发后是否仍交由 LLM 回复的判定"""

import re

import astrbot.api.message_components as Comp

def needs_llm_response(message: str, event, patterns, wake_prefix: str, private_mode: str) -> bool:
    """判断消息是否需要 LLM 回复"""
    clean_message = message.strip()
    is_private = not event.message_obj.group_id

    is_wake_word = clean_message.startswith(wake_prefix)

    is_at_bot = False
    bot_id = str(event.message_obj.self_id)
    for comp in event.message_obj.message:
        if isinstance(comp, Comp.At):
            qq_id = getattr(comp, 'qq', None) or getattr(comp, 'target', None)
            if qq_id and str(qq_id) == bot_id:
                is_at_bot = True
                break

    if is_wake_word:
        return False

    if is_private:
        if private_mode == "always": return True
        elif private_mode == "never": return False

    if not is_private and not is_at_bot:
        return False

    for pattern in patterns:
        if re.search(pattern, clean_message):
            return True

    # 简单判断：如果消息很短且触发了语音，可能不需要 LLM
    if len(clean_message) < 5:
         return False

    return True
