"""Strip docstrings and comments from a module, keeping behaviour identical.

CircuitPython compiles source in RAM at import, and docstrings become string
objects that live there for the life of the module. On an RP2040 the full
driver plus a large script no longer fits; this removes only things the
interpreter never needs to run the code.
"""
import ast
import io
import sys


def strip(source):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            if len(body) == 1:
                body[0] = ast.Pass()      # never leave an empty body
            else:
                del body[0]
    return ast.unparse(ast.fix_missing_locations(tree))


if __name__ == "__main__":
    src = io.open(sys.argv[1], encoding="utf-8").read()
    out = strip(src)
    io.open(sys.argv[2], "w", encoding="utf-8").write(out + "\n")
    print("%s: %d -> %d bytes (%.0f%% smaller)"
          % (sys.argv[2], len(src), len(out), 100 * (1 - len(out) / len(src))))
