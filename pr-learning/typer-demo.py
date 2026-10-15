"""
Standalone Typer playground. No project logic here on purpose —
just the mechanics. Run each command below and read the --help
output typer generates for you automatically.

Install:
    pip install typer

Try:
    python typer_demo.py --help
    python typer_demo.py greet --help
    python typer_demo.py greet Alice
    python typer_demo.py greet Alice --shout
    python typer_demo.py greet Alice --times 3
    python typer_demo.py divide 10 2
    python typer_demo.py divide 10 0
    python typer_demo.py set-age Bob 25
    python typer_demo.py set-age Bob -5
"""

import typer

app = typer.Typer(help="A playground app to learn typer, not a real tool.")


# ── 1. Positional ARGUMENT vs. --option ──────────────────────────────
# A plain type-hinted parameter becomes a required positional argument.
# A parameter with a default value becomes an --option automatically.
@app.command()
def greet(
    name: str,  # positional argument: `greet Alice`
    shout: bool = False,  # becomes --shout / --no-shout flag
    times: int = 1,  # becomes --times 3
) -> None:
    """Greet someone, optionally shouting, optionally more than once."""
    message = f"Hello, {name}!"
    if shout:
        message = message.upper()
    for _ in range(times):
        typer.echo(message)


# ── 2. typer.Argument / typer.Option for extra metadata ──────────────
# Use these when you want help text, defaults with explanation,
# or to mark something required/optional explicitly.
@app.command()
def divide(
    numerator: float = typer.Argument(..., help="The number being divided."),
    denominator: float = typer.Argument(..., help="The number to divide by."),
) -> None:
    """Divide two numbers. Demonstrates typer.Exit on a bad state."""
    if denominator == 0:
        typer.echo("Error: cannot divide by zero.", err=True)
        raise typer.Exit(code=1)  # non-zero exit code, clean message, no traceback
    typer.echo(f"{numerator} / {denominator} = {numerator / denominator}")


# ── 3. Custom validation via a callback function ──────────────────────
# This is the typer equivalent of argparse's custom `type=` function.
def validate_age(value: int) -> int:
    if value < 0:
        raise typer.BadParameter("Age cannot be negative.")
    return value


@app.command()
def set_age(
    name: str,
    age: int = typer.Argument(..., callback=validate_age),
) -> None:
    """Demonstrates per-parameter validation via callback=."""
    typer.echo(f"{name} is {age} years old.")


# ── 4. An --option with a short flag and a default, shown in --help ──
@app.command()
def power(
    base: int,
    exponent: int = typer.Option(2, "--exp", "-e", help="Exponent to raise base to."),
) -> None:
    """Demonstrates an option with both a long and short flag name."""
    typer.echo(f"{base} ** {exponent} = {base ** exponent}")


if __name__ == "__main__":
    app()