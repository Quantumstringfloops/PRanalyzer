import re 

text = "123 files have been changed 5 major edits"
result = re.compile("r\d+") 
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
# print(match.groupdict())