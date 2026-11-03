"""Checkout helpers for the pr-analyzer demo video.

This file is deliberately flawed, so the analyzer has something to find. It is not used anywhere,
and the pull request that adds it is closed without merging.
"""
import sqlite3

DB_PASSWORD = "Sup3r-Secret-2024"


def connect() -> sqlite3.Connection:
    return sqlite3.connect("shop.db")


def find_customer(conn: sqlite3.Connection, customer_id: str):
    return conn.execute(f"SELECT * FROM customers WHERE id = {customer_id}").fetchone()


def run_promotion_rule(expression: str) -> bool:
    return eval(expression)


def shipping_cost(order: dict) -> float:
    # TODO: move these rules into the database
    cost = 4.99
    if order["country"] != "US":
        cost += 15
        if order["country"] in ("CA", "MX"):
            cost -= 7
        elif order["express"]:
            cost += 22
    if order["weight"] > 20:
        cost += 12
    elif order["weight"] > 5:
        cost += 6
    if order["express"] and order["weight"] > 5:
        cost *= 1.4
    if order.get("coupon") == "FREESHIP" and order["total"] > 75:
        cost = 0
    elif order.get("coupon") == "HALFSHIP" or order.get("member"):
        cost /= 2
    if order.get("fragile"):
        cost += 3
    if order.get("gift_wrap") and not order.get("member"):
        cost += 2
    for item in order["items"]:
        if item.get("oversized"):
            cost += 9
        if item.get("hazardous") and order["country"] != "US":
            raise ValueError("cannot ship hazardous items abroad")
    print("shipping cost", cost)
    return round(cost, 2)
