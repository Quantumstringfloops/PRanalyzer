def greet(name):
    return f"Hello, {name}"


def calculate_total(numbers):
    total = 0

    for number in numbers:
        total += number

    return total


class Calculator:
    def multiply(self, first, second):
        return first * second