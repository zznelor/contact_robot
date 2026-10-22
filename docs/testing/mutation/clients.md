# Выжившие мутанты mutmut: клиенты (bitrix.py, orbita_api.py, orbita.py, db.py)

Прогон: mutmut 3.8.0 на коммите `befbd6b`, весь `src/first_contact/`. Повторно не запускался (тест-гайд,
часть II); каждый закрытый пробел проверен узким прогоном файла тестов группы на копии дерева с применённым
мутантом — тест красный, на настоящем коде — зелёный. Эталоны — спецификация Орбиты
`docs/orbita-openapi-2026-10-20.json`, статьи Битрикса (`/workspaces/orbita/docs/research/bitrix24/rest-docs/`),
`docs/РАЗРАБОТЧИКУ.md`.

## Сводка

| Вердикт | Мутантов |
|---|---|
| пробел — закрыт тестом | 101 |
| эквивалентный | 70 |
| непродуктивный | 17 |
| всего на разбор (`review-clients.txt`) | 188 |
| признаны эквивалентными автоматически (`equiv-clients.json`) | 179, из них 74 на деле — пробелы, теперь убиты (см. последний раздел) |

## Разбор

Имена — без префикса `first_contact.`, номера — `__mutmut_N`.

### bitrix.py

| Функция | Мутанты | Вердикт | Обоснование | Чем убит |
|---|---|---|---|---|
| `_text` | 2, 3 | пробел | `rstrip(None)` / `lstrip` не снимают точку: «Not found.» и «Access denied.» с пустым кодом (дословно из статьи `crm.deal.contact.items.get`) перестают быть «сделки нет» / «повтор не поможет» | `test_deal_contacts_errors_from_doc` |
| `normalize_phone` | 25 | пробел | `digits[1]`: мобильный без кода страны на 9 со второй цифрой не 9 (916…) не получает +7 | `test_contact_phones_normalized` |
| `normalize_phone` | 31, 32 | непродуктивный | граница «10 цифр» для номера не на 9 требованием не задана; см. «Дефекты продукта», п. 1 | — |
| `Bitrix.__init__` | 4, 5 | пробел | адрес вебхука со слешем на конце даёт `…/x//crm.deal.get.json` | `test_method_called_at_webhook_address` |
| `Bitrix.__init__` | 8 | пробел | таймаут не доходит до запроса — зависший портал держит проход бесконечно | `test_timeout_from_settings_on_every_request` |
| `Bitrix.__init__` | 1 | непродуктивный | таймаут по умолчанию 30 → 31 с: число требованием не задано | — |
| `Bitrix._raw` | 27, 29 | пробел | без таймаута / таймаут httpx по умолчанию (5 с) вместо настроенного | `test_timeout_from_settings_on_every_request` |
| `Bitrix._raw` | 45, 47 | пробел | 3xx без Location (`is_redirect` в httpx 0.28 — любой 3xx): `"/rest/" in None` → TypeError вместо ошибки Битрикса | `test_not_bitrix_answer_is_bitrix_error` |
| `Bitrix._raw` | 52, 54, 60 | пробел | в текст ошибки уходит весь Location с путём вебхука — его кодом (секретом); текст попадает в журнал и панель | `test_portal_moved_shows_new_address` (утверждение `"/rest/" not in`) |
| `Bitrix._raw` | 78 | пробел | JSON-объект без `result` и `error` проходит проверку → KeyError из `call()` вместо «ответил не Битрикс24 REST» | `test_not_bitrix_answer_is_bitrix_error` |
| `Bitrix._raw` | 114 | пробел | срок OPERATION_TIME_LIMIT не берётся из этого ответа (РАЗРАБОТЧИКУ.md: «из этого ответа или из последнего успешного»). ПРЕДПОЛОЖЕНИЕ: что ответ-ошибка несёт `time.operating_reset_at`, статьи Битрикса не показывают — эталон взят из `docs/РАЗРАБОТЧИКУ.md` | `test_operation_time_limit_waits_for_reset_from_this_answer` |
| `Bitrix._raw` | 13, 122, 128, 139, 72, 74 | эквивалентный | только текст сообщения исключения (§5: формулировка не контракт) | — |
| `Bitrix._raw` | 14, 15, 17, 18, 19 | эквивалентный | атрибуты `method` / `code` ошибки «приостановлено до»: `retry_at` сохранён, а потребители (`Worker`, возврат этапа) смотрят его первым и выходят; `permanent` / `deal_missing` для кода лимита ложны при любых `method` / `code` | — |
| `Bitrix._raw` | 37, 39, 62, 64, 73, 75 | эквивалентный | `method` ошибок «нет связи», «переадресация», «не Битрикс»: читает только `deal_missing`, а он требует `not_found`, которого у этих ошибок нет при любом `method` | — |
| `Bitrix._raw` | 53 | эквивалентный | без `/rest/` в адресе `split("/rest/")[0]` даёт ту же строку | — |
| `Bitrix._limit_wait` | 3, 12, 17, 18 | пробел | последовательность пауз QUERY_LIMIT_EXCEEDED не «1, 2, 4 … минут, не больше 30» (РАЗРАБОТЧИКУ.md) | `test_long_query_limit_pause_stays_within_30_minutes` (точная последовательность 1, 2, 4, 8, 16, 30, 30) |
| `Bitrix._limit_wait` | 29 | пробел | `>=`: ошибка в момент `operating_reset_at` — срок «уже наступил», паузы нет вовсе | `test_operation_time_limit_when_reset_reached_waits_increasing_pause` |
| `Bitrix._limit_wait` | 34 | пробел | OPERATION_TIME_LIMIT блокирует весь портал, а не метод («Другие … методы продолжают работать», limits.md) | `test_operation_time_limit_blocks_only_that_method` |
| `Bitrix.call_all` | 24 | пробел | `next` равен текущему смещению — бесконечный цикл по той же странице | `test_list_stops_when_next_does_not_advance` |
| `Bitrix.categories` | 3, 5, 11 | пробел | `crm.category.list` без `entityTypeId` или с 3 (контакты) вместо 2 (сделки) | `test_categories_of_deals` |
| `Bitrix.categories` | 13, 16, 17, 22 | пробел | id / названия воронок из ответа теряются (пример ответа статьи) | `test_categories_of_deals` |
| `Bitrix.categories` | 28, 29, 31, 33–37 | пробел | основная воронка «0»: вставка при отказе портала падает, вставляется при наличии, теряется или встаёт не первой («список фильтруется по правам») | `test_categories_of_deals` |
| `Bitrix.categories` | 23, 25 | эквивалентный | расходятся только на ответе без ключа `categories`; по статье он есть всегда | — |
| `Bitrix.comment` | 1–4 | пробел | метод / `fields` `crm.timeline.comment.add` | `test_deal_writes_by_doc` |
| `Bitrix.deal_contacts` | 4, 6 | пробел | `crm.deal.contact.items.get` без `id` сделки | `test_deal_contacts_request_and_equal_sort_keep_answer_order` |
| `Bitrix.deal_contacts` | 32 | пробел | при равном SORT сравниваются словари → TypeError | `test_deal_contacts_request_and_equal_sort_keep_answer_order` |
| `Bitrix.deal_contacts` | 39 | пробел | удалённый контакт роняет сделку, контакт без прав молча пропускается | `test_deleted_contact_skipped`, `test_contact_without_access_is_error` |
| `Bitrix.deal_contacts` | 31 | эквивалентный | `SORT or 1` вместо `or 0`: SORT — integer в каждой привязке (статья метода), подстановка не срабатывает | — |
| `Bitrix.deal_ids_after` | 43 | пробел | полная страница без роста ID (фильтр не применён) и `max == after` — бесконечный опрос | `test_new_deals_without_filter_applied_is_error` |
| `Bitrix.get_contact` | 18, 20 | эквивалентный | `normalize_phone(None)` = `normalize_phone("")` (`raw or ""`) | — |
| `Bitrix.get_contact` | 27 | эквивалентный | `Contact.id` в продукте никто не читает | — |
| `Bitrix.get_deal` | 35, 36, 42, 43 | пробел | STAGE_SEMANTIC_ID S/F → «P» (закрытая сделка в работе), CATEGORY_ID → «0» | `test_deal_fields_from_doc_example` |
| `Bitrix.get_deal` | 10 | эквивалентный | `Deal.id` в продукте никто не читает (робот знает ID сам) | — |
| `Bitrix.get_deal` | 48 | эквивалентный | `_parse_date(None)` даёт тот же None | — |
| `Bitrix.max_deal_id` | 1, 3, 5, 20, 21, 22, 23, 28 | пробел | параметры `crm.deal.list` (ID DESC, start −1), первая строка, пустой портал → 0 | `test_max_deal_id_is_newest_deal` |
| `Bitrix.stages` | 1, 2, 3, 6 | пробел | ENTITY_ID: DEAL_STAGE для основной, DEAL_STAGE_N для воронки N (crm-status-add.md) | `test_stages_of_funnel_requested_by_entity` |

