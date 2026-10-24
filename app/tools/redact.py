"""Redact secrets and IP addresses, then mark the text as untrusted.

Service names and hostnames stay. Injection text stays. The same IP always
receives the same placeholder inside one tool adapter.
"""

import re

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SECRET = re.compile(r"(?i)\b(api[_-]?key|token|password|secret)\b\s*[:=]\s*\S+")
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b")

UNTRUSTED_START = "<<UNTRUSTED_DATA>>"
UNTRUSTED_END = "<<END_UNTRUSTED_DATA>>"


def redact_text(text: str, ip_aliases: dict[str, str]) -> str:
    redacted = _EMAIL.sub("<REDACTED:email>", text)
    redacted = _SECRET.sub(r"\1=<REDACTED:secret>", redacted)

    def replace_ip(match: re.Match[str]) -> str:
        ip = match.group(0)
        alias = ip_aliases.get(ip)
        if alias is None:
            alias = f"<ip-{len(ip_aliases) + 1}>"
            ip_aliases[ip] = alias
        return alias

    return _IPV4.sub(replace_ip, redacted)


def wrap_untrusted(text: str) -> str:
    return f"{UNTRUSTED_START}\n{text}\n{UNTRUSTED_END}"
