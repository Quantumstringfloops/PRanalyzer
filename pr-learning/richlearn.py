from rich import print
from rich import pretty
from rich.panel import Panel
from rich.color import Color

print(Panel.fit("[bold yellow]Hi, I'm a Panel", border_style="red"))
pretty.install()
["Rich and pretty", True]