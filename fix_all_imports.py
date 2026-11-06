import os
import re

def fix_all_imports():
    # Список всех модулей проекта
    modules = [
        'patch_applier', 'static_analyzer', 'sandbox_environment', 
        'path_guard', 'memory_indexer', 'snapshot_manager'
    ]
    
    # Рекурсивный поиск во всех .py файлах в src/
    for root, _, files in os.walk('src'):
        for file in files:
            if file.endswith('.py'):
                path = os.path.join(root, file)
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()

                for mod in modules:
                    # Ищем: "from mod" или "import mod"
                    # Исключаем, если уже есть "from src.mod"
                    # Заменяем только если модуль упоминается как пакет
                    
                    # Pattern: from mod (не from src.mod)
                    pattern = rf'(?<!src\.)\b{mod}\b'
                    
                    # Логика: если строка начинается с "from" или "import" и содержит наш модуль
                    # Простая замена:
                    content = re.sub(rf'from\s+{mod}', rf'from src.{mod}', content)
                    content = re.sub(rf'import\s+{mod}', rf'import src.{mod}', content)
                
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)
                    print(f"Обработан файл: {path}")

if __name__ == "__main__":
    fix_all_imports()