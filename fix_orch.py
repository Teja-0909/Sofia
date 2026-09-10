import sys

file_path = r'c:\Games\Alya\app\orchestrator.py'
with open(file_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
in_tools_loop = False

for i, line in enumerate(lines):
    if line.startswith('        for call in tool_calls:'):
        new_lines.append('        async with _tool_lock:\n')
        new_lines.append('            for call in tool_calls:\n')
        in_tools_loop = True
        continue
    
    if in_tools_loop:
        if line.strip() == '' or line.startswith('            ') or line.startswith('        #') or line.startswith('                '):
            if line.startswith('                args = json.loads(func["arguments"])'):
                new_lines.append('                    args = func.get("arguments", "{}")\n')
                new_lines.append('                    if isinstance(args, str):\n')
                new_lines.append('                        try:\n')
                new_lines.append('                            args = json.loads(args)\n')
                new_lines.append('                        except Exception:\n')
                new_lines.append('                            args = {}\n')
                new_lines.append('                    if not isinstance(args, dict):\n')
                new_lines.append('                        args = {}\n')
            else:
                if line.strip() == '':
                    new_lines.append(line)
                else:
                    new_lines.append('    ' + line)
        else:
            in_tools_loop = False
            new_lines.append(line)
    else:
        new_lines.append(line)

with open(file_path, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print('Done!')
