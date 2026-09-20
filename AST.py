import tree_sitter_python as tspython
from tree_sitter import Language, Parser

PY_LANGUAGE = Language(tspython.language())
parser = Parser(PY_LANGUAGE)

tree = parser.parse(
    bytes(
     """
def foo ():
        if bar():
            baz()
""", 
        "utf8"
    )
)

print(tree)


root_node = tree.root_node
print(root_node) 