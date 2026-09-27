"""Minimal pyflakes: undefined globals and unused imports, via symtable.

No linter is installed on the development machine, and symtable does real
scope resolution, so this catches the two mistakes that matter most after
moving code between modules. Usage: python3 tools/lint.py sonolin/*.py
"""
import ast, builtins, sys, symtable

def check(path):
    src = open(path).read()
    tree = ast.parse(src)
    top = symtable.symtable(src, path, "exec")
    module_names = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported()}
    module_names |= set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
    problems = []
    def walk(tab):
        for sym in tab.get_symbols():
            if sym.is_referenced() and (sym.is_global() or tab.get_type() == "module") \
               and not sym.is_assigned() and not sym.is_imported() \
               and sym.get_name() not in module_names:
                problems.append(f"undefined name {sym.get_name()!r} in {tab.get_name()}")
        for child in tab.get_children():
            walk(child)
    walk(top)
    # unused imports: imported at module level, never referenced anywhere
    imported = {}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                imported[(a.asname or a.name).split(".")[0]] = node.lineno
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | \
           {n.value.id for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
    exported = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "__all__" for t in node.targets):
            exported = {e.value for e in node.value.elts}
    for name, line in imported.items():
        if name not in used and name not in exported and name != "annotations":
            problems.append(f"line {line}: unused import {name!r}")
    return problems

bad = 0
for path in sys.argv[1:]:
    for p in check(path):
        print(f"{path}: {p}"); bad += 1
print(f"{bad} problem(s) in {len(sys.argv)-1} file(s)")
