# Публикация репозитория на GitHub / GitLab

Инструкция для агента. Версия для человека: [ДЛЯ-ЧЕЛОВЕКА.md](ДЛЯ-ЧЕЛОВЕКА.md).

## Правила

- Значения секретов не выводить в чат, отчёты, журналы, команды, файлы, URL remote.
- Без явной команды пользователя не делать: push в `main` и в публичный remote, смену видимости, изменение remote,
  изменение защиты веток, переписывание истории.
- Никогда: `push --force`, `--all`, `--mirror`, `reset --hard`, `gh repo delete`, `gh auth token`, удаление веток на сервере.
- Токены у пользователя в чате не запрашивать: вход через `gh auth login` / `glab auth login` выполняет он сам.
- Рабочие файлы проекта не удалять; предлагать перенос.

## Подготовка

Не публикуется:
- секреты: `.env*` (кроме `.env.example`), вебхуки `…/rest/<id>/<токен>/`, `orb_pk_…`, `whsec_…`, `ghp_…`, `glpat-…`, `sk-…`, `*.pem`, `*.key`, `credentials*.json`;
- данные: `*.sqlite3`, `*.db`, `data/`, `backup-*`, `*.sql`, `*.csv`, `*.xlsx`, `*.log`, `logs/`;
- реальные телефоны, ID сделок и диалогов, домены тестовых порталов, имена заказчиков — в коде, тестах, docs;
- рабочие заметки (журнал, план, ТЗ, переписка), медиа (`*.mov`, `*.mp4`, скриншоты с интерфейсами);
- `.venv/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `mutants/`, `node_modules/`, `.DS_Store`, `.idea/`, `.vscode/`.

Сделать:
1. Заметки, `.env`, медиа — вне каталога репозитория (уровнем выше).
2. `.gitignore` и `.dockerignore` закрывают всё перечисленное выше; `!.env.example`.
3. `.env.example` — те же ключи, пустые значения, комментарии.
4. Секреты в коде только из окружения или настроек в БД; в тестах — заглушки (`+7999…`, `p.bitrix24.ru/rest/1/x/`, `orb_pk_secret123456`).
5. Секреты не пишутся в логи (в т. ч. URL в логах `httpx`/`requests`/`urllib3` — уровень `WARNING`); в UI — маска.
6. Проверить: `git check-ignore -v .env data`; `git ls-files | grep -Ei 'env|sqlite|\.db|data/|key|secret|token|\.log'` → только `.env.example`.

## Доступ

Агенту — отдельный ключ на один репозиторий. Личный SSH-ключ и classic token — нет.

SSH-ключ репозитория (deploy key):
```bash
ssh-keygen -t ed25519 -C "agent <репо>" -f ~/.ssh/id_ed25519_<репо> -N ""
cat ~/.ssh/id_ed25519_<репо>.pub   # отдать пользователю → Deploy keys, write access
```
`~/.ssh/config`:
```
Host <хост>-<репо>
  HostName <github.com | gitlab-хост>
  User git
  IdentityFile ~/.ssh/id_ed25519_<репо>
  IdentitiesOnly yes
