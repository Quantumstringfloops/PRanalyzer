import re 

text = "123 files have been changed 5 major edits"
# shows 

# for match in result.finditer(text):
#     print(match.group())


# search and findal

match = re.search(r"\d+", text) #returns only the first match
numbers = re.findall(r"\d+", text)
print(numbers)
print(match.group())


# pattern = re.compile(
#     r"@@ -(?P<old_line>\d+) "
#     r"\+(?P<new_line>\d+) @@"
# )
# match = pattern.search("@@ -10 +12 @@")
patch = """ context
+first added line
 context
+second added line"""

try:
    pattern = re.compile(
    r"^+.*$",
    re.MULTILINE,
    )

    print(pattern.findall(patch))

except Exception as e:
    print(e)



def find_all_todos(patch):
    TODO_PATTERN = re.compile(
        r"^.*\b(?:TODO|FIXME)\b.*$",
        re.MULTILINE | re.IGNORECASE,
    )
    result = TODO_PATTERN.findall(patch)
