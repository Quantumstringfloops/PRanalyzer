import tree_sitter_python as tspython
from tree_sitter import Language, Parser

PY_LANGUAGE = Language(tspython.language())
parser = Parser(PY_LANGUAGE)

tree = parser.parse(
    bytes = (
     """
    def foo ():
        if bar():
            baz()
    """
    )
)

print(tree)