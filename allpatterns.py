import re


test_patch = '''
+aws_access_key_id = "AKIA1234567890ABCDEF"
+password = "admin123"
+-----BEGIN PRIVATE KEY-----
+FAKE_TEST_DATA_ONLY
+-----END PRIVATE KEY-----
+result = eval(user_input)
+query = "SELECT * FROM users WHERE name = '%s'" % username
'''


patterns = {
    "aws-key": re.compile(
        r"^\+(?!\+\+).*\bAKIA[0-9A-Z]{16}\b.*$",
        re.MULTILINE,
    ),
    "password": re.compile(
        r"^\+(?!\+\+)\s*"
        r"(?:(?:const|let|var)\s+)?"
        r"(?:password|passwd|pwd)"
        r"\s*[:=]\s*[\"'][^\"'\r\n]+[\"']",
        re.MULTILINE | re.IGNORECASE,
    ),
    "private-key": re.compile(
        r"^\+\s*-----BEGIN "
        r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s*$",
        re.MULTILINE,
    ),
    "eval": re.compile(
        r"^\+(?!\+\+).*\beval\s*\(",
        re.MULTILINE,
    ),
    "sql-format": re.compile(
        r"^\+(?!\+\+).*"
        r"[\"']\s*(?:SELECT|INSERT|UPDATE|DELETE)\b"
        r".*[\"']\s*%\s*.+$",
        re.MULTILINE | re.IGNORECASE,
    ),
}


for rule_name, pattern in patterns.items():
    matches = list(pattern.finditer(test_patch))

    print(f"{rule_name}: {len(matches)} match(es)")

    for match in matches:
        print(" ", match.group())