### orbita_api.py, orbita.py

| Функция | Мутанты | Вердикт | Обоснование | Чем убит |
|---|---|---|---|---|
| `_next_utc_midnight` | 2, 5, 8, 11, 16, 17 | пробел | полночь по часам машины, без зоны (наивное время → TypeError при сравнении), +2 суток, 01:00, 00:01 вместо ближайшей полуночи UTC («1000 отправок в сутки UTC») | `test_daily_limit_waits_until_utc_midnight` |
| `_next_utc_midnight` | 14, 15 | эквивалентный | `time(0)` = `time(0, 0)` | — |
| `wait_until` | 14 | пробел | `Retry-After: 1` (спецификация: minimum 1) игнорируется | `test_rate_limit_waits_retry_after` |
| `wait_until` | 13 | эквивалентный | расходится только на 0, а у `Retry-After` и `retry_after` в спецификации minimum 1 | — |
| `verify_signature` | 3, 4 | пробел | при пустом секрете проверка продолжается — подпись пустым секретом принимается | `test_signature_rejected` (подпись делается тем же, пустым, секретом) |
| `verify_signature` | 1, 10 | непродуктивный | окно 300 с — своё число, спецификацией не задано; граница ±1 с эталона не имеет | — |
| `verify_signature` | 32 | непродуктивный | спецификация описывает одну подпись `v1=…`; разделитель списка подписей не задан — эталона нет | — |
| `OrbitaAPIError.__init__`, `OrbitaError.__init__` | 1, 1 | эквивалентный | текст сообщения | — |
| `OrbitaAPI.__init__` | 3, 4 | пробел | адрес API со слешем на конце даёт `//account` | `test_account_by_spec` |
| `OrbitaAPI.__init__` | 8 | пробел | таймаут не доходит до запроса | `test_timeout_from_settings_on_every_request` |
| `OrbitaAPI.__init__` | 1 | непродуктивный | таймаут по умолчанию 45 → 46 с — число без требования | — |
| `OrbitaAPI._request` | 9 | пробел | без таймаута | `test_timeout_from_settings_on_every_request` |
| `OrbitaAPI._request` | 27, 34 | эквивалентный | `""` вместо None для не-JSON тела ведёт в ту же ветку `r.text[:300]`; 301 символ — только текст `detail` в сообщении | — |
| `OrbitaAPI._request` | 39, 41 | эквивалентный | `code` None вместо "" у detail без кода: ни в одном множестве кодов, `definite` и `wait_until` дают то же, `raw` идёт только в текст | — |
| `OrbitaAPI._request` | 48 | эквивалентный | `OrbitaAPIError.detail` никто не читает, срок считается по исходному detail | — |
| `OrbitaAPI._request` | 64 | эквивалентный | расходится только на пустом теле 2xx; у используемых методов 200/201/202 по спецификации всегда JSON | — |
| `OrbitaAPI.account` | 9 | пробел | `[2]` → IndexError | `test_account_by_spec` |
| `OrbitaAPI.channel_status` | 7, 8 | пробел | «подключён хоть у одного» → «у последнего в списке» | `test_channel_connected_if_any_employee_connected` |
| `OrbitaAPI.channels` | 3, 5 | эквивалентный | расходятся только без ключа `items`; в ответе он обязателен | — |
| `OrbitaAPI.channels` | 30 | эквивалентный | «сотрудник» в продукте никто не читает | — |
| `OrbitaAPI.history` | 4, 76, 78 | пробел | граница чтения страниц: `since + CLOCK_SKEW`, `or` (любое время — стоп), `<=` (сообщение ровно на границе запаса — стоп): ответ клиента на следующей странице теряется | `test_history_reads_next_page_within_clock_skew` |
| `OrbitaAPI.history` | 61 | пробел | возвращается только последняя страница | `test_history_follows_next_cursor` (все сообщения всех страниц по порядку), `test_history_repeated_cursor_ends_feed` |
| `OrbitaAPI.history` | 67, 72 | пробел | повтор курсора не останавливает чтение — бесконечный цикл | `test_history_repeated_cursor_ends_feed` |
| `OrbitaAPI.history` | 11 | эквивалентный | `limit` 201: «не больше 200; больше — отдаётся 200» (спецификация) | — |
| `OrbitaAPI.history` | 26, 27, 34, 35, 36 | эквивалентный | `Message.author` / `text` из истории в продукте не читаются (решения — по `outgoing` и `at`) | — |
| `OrbitaAPI.history` | 56, 58 | эквивалентный | только без ключа `items`, а он в ответе есть всегда | — |
| `orbita._parse_time` | 12 | пробел | смещение отбрасывается: 13:10+03:00 становится 13:10 UTC | `test_history_time_without_offset_is_utc` (параметр со смещением) |
| `OrbitaAPI.send_first` | 3 | пробел | при пустом списке каналов вместо ошибки — «номера нет ни в одном мессенджере» (пустой список возможен из `ORBITA_CHANNELS` с неизвестными именами) | `test_send_first_without_selected_channels_is_error` |
| `OrbitaAPI.send_first` | 9 | пробел | неподключённый первый канал обрывает перебор | `test_send_first_goes_on_to_next_channel` |
| `OrbitaAPI.send_first` | 33, 41, 43 | эквивалентный | `raw` результата `no_messenger` в продукте не читается (панель пишет свой текст) | — |
| `OrbitaAPI.start_dialog` | 44, 45 | пробел | 5xx становится «не проверен» (робот идёт в следующий канал при неизвестном исходе) / 422 перестаёт быть «не проверен» | `test_start_dialog_server_error_is_unknown_outcome`, `test_start_dialog_unchecked_keeps_refusal_code_and_wait`, `test_send_first_goes_on_to_next_channel` |
| `OrbitaAPI.start_dialog` | 47, 49, 53 | пробел | статус «unchecked» и код отказа в `raw` (робот выводит его человеку в `last_error`) | `test_start_dialog_unchecked_keeps_refusal_code_and_wait` |
| `OrbitaAPI.start_dialog` | 60, 64, 65 | пробел | канал результата 202 — None или тело ответа; робот записывает его в сделку `pending_first` | `test_start_dialog_names_channel_it_went_to` |
| `OrbitaAPI.start_dialog` | 26, 27, 29, 30, 36, 37, 39, 40, 48, 52 | эквивалентный | `channel` / `raw` результатов not_found, invalid_phone, unchecked: робот и `send_first` берут канал из своего цикла, `raw` invalid_phone идёт только в текст ошибки | — |
| `OrbitaAPI.start_dialog` | 61, 66, 84, 88 | эквивалентный | `raw` (тело) результатов 201/202 никто не читает | — |

