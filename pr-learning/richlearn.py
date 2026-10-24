from rich import print
from rich import pretty
from rich.panel import Panel
from rich.color import Color
from rich.console import Console 
from rich import inspect
from rich.json import JSON


print("PANEL")
print(Panel.fit("[bold yellow]Hi, I'm a Panel", border_style="red"))
pretty.install()
print("PRETTY")
print(["Rich and pretty", True])

# color = Color.parse("red")
# print("inspect: ")
# print(inspect(color, methods=True))


# console

# console = Console()

# console.print([1, 2, 3])
# console.print("[blue underline]Looks like a link")
# # console.print(locals())
# console.print("FOO", style="white on blue")
console = Console(width=20)
console2 = Console()

style = "bold white on blue"
console.print("Rich", style=style)
console.print("Rich", style=style, justify="left")
console.print("Rich", style=style, justify="center")
console.print("Rich", style=style, justify="right")

#logging JSON object
console2.log(JSON('["foo", "bar"]'))
# JSON printintg
console.rule("[bold red]Chapter 2")

console3 = Console(width=14)
supercali = "supercalifragilisticexpialidocious"

overflow_methods = ["fold", "crop", "ellipsis"]
for overflow in overflow_methods:
    console3.rule(overflow)
    console3.print(supercali, overflow=overflow, style="bold blue")
    console3.print()


#Console styling 
blue_console = Console(style="white on blue")
blue_console.print("Im blue console")