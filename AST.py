from pathlib import Path
import tree_sitter_python as tspython
from tree_sitter import Language, Parser


source = Path('func.py').read_bytes()

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


# WALKING SYNTAX TREES
# USE cursor method

cursor = tree.walk()
assert cursor.node.type == "module"


# FUNCTION WALKIING 

tree2 = parser.parse(source)
def walk(node): 
    if node.type == "function_definition": 
        name_node = node.child_by_field_name("name")
        if name_node is not None: 
            name = source[name_node.start_byte:name_node.end_byte]
            print(name.decode("utf-8"))


    for child in node.children:
        walk(child)

print("Functions found: ")
walk(tree2.root_node)

# function walking using cursor