### db.py

| Функция | Мутанты | Вердикт | Обоснование | Чем убит |
|---|---|---|---|---|
| `DB.find` | 8, 15 | пробел | фильтр по статусу не применяется / вместе со строкой поиска — ошибка числа параметров SQLite | `test_find_by_status_and_text` |
| `DB.incoming_since` | 11, 12, 21 | пробел | исходящее message.received (direction — enum incoming / outgoing) считается ответом клиента | `test_incoming_since_counts_only_client_messages` |
| `DB.incoming_since` | 20 | пробел | испорченное тело обрывает просмотр — следующий ответ клиента не виден | `test_incoming_since_counts_only_client_messages` |
| `DB.incoming_since` | 22, 24 | эквивалентный | расходятся только без `direction`, а он у message.received обязателен | — |
| `DB.events` | 1 | непродуктивный | размер выборки по умолчанию 300 → 301 — число без требования | — |
| `service_lock` | 3, 5, 7 | эквивалентный | папку базы раньше создаёт `DB()` (`__main__.build`), `parents` в `service_lock` не срабатывает | — |
| `service_lock` | 26–34 | непродуктивный | ветка Windows (`msvcrt`): на Linux, где идут тесты, не исполняется; подделка `msvcrt` проверяла бы саму подделку (§6) | — |

