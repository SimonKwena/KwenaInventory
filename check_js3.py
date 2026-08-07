import urllib.request, re

req = urllib.request.Request('http://localhost:8000/static/js/site.js?v=44', headers={'Host': 'localhost:8000'})
resp = urllib.request.urlopen(req, timeout=5)
content = resp.read().decode()

lines = content.split('\n')
issues = []

for i in range(len(lines)):
    line = lines[i].strip()
    if line.startswith('const ') or line.startswith('let '):
        var_match = re.match(r'(?:const|let)\s+(\w+)', line)
        if var_match:
            var_name = var_match.group(1)
            indent = len(lines[i]) - len(lines[i].lstrip())
            for j in range(i+1, min(i+100, len(lines))):
                next_line = lines[j].strip()
                if next_line.startswith('function ') or next_line.startswith('}'):
                    break
                next_indent = len(lines[j]) - len(lines[j].lstrip())
                if (next_line.startswith('const ') or next_line.startswith('let ')) and next_indent == indent:
                    next_match = re.match(r'(?:const|let)\s+(\w+)', next_line)
                    if next_match and next_match.group(1) == var_name:
                        issues.append(f'Line {i+1} and {j+1}: duplicate "{var_name}" at indent {indent}')

for issue in issues[:20]:
    print(issue)
print(f'Total potential duplicates: {len(issues)}')
