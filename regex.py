import re 

text = "123 files have been changed 5 major edits"
result = re.compile("r\d+")

print(result)