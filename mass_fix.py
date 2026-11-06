import os

# Путь к папке, где лежат твои файлы
root_dir = r'C:\oko\src'

# Что ищем и на что меняем
# Ищем: 'from src.'
# Меняем: 'from '
search_str = 'from src.'
replace_str = 'from '

for root, dirs, files in os.walk(root_dir):
    for file in files:
        if file.endswith('.py'):
            filepath = os.path.join(root, file)
            
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            if search_str in content:
                new_content = content.replace(search_str, replace_str)
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                print(f'Исправил файл: {filepath}')

print('Всё готово, дружище.')