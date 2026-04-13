import os
import shutil
import json
import tempfile
from datetime import datetime
from typing import Dict, Any, List
import git
from git import Repo
from pydriller import Repository
from openai import OpenAI


class RepositoryAnalyzerAgent:
    """
    Агент для анализа качества кода в Git-репозитории.
    """

    def __init__(self, api_key: str, model: str = "openrouter/free", base_url: str = "https://openrouter.ai/api/v1"):
        """
        Инициализация агента.
        входные параметры:
            api_key: API-ключ для доступа LLM
            model: Идентификатор модели
            base_url: Базовый URL API
        """
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url
        )
        self.model = model

    def analyze(self, repo_url: str, max_commits_meta: int = 30, max_commits_diff: int = 15) -> Dict[str, Any]:
        """
        Основная функция для анализа репозитория
        входные:
            :param repo_url:
            :param max_commits_meta:
            :param max_commits_diff:
        выходыне:
            json c информацией по репозиторию
        """
        # Создаём временную папку в текущей директории
        tmpdir = tempfile.mkdtemp(prefix='repo_', dir=os.getcwd())
        # Реализуем простый сценарий обработки по архитектуре Chain - простая последовательность
        # 1. Клонируем репозиторий
        # 2. Собираем все метаданные о коммитах
        # 3. Собирает все что только можно из коммитов - логи, диф и тд
        # 4. формируем промт и отправляем промт ЛЛМ
        # 5. Получаем Json с информацией по репозиторию и отображаем отчет
        # 6. Удаляем времененный файл репозитория
        try:
            print(f"Клонирование репозитория {repo_url} в {tmpdir}")
            try:
                repo = Repo.clone_from(repo_url, tmpdir)
            except git.GitCommandError as e:
                raise RuntimeError(f"Не удалось клонировать репозиторий: {e}")

            print("Сбор метаданных коммитов")
            commits_meta = self._get_commits_meta(repo, max_commits=max_commits_meta)

            print("Детальный анализ изменений в коммитах")
            commits_with_diffs = self._get_commits_with_diffs(tmpdir, max_commits=max_commits_diff)

            print("Формирование промпта для LLM")
            prompt = self._create_analysis_prompt(repo_url, commits_meta, commits_with_diffs)

            print("Отправка запроса в LLM через API")
            analysis_result = self._call_openrouter_api(prompt)

            required_keys = ["criteria", "summary", "debt_by_type", "debt_by_file"]
            if not all(key in analysis_result for key in required_keys):
                raise ValueError("Модель вернула неполный JSON")

            self._save_result_to_json(analysis_result, repo_url)
            return analysis_result
        finally:
            # Удаляем временную директорию
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _get_commits_meta(self, repo: Repo, max_commits: int = 30) -> List[Dict[str, str]]:
        """
        Извлекает базовую информацию о последних коммитах.
        входные:
            repo: репозиторий
            max_commits: число забираемых коммитов, нужно в виду несовершенства бесплатной модели
        выходные:
            список коммитов с названиями, авторами и временем
        """
        commits = []
        for commit in repo.iter_commits(max_count=max_commits):
            commits.append({
                "hash": commit.hexsha,
                "author": str(commit.author),
                "date": commit.committed_datetime.isoformat(),
                "message": commit.message.strip()
            })
        return commits

    def _get_commits_with_diffs(self, local_path: str, max_commits: int = 15) -> List[Dict[str, Any]]:
        """
        Извлекает подробную информацию о коммитах.
        входные:
            local_path: путь до скаченного репозитория
            max_commits: число забираемых коммитов, нужно в виду несовершенства бесплатной модели
        выходные:
            список коммитов с подробной информацией
        """
        detailed_commits = []
        count = 0
        try:
            for commit in Repository(local_path).traverse_commits():
                if count >= max_commits:
                    break
                try:
                    commit_data = {
                        "hash": commit.hash,
                        "author": commit.author.name,
                        "date": commit.author_date.isoformat(),
                        "msg": commit.msg,
                        "files": []
                    }
                    for modified_file in commit.modified_files:
                        try:
                            commit_data["files"].append({
                                "filename": modified_file.filename,
                                "change_type": modified_file.change_type.name,
                                "added_lines": modified_file.added_lines,
                                "deleted_lines": modified_file.deleted_lines,
                                "source_code_before": self._truncate_code(modified_file.source_code_before),
                                "source_code_after": self._truncate_code(modified_file.source_code)

                            })
                        except Exception as file_err:
                            print(f"Ошибка при обработке файла {modified_file.filename}: {file_err}")
                            continue
                    detailed_commits.append(commit_data)
                    count += 1
                except Exception as commit_err:
                    print(f"Пропуск коммита {commit.hash} из-за ошибки: {commit_err}")
                    continue
        except Exception as e:
            print(f"Критическая ошибка PyDriller: {e}. Возвращаем частичные данные.")
        return detailed_commits

    # Обрезка кода, оставляющая n строк - бесплатная модель мало токенов, поэтому необходимо
    @staticmethod
    def _truncate_code(code, max_lines: int = 100):
        """Обрезает длинные фрагменты кода, оставляя первые max_lines строк."""
        if code is None:
            return None
        lines = code.splitlines()
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + "\n... (truncated)"
        return code

    def _create_analysis_prompt(self, repo_url: str, commits_meta: List[Dict], commits_with_diffs: List[Dict]) -> str:
        """
        Формирует подробный промпт с роли агента, метрик анализа, требуемом формате ответа

        входные:
            repo_url: ссылка на репозиторий
            commits_meta: метаинформация о коммитах из _get_commits_meta
            commits_with_diffs: подробная информация о коммитах их _get_commits_with_diffs
        выходные:
            подробный промт для LLM
        """
        commits_meta_str = json.dumps(commits_meta, indent=2, ensure_ascii=False)
        diffs_str = json.dumps(commits_with_diffs, indent=2, ensure_ascii=False)

        return f"""
        Ты — эксперт по анализу качества кода. Твоя задача — провести всестороннюю оценку репозитория {repo_url} на основе предоставленных данных.
        
        ### Доступные данные:
        1. Метаданные о коммитах:
        {commits_meta_str}
        
        2. Детальная информация об изменениях в коде (первые {len(commits_with_diffs)} коммитов с диффами):
        {diffs_str}
        
        ### Требования к анализу:
        Оцени репозиторий по следующим критериям (для каждого укажи оценку от 1 до 10 и краткий комментарий):
        
        1. **Читаемость кода** – насколько код легко читать, понятны ли имена переменных/функций, есть ли необходимые комментарии.
        2. **Тестирование** – наличие тестов, оценка покрытия (по косвенным признакам, например, наличие папки tests, частота изменений тестовых файлов).
        3. **Безопасность** – отсутствие захардкоженных ключей, паролей, потенциальных уязвимостей.
        4. **Документация** – наличие README, docstring'ов, комментариев к публичным API.
        5. **Качество сообщений коммитов** – осмысленность и структурированность сообщений (например, соответствие Conventional Commits).
        6. **Размер коммитов** – оптимальность размера изменений (слишком большие коммиты могут усложнять код-ревью).
        7. **Соответствие PEP8** – соблюдение стандарта оформления кода Python (имена, отступы, длина строк).
        8. **Часто изменяемые файлы** – выявление файлов, которые правятся чаще всего (признак нестабильности или плохой архитектуры).
        9. **Актуальность зависимостей** – оценка устаревания используемых библиотек (на основе названий и версий в файлах зависимостей, если они присутствуют в диффах).
        10. **Потенциально опасные фрагменты кода** - подсчет количества фрагментов кода, которые могут напрямую сказаться на его выполнении - (Пример: есть угроза бесконечного цикла)
        
        На основе анализа также сформируй:
        - **debt_by_type** – распределение технического долга по типам (названия типов и количество найденных фактов). Используй следующие категории: "Дублирование", "Длинные методы", "Отсутствие тестов", "Нарушение стиля", "Плохие имена", "Устаревшие библиотеки", "Большие коммиты", "Часто изменяемый код".
        - **debt_by_file** – топ файлов с наибольшим количеством проблем технического долга (имя файла и число проблем).
        
        ### Формат ответа:
        Ответ должен быть **строго в формате JSON** со следующей структурой:
        {{
          "criteria": [
            {{ "name": "...", "score": N, "comment": "..." }},
            ...
          ],
          "summary": "Краткое итоговое резюме (2-3 предложения).",
          "name": "Название репозитория",
          "last_commit": " Время последнего коммита в формате %Y-%m-%d %H:%M",
          "language": "Основной язык репозитория",
          "debt_by_type": {{
            "labels": ["...", ...],
            "values": [N, ...]
          }},
          "debt_by_file": {{
            "labels": ["...", ...],
            "values": [N, ...]
          }}
        }}
        
        Убедись, что все поля заполнены, оценки корректны, а JSON валиден. 
"""

    def _call_openrouter_api(self, prompt: str) -> Dict[str, Any]:
        """
        Вызов самой LLM через API
        Входные:
            :param prompt: - промт для LLM
        Выходные:
            json файл с информацией
        """
        try:
            client = OpenAI(
                base_url="https://openrouter.ai/api/v1",  # url для модели
                api_key="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",  # API ключ из кабинета
                default_headers={
                    "HTTP-Referer": "http://127.0.0.1:5000/",
                    "X-Title": "Repo Quality Analyzer"
                }
            )
            response = client.chat.completions.create(
                model="openrouter/free",  # выбираем бесплатную модель
                messages=[
                    {"role": "system",
                     "content": "Ты — ИИ-ассистент для анализа качества кода. Ответ должен быть строго в формате JSON."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.0  # Указываем температуру близкую к нулю, для детерменированности результата
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            raise RuntimeError(f"Ошибка при вызове OpenRouter API: {e}")

    def _save_result_to_json(self, result: Dict[str, Any], repo_url: str) -> None:
        """
        Сохраняет результат анализа в JSON-файл в папке 'reports'.

        входные:
            result: словарь с информацией
            repo_url: ссылка на репозиторий
        выходные:
            ничего, просто сохраняет json результат в отдельных файлик
        """
        # Создаём папку reports, если её нет
        reports_dir = "reports"
        os.makedirs(reports_dir, exist_ok=True)

        # Формируем имя файла с учётом названия репозитория и времени
        repo_name = repo_url.rstrip('/').split('/')[-1].replace('.git', '')
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{repo_name}_{timestamp}.json"
        filepath = os.path.join(reports_dir, filename)

        output = {
            "repo_url": repo_url,
            "analysis_date": datetime.now().isoformat(),
            "result": result
        }
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        print(f"Результат сохранён в {filepath}")
