def validate_user(user):
    if not user:
        return False

    if user.is_blocked:
        return False

    return True


def calculate_discount(total, premium):
    if premium:
        return total * 0.8

    return total