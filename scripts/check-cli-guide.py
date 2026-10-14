#!/usr/bin/env python3
"""Check web guide commands against the CLI declarations without running them."""
from __future__ import annotations

import ast
from html.parser import HTMLParser
from pathlib import Path
import re
import shlex


ROOT = Path(__file__).resolve().parents[1]


def cli_options() -> dict[str, set[str]]:
    result = {}
    tree = ast.parse((ROOT / "src/netvault/cli/user.py").read_text())
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef):
            continue
        for decorator in function.decorator_list:
            if not (isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and isinstance(decorator.func.value, ast.Name)
                    and decorator.func.value.id == "app"
                    and decorator.func.attr in {"command", "callback"}):
                continue
            name = "" if decorator.func.attr == "callback" else function.name.replace("_", "-")
            if decorator.args and isinstance(decorator.args[0], ast.Constant):
                name = decorator.args[0].value
            options = {"--help"}
            for parameter, default in zip(function.args.args[-len(function.args.defaults):],
                                          function.args.defaults):
                if not (isinstance(default, ast.Call)
                        and isinstance(default.func, ast.Attribute)
                        and default.func.attr == "Option"):
                    continue
                explicit = {item.value for item in default.args
                            if isinstance(item, ast.Constant) and isinstance(item.value, str)
                            and item.value.startswith("-")}
                options.update(explicit or {"--" + parameter.arg.replace("_", "-")})
            result[name] = options
    return result


class CommandRow(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_code = False
        self.display = []
        self.copied = None

    def handle_starttag(self, tag, attrs):
        if tag == "code":
            self.in_code = True
        if tag == "button":
            self.copied = dict(attrs).get("data-copy")

    def handle_endtag(self, tag):
        if tag == "code":
            self.in_code = False

    def handle_data(self, data):
        if self.in_code:
            self.display.append(data)


def main() -> None:
    options = cli_options()
    template = ROOT / "packages/netvault-server/src/netvault_server/server/templates/cli.html"
    rows = re.findall(r'<div class="command-row">(.*?)</div>', template.read_text(), re.S)
    if not rows:
        raise RuntimeError("No CLI guide commands found")
    for row in rows:
        parser = CommandRow()
        parser.feed(row)
        shown = shlex.split("".join(parser.display))
        copied = shlex.split(parser.copied or "")
        if shown != copied:
            raise RuntimeError(f"Displayed and copied commands differ: {shown!r} != {copied!r}")
        if not copied or copied[0] != "nv":
            continue
        name = "" if copied[1].startswith("-") else copied[1]
        if name not in options:
            raise RuntimeError(f"Unknown CLI command in guide: {name}")
        for token in copied[1 if not name else 2:]:
            if token.startswith("-") and token.split("=", 1)[0] not in options[name]:
                raise RuntimeError(f"Unknown option in guide: {name} {token}")
    print(f"CLI guide matches CLI declarations and copy buttons: {len(rows)} commands")


if __name__ == "__main__":
    main()
