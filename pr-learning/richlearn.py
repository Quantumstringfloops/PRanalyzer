from rich import print
from rich import pretty
from rich.panel import Panel
from rich.color import Color
from rich.console import Console
from rich import inspect

print(Panel.fit("[bold yellow]Hi, I'm a Panel", border_style="red"))
pretty.install()
print(["Rich and pretty", True])

color = Color.parse("red")
print(inspect(color, methods=True))