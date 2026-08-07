import re

with open('static/js/site.js', 'r') as f:
    lines = f.read().split('\n')

# Simple brace-based scope tracking
brace_depth = 0
declarations = {}  # key = "depth:name" -> line number

for i, line in enumerate(lines, 1):
    stripped = line.strip()
    
    # Skip comments and empty lines
    if not stripped or stripped.startswith('//'):
        continue
    
    # Track brace depth changes
    for ch in stripped:
        if ch == '{':
            brace_depth += 1
        elif ch == '}':
            brace_depth -= 1
    
    # Check const/let declarations at this depth
    if stripped.startswith('const ') or stripped.startswith('let '):
        match = re.match(r'(?:const|let)\s+(\w+)', stripped)
        if match:
            name = match.group(1)
            key = f"{brace_depth}:{name}"
            if key in declarations:
                print(f'SYNTAX ERROR: "{name}" declared twice at brace depth {brace_depth}: line {i} and line {declarations[key]}')
            else:
                declarations[key] = i

print("Check complete")
