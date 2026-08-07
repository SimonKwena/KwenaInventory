import re

with open('static/js/site.js', 'r') as f:
    content = f.read()

lines = content.split('\n')
declarations = {}
for i, line in enumerate(lines, 1):
    stripped = line.strip()
    if stripped.startswith('const ') or stripped.startswith('let '):
        match = re.match(r'(?:const|let)\s+(\w+)', stripped)
        if match:
            name = match.group(1)
            if name in declarations:
                print(f'Duplicate declaration of "{name}" at line {i} (first at line {declarations[name]})')
            else:
                declarations[name] = i

print("Done checking")