```
```bash
git remote set-url <remote> git@<хост>-<репо>:<владелец>/<репо>.git
ssh -T git@<хост>-<репо>
```

Для PR/MR: fine-grained token (GitHub: один репозиторий, Contents + Pull requests RW, 30–90 дн.) или project access
token (GitLab: Developer, `api`, `read_repository`, `write_repository`, 30–90 дн.). Хранение — связка ключей через
`gh auth login` / `glab auth login --hostname <хост>`; проверка `gh auth status` / `glab auth status`.

`.claude/settings.json` в репозитории:
```json
{
  "permissions": {
    "allow": [
      "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
      "Bash(git add:*)", "Bash(git commit:*)", "Bash(git switch:*)", "Bash(git fetch:*)",
      "Bash(gitleaks:*)", "Bash(gh pr view:*)", "Bash(gh pr list:*)", "Bash(glab mr view:*)"
    ],
    "ask": [
      "Bash(git push:*)", "Bash(gh pr create:*)", "Bash(glab mr create:*)",
      "Bash(gh repo edit:*)", "Bash(git remote:*)", "Bash(git filter-repo:*)"
    ],
    "deny": [
      "Bash(git push --force:*)", "Bash(git push -f:*)", "Bash(git push --mirror:*)", "Bash(git push --all:*)",
      "Bash(git reset --hard:*)", "Bash(gh repo delete:*)", "Bash(gh auth token:*)",
      "Read(./.env)", "Read(../.env)"
    ]
  }
}
```

Порядок работы: `git fetch` → `git switch -c <тема>` от свежего `main` → изменения, тесты → «Проверка» по коммитам
ветки → `git push -u origin <тема>` → `gh pr create` / `glab mr create` → слияние делает пользователь.

## Проверка

Охват: все коммиты, которые уйдут; при первой публикации — вся история всех веток (`--all`).

```bash
git status
git log --oneline <публичный-remote>/main..main

# значения из .env — печатать только факт и коммит
sed -n 's/^[A-Z_]*=//p' <путь>/.env | tr -d '"' | awk 'length>=8' | while IFS= read -r v; do
  git log --all -F -S"$v" --format=%h | sed 's/^/НАЙДЕН СЕКРЕТ ИЗ .env В /'
done

# шаблоны ключей и «свои» строки по добавленным строкам диффов
for p in 'rest/[0-9]+/[a-z0-9]{10,}' 'orb_pk_[A-Za-z0-9]{12,}' 'whsec_[A-Za-z0-9]{12,}' 'ghp_|glpat-|sk-[A-Za-z0-9]{20,}' \
         'BEGIN [A-Z ]*PRIVATE KEY' '\+7 ?9[0-9]{9}' '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}' '/Users/' \
         '<домен портала>' '<имена>' '<закрытый хост>'; do
  printf '%-40s %s\n' "$p" "$(git log -p --all --format= -E -G"$p" | grep -E '^\+' | grep -cE -- "$p")"
done
git log -p --all --format='@@ %h' -E -G'<шаблон>' | grep -E '^@@|^\+.*<шаблон>'   # разбор совпадений

gitleaks git -v --log-opts="--all"   # если установлен (brew install gitleaks); версии <8.19: gitleaks detect
git log --all --format='%an <%ae>' | sort -u
git branch -a; git tag
git ls-files | grep -Ei '\.(png|jpe?g|gif|mov|mp4|pdf)$'   # медиа — пользователю на просмотр
```

Каждое совпадение классифицировать: секрет / персональные данные / заглушка. Заглушки перечислить отдельно.

Отчёт:
- вердикт: можно / нельзя;
- найденное: файл, коммит, тип — без значения;
- что станет публичным и требует решения пользователя (почты авторов, упоминания, медиа);
- исправления; при секрете в истории — сначала «перевыпустить <ключ>», затем план из «Утечка».

## Выгрузка

Только после вердикта «можно» и команды пользователя.

```bash
git push <публичный-remote> main
```

Смена видимости репозитория — пользователь в веб-интерфейсе (или `gh repo edit --visibility public
--accept-visibility-change-consequences` по его команде).

## Утечка

1. Пользователь перевыпускает ключ (Битрикс: входящий вебхук заново; Орбита: кабинет → Разработчикам). До этого историю не трогать.
2. Резервная копия: `git clone --mirror <путь> <scratchpad>/backup.git`.
3. План пользователю, после «да»:
   ```bash
   brew install git-filter-repo
   git filter-repo --replace-text <файл-замен>   # строки вида: старое_значение==>***REMOVED***; файл вне репозитория, удалить после
   git filter-repo --invert-paths --path <файл>  # если файл целиком
   git push <remote> main --force                # только после временного снятия защиты main пользователем
   ```
4. Повторить «Проверка».
5. GitHub: пользователь просит support.github.com удалить закэшированные коммиты.
6. Запись в журнал проекта: что, когда, что перевыпущено.
