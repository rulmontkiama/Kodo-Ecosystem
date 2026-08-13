import ast

def find_undefined_names(filename):
    with open(filename, 'r') as f:
        tree = ast.parse(f.read())

    defined_names = set()
    used_names = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                defined_names.add(node.id)
            elif isinstance(node.ctx, ast.Load):
                used_names.add(node.id)
        elif isinstance(node, ast.Import):
            for n in node.names:
                defined_names.add(n.asname or n.name)
        elif isinstance(node, ast.ImportFrom):
            for n in node.names:
                defined_names.add(n.asname or n.name)
        elif isinstance(node, ast.ClassDef):
            defined_names.add(node.name)
        elif isinstance(node, ast.FunctionDef):
            defined_names.add(node.name)

    # Builtins
    import builtins
    defined_names.update(dir(builtins))

    undefined = used_names - defined_names
    return undefined

if __name__ == "__main__":
    import sys
    for f in sys.argv[1:]:
        print(f"File: {f}")
        und = find_undefined_names(f)
        if und:
            print(f"Undefined: {und}")
        else:
            print("No undefined names found.")
