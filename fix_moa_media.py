import sys

file_path = r'c:\Games\Alya\app\orchestrator.py'
with open(file_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
for line in lines:
    if 'router_msg = [{' in line and 'Forebrain' in line:
        new_lines.append(line)
        new_lines.append('            if current_messages and "image_bytes" in current_messages[-1]:\n')
        new_lines.append('                router_msg[-1]["image_bytes"] = current_messages[-1]["image_bytes"]\n')
        new_lines.append('                router_msg[-1]["media_bytes"] = current_messages[-1].get("media_bytes")\n')
        new_lines.append('                router_msg[-1]["mime_type"] = current_messages[-1].get("mime_type", "image/jpeg")\n')
    elif 'spec_msg = [{' in line and 'Specialist:' in line:
        new_lines.append(line)
        new_lines.append('                if current_messages and "image_bytes" in current_messages[-1]:\n')
        new_lines.append('                    spec_msg[-1]["image_bytes"] = current_messages[-1]["image_bytes"]\n')
        new_lines.append('                    spec_msg[-1]["media_bytes"] = current_messages[-1].get("media_bytes")\n')
        new_lines.append('                    spec_msg[-1]["mime_type"] = current_messages[-1].get("mime_type", "image/jpeg")\n')
    elif 'synth_msg = [{' in line and 'synth_content' in line:
        new_lines.append(line)
        new_lines.append('            if current_messages and "image_bytes" in current_messages[-1]:\n')
        new_lines.append('                synth_msg[-1]["image_bytes"] = current_messages[-1]["image_bytes"]\n')
        new_lines.append('                synth_msg[-1]["media_bytes"] = current_messages[-1].get("media_bytes")\n')
        new_lines.append('                synth_msg[-1]["mime_type"] = current_messages[-1].get("mime_type", "image/jpeg")\n')
    else:
        new_lines.append(line)

with open(file_path, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)
print('Done preserving media in MoA!')
