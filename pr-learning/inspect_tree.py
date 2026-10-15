from pathlib import Path
import tree_sitter_python as tspython
from tree_sitter import Language, Parser


PYTHON_LANGUAGE = Language(tspython.language())
parser = Parser(PYTHON_LANGUAGE)

source = Path("sample.py").read_bytes()
tree = parser.parse(source)

if_count = 0

def walk(node) -> None:
    global if_count

    if node.type == "function_definition": 
        name_node = node.child_by_field_name("name")

        if name_node is not None: 
            function_name = source[name_node.start_byte: name_node.end_byte].decode("utf-8")

            print("Function: ", function_name)
    if node.type == "if_statement":
        if_count += 1

    for child in node.children:
        walk(child) 

walk(tree.root_node)
print("Number of if statements:", if_count)