## Дефекты продукта

Новые тесты на настоящем коде прошли с первого запуска: дефекта, который тест показал бы красным, не найдено.
Вопрос к человеку, не подтверждённый дефект:

1. **`normalize_phone`, 10 цифр не на 9.** `"4951234567"` (московский городской без кода страны) становится
   `"+4951234567"` — несуществующий международный номер; уйдёт в Орбиту и получит отказ канала. Требования
   на этот случай нет — поэтому мутанты 31, 32 вынесены в непродуктивные, а не закрыты тестом.

## Эквивалентные автоматически (`equiv-clients.json`)

179 мутантов: 172 класса «текст внутри строк», 7 класса «текст сообщения исключения → None» (сообщения
`_raw` «неожиданный ответ», `deal_ids_after` «фильтр >ID не применён», `deal_by`, `_request`, три в
`send_first`) — эти 7 действительно меняют только текст.

**Класс «текст внутри строк» собран неверно: 74 из 172 — не текст, а контракт запроса или разбора ответа,**
и узкий прогон новыми тестами их убивает: ключи и значения параметров Битрикса (`fields` —
«принимает только параметры с ключом fields в нижнем регистре», `ENTITY_TYPE` `deal`, `entityTypeId`,
`ENTITY_ID`, `DEAL_STAGE`, `order`/`ID`/`DESC`, `start`, `id` …: `comment` 5–16, `max_deal_id` 8–19,
`stages` 4, 5, 7, 8, 16–25, `categories` 8–10, 18, 19, 26, 27, 32, 38, `set_stage` 7, 8, `deal_contacts` 9, 10,
`get_deal` 37, 38, 44, 45, `__init__` 7, `_raw` 55, 56, 58, 59), разбор времени ответа (`_parse_time` 11 — замена `Z` на `+00:00`),
метод и путь Орбиты (`account` 5, 7, 8, `channels` 10 — метод `XXGETXX`), статусы, которые сравнивает код
(`start_dialog` 55, 56 — `unchecked`, `send_first` 27, 28 — `invalid_phone`), поля вебхука в базе
(`incoming_since` 18, 19, 25, 26, `find` 21). Они закрыты теми же тестами, что и пробелы выше.

Остальные 98 действительно эквивалентны:

- регистр SQL — ключевых слов, имён таблиц и колонок, ключей `sqlite3.Row` (SQLite к ним нечувствителен);
- регистр имён заголовков HTTP (`X-Api-Key`, `Idempotency-Key`, `retry-after`, `location`) и метода
  (`get`/`post` — httpx приводит к верхнему), `utf-8` → `UTF-8`;
- тексты сообщений и подписей («Основная воронка», «проверены каналы», «сотрудник»);
- значения по умолчанию полей, которые по статье или спецификации есть всегда (`TITLE`, `STAGE_ID`,
  `STAGE_SEMANTIC_ID`, `CATEGORY_ID`, `NAME`, `VALUE`, `direction`, имя воронки);
- `select` в `deal_ids_after` (21–24): без него «по умолчанию берутся все поля» (`crm-deal-list.md`), `ID`
  в ответе есть;
- `limit` истории (`history` 9, 10): без него Орбита отдаёт страницу своего размера, чтение по `next_cursor`
  то же;
- `Z` в `_parse_time` (9, 10): `datetime.fromisoformat` принимает `Z` с Python 3.11, образ — `python:3.12`;
- `rstrip("XX/XX")` адреса Орбиты (`__init__` 5): адрес API на `X` не кончается.
