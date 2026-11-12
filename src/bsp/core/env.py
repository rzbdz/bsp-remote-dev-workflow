"""Host environment: config values taken from the process environment, and shell-like expansion.

A host config may write any string as ``env:NAME`` (at any depth) to take the value of the
``NAME`` variable from the environment bsp-hostd runs with. A host-level ``env:`` map holds
values for hooks; hooks receive it as ``req["env"]``, in their process environment, and may
use ``${NAME}`` / ``${NAME:-default}`` in their ``args``.

Only ``env:NAME`` (a whole string) is a reference; a value that merely contains ``env:`` is a
literal. Expansion is deliberately the small shell subset hook arguments use in practice.
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping

from .errors import InvalidRequest

_REF = re.compile(r"^\s*env:([A-Za-z_][A-Za-z0-9_]*)\s*$")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def resolve_refs(value: Any, where: str = "config", environ: Mapping[str, str] | None = None) -> Any:
    """Replace every string written as ``env:NAME`` with the process environment's value.

    Walks dicts and lists. ``where`` names the config document in the error for an unset
    variable, so the message points at the file the operator must fix.
    """
    env = os.environ if environ is None else environ
    if isinstance(value, str):
        m = _REF.match(value)
        if not m:
            return value
        name = m.group(1)
        if name not in env:
            raise InvalidRequest(
                f"{where}: env:{name} is not set in bsp-hostd's environment",
                hint=f"export {name} where bsp-hostd runs (for a service, put it in "
                     f"/etc/bsp/hostd.env), or write the value into the host config",
            )
        return env[name]
    if isinstance(value, dict):
        return {k: resolve_refs(v, f"{where}.{k}", env) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, f"{where}[{i}]", env) for i, v in enumerate(value)]
    return value


def expand(value: Any, env: Mapping[str, str]) -> Any:
    """Expand ``${NAME}`` and ``${NAME:-default}`` in strings, recursively through dicts/lists.

    As in a shell, ``${NAME}`` becomes empty when unset, and ``${NAME:-default}`` uses the
    default when ``NAME`` is unset or empty.
    """

    def one(text: str) -> str:
        def repl(m: re.Match[str]) -> str:
            name, default = m.group(1), m.group(2)
            got = env.get(name)
            if got:
                return got
            return default or ""

        return _VAR.sub(repl, text)

    if isinstance(value, str):
        return one(value)
    if isinstance(value, dict):
        return {k: expand(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, env) for v in value]
    return value
