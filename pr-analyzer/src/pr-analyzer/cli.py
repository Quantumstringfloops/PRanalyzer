import argparse
import typer
from rich import print



app = typer.Typer(help="This is PR analyzer. check out --help for more details")

@app.command()
def greet(name: str) -> None:
    message = f"Hello{name}"
    typer.echo(message)
if __name__ == "__main__":
    app()