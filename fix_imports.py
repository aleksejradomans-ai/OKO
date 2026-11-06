import os
import re

def fix_imports():
    src_dir = 'src'
    # Целевые модули, которые нужно обернуть в src
    # Добавь сюда свои модули, если их больше
    modules = ['patch_applier', 'static_analyzer', 'sandbox_environment', 'path_guard', 'memory_indexer']
    
    for root, _, files in os.walk(src_dir):
        for file in files:
            if file.startswith('test_') and file.endswith('.py'):
                path = os.path.join(root, file)
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()

                for mod in modules:
                    # Ищем импорты, которые начинаются не с src.
                    pattern = rf'(from\s+){mod}(\s+import)'
                    replacement = r'\1src.' + mod + r'\2'
                    if re.search(pattern, content):
                        content = re.sub(pattern, replacement, content)
                        print(f"Исправлен импорт в {path}")
                
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(content)

if __name__ == "__main__":
    fix_imports()