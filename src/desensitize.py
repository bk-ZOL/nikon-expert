# src/desensitize.py
# 网页显示前的客户信息脱敏：把客户名/客户编号换成固定代号（客户A、客户B…）。
# 客户名单复用 acl_classify.CUSTOMER_DICT（定级用的那份），新增客户只需改那一处。
# 只改显示文字，不改库里的数据；检索、权限判断照常用真名。

import re

from src.acl_classify import CUSTOMER_DICT

# 同一客户的长写法要排在短写法前面先替换（"苏州星钥光子" 先于 "星钥"）
_EXTRA = {
    "customer:xingyue": ("苏州星钥光子", "星钥光子"),
}


def _build():
    pairs = []
    for i, (keys, scope) in enumerate(CUSTOMER_DICT):
        alias = f"客户{chr(ord('A') + i)}"
        for k in tuple(_EXTRA.get(scope, ())) + tuple(keys):
            pairs.append((k, alias))
    pairs.sort(key=lambda p: -len(p[0]))
    parts = []
    for k, _ in pairs:
        if re.fullmatch(r"[A-Za-z]+", k):
            parts.append(rf"(?<![A-Za-z]){re.escape(k)}(?![A-Za-z])")   # 英文按整词，免得误伤 "pcl" 之类的子串
        elif k.isdigit():
            parts.append(rf"(?<!\d){re.escape(k)}(?!\d)")
        else:
            parts.append(re.escape(k))
    lookup = {k.lower(): a for k, a in pairs}
    return re.compile("|".join(parts), re.IGNORECASE), lookup


_PAT, _LOOKUP = _build()


def mask(text: str) -> str:
    """把文字里的客户名换成代号。"""
    if not text:
        return text
    return _PAT.sub(lambda m: _LOOKUP.get(m.group(0).lower(), "客户"), text)
