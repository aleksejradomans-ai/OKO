import os

root_dir = r'C:\oko\src'
search_str = 'from '
replace_str = 'from src.'

for root, dirs, files in os.walk(root_dir):
    for file in files:
        if file.endswith('.py'):
            filepath = os.path.join(root, file)
            with open(filepath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            
            new_lines = []
            for line in lines:
                # Меняем только те строки, которые явно относятся к нашим модулям
                # чтобы не сломать стандартные библиотеки (типа from os import...)
                if line.startswith('from ') and not line.startswith('from src.') and not line.startswith('from __future__'):
                    new_lines.append(line.replace('from ', 'from src.'))
                else:
                    new_lines.append(line)
            
            with open(filepath, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            print(f'Откатил файл: {filepath